#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["numpy", "Pillow>=9.1"]
# ///
"""Put a background behind the text in your terminal, from a background job.

  ./background.py &                     a slow animated plasma behind your text
  ./background.py --color teal &        ...kept to one color family
  ./background.py --still &             marbled swirls that hold still (a new one each run)
  ./background.py --still --seed 42 &   ...the same one every time
  ./background.py --image pic.jpg &     a picture behind your text
  ./background.py --opacity 0.4 ...     stronger (1 is solid; the default is 0.25)
  ./background.py --tty /dev/pts/3 &    draw on another terminal (run `tty` there)
  kill %1                               stop; it removes its image on the way out

Needs a terminal with the kitty graphics protocol: made for Ghostty, and kitty
should work too.

How a background job draws without getting in your way:
  * It never reads from the terminal or changes its settings (a background job
    that does gets stopped), and every command carries q=2. Without that, the
    terminal's replies would show up at your prompt as if you had typed them.
  * Each frame goes out in a single write(). Linux keeps a write to a terminal in
    one piece, so the shell's output can't land in the middle of a frame. macOS
    doesn't: there, while output scrolls by nonstop, a line can occasionally get
    swallowed (about 1 in 1,000 in testing; none in normal interactive use).
  * Each frame saves and restores the cursor (ESC 7 / ESC 8), so the cursor you're
    typing at never moves.
  * Images are pinned to cells, not to the window: they scroll away with your
    output, and `clear` deletes them. So it re-pins the image on every tick.
"""

import argparse
import base64
import colorsys
import errno
import fcntl
import io
import os
import random
import re
import signal
import struct
import sys
import termios
import time
import zlib

import numpy as np
from PIL import Image, ImageOps

IMAGE_ID = 0x4F56524C  # arbitrary; well away from the small ids other programs use
CHUNK = 4096           # the protocol takes base64 payloads in chunks of at most 4096 bytes

# Hue (0-1) for each named color. "rainbow" slowly drifts through all of them.
COLORS = {"rainbow": None, "red": 0.0, "orange": 0.07, "yellow": 0.14, "green": 0.33,
          "teal": 0.47, "blue": 0.6, "purple": 0.75, "pink": 0.88}
SATURATION = 0.65


# --- kitty graphics protocol -------------------------------------------------

def apc(keys, payload=b""):
    """One graphics command: ESC _ G <keys> [; <payload>] ESC \\"""
    return b"\x1b_G" + keys.encode() + (b";" + payload if payload else b"") + b"\x1b\\"


def chunked(keys, data):
    """Send data base64-encoded, split into the chunks the protocol wants (m=1 ... m=0)."""
    b64 = base64.standard_b64encode(data)
    parts = [b64[i:i + CHUNK] for i in range(0, len(b64), CHUNK)] or [b""]
    out = []
    for k, part in enumerate(parts):
        more = int(k < len(parts) - 1)
        out.append(apc(f"{keys},m={more}" if k == 0 else f"m={more},q=2", part))
    return b"".join(out)


def at(col, row, payload):
    """Run payload with the cursor at (col, row), then put the cursor back."""
    return b"\x1b7" + f"\x1b[{row + 1};{col + 1}H".encode() + payload + b"\x1b8"


class Tty:
    def __init__(self, path):
        self.fd = os.open(path, os.O_WRONLY | os.O_NOCTTY)

    def size(self):
        """(cols, rows, cell_w, cell_h); asking for the size is fine from a background job."""
        rows, cols, xpx, ypx = struct.unpack(
            "HHHH", fcntl.ioctl(self.fd, termios.TIOCGWINSZ, b"\0" * 8))
        cols, rows = max(cols, 1), max(rows, 1)
        return cols, rows, (xpx / cols if xpx else 10.0), (ypx / rows if ypx else 20.0)

    def send(self, data):
        view = memoryview(data)
        while view:
            view = view[os.write(self.fd, view):]


# --- the backgrounds ---------------------------------------------------------

def hsv_to_rgb(h, s, v):
    """Vectorized HSV -> RGB, inputs in 0-1 (hue wraps). Returns a float array (..., 3)."""
    h = (np.asarray(h) % 1.0) * 6
    i = h.astype(int) % 6
    f = h - np.floor(h)
    p, q, t = v * (1 - s), v * (1 - s * f), v * (1 - s * (1 - f))
    r = np.choose(i, [v, q, p, p, t, v])
    g = np.choose(i, [t, v, v, q, p, p])
    b = np.choose(i, [p, p, t, v, v, q])
    return np.stack([r, g, b], axis=-1)


class Plasma:
    """A slow, see-through plasma underneath your text.

    z=-1 puts it below the text but above cell background colors, so it also shows
    through apps that paint their own backgrounds (vim, htop). Fading uses alpha, so
    it blends into whatever your terminal's background is, light or dark.
    """

    z = -1
    width = 96  # a tiny source image; the terminal stretches and smooths it

    def __init__(self, hue, saturation, opacity):
        self.hue, self.saturation = hue, saturation
        self.alpha = round(opacity * 255)
        self.t0 = time.monotonic()

    def tick(self, cols, rows, cw, ch):
        w = self.width
        h = max(8, round(w * rows * ch / (cols * cw)))
        y, x = np.mgrid[0:h, 0:w] / w
        t = time.monotonic() - self.t0
        v = (np.sin(x * 9 + t * 0.7) + np.sin(y * 7 - t * 0.5)
             + np.sin((x + y) * 6 + t * 0.3)
             + np.sin(np.hypot(x - 0.5, y - 0.3) * 14 - t)) / 4  # -1 to 1
        if self.hue is None:
            hue = 0.62 + 0.18 * v + t * 0.005  # drifts through every hue, a lap every ~3.5 min
        else:
            hue = self.hue + 0.05 * v          # stays close to the chosen color
        px = np.empty((h, w, 4), np.uint8)
        px[..., :3] = hsv_to_rgb(hue, self.saturation, 0.75 + 0.25 * v) * 255
        px[..., 3] = self.alpha
        body = chunked(f"a=T,f=32,s={w},v={h},i={IMAGE_ID},p=1,c={cols},r={rows},"
                       f"z={self.z},C=1,o=z,q=2", zlib.compress(px.tobytes(), 1))
        return at(0, 0, body)


def smooth_noise(rng, h, w, falloff):
    """A smooth random field that wraps at the edges, roughly -1 to 1: random waves
    whose strength falls off with frequency (as 1/f^falloff), summed by an inverse FFT."""
    fy, fx = np.meshgrid(np.fft.fftfreq(h), np.fft.rfftfreq(w), indexing="ij")
    f = np.hypot(fx, fy)
    f[0, 0] = np.inf  # no constant term
    waves = (rng.standard_normal(f.shape) + 1j * rng.standard_normal(f.shape)) / f ** falloff
    field = np.fft.irfft2(waves, s=(h, w))
    return field / (3 * field.std())


def lookup(field, x, y):
    """Read field at fractional pixel positions (bilinear, wrapping at the edges)."""
    h, w = field.shape
    x0, y0 = np.floor(x), np.floor(y)
    fx, fy = x - x0, y - y0
    x0, y0 = x0.astype(int) % w, y0.astype(int) % h
    x1, y1 = (x0 + 1) % w, (y0 + 1) % h
    top = field[y0, x0] * (1 - fx) + field[y0, x1] * fx
    bottom = field[y1, x0] * (1 - fx) + field[y1, x1] * fx
    return top * (1 - fy) + bottom * fy


def still(seed, hue, saturation, size=(1280, 800)):
    """Marbled swirls that hold still, made by warping noise with noise, twice.

    Every pixel looks up a noise field ("the ink") at a spot pushed sideways by other
    noise fields, which were themselves read at pushed spots. Very smooth fields do
    the pushing, so the ink folds and streaks like ink in water instead of crumbling.
    The same seed always draws the same swirls. Rainbow's hue is None: then the seed
    also picks the colors.
    """
    w, h = size
    rng = np.random.default_rng(seed)
    ax, ay, bx, by = (smooth_noise(rng, h, w, 3.2) for _ in range(4))  # the pushers
    ink = smooth_noise(rng, h, w, 2.0)
    y, x = np.mgrid[0:h, 0:w]
    push = 0.55 * w
    px, py = x + push * ax, y + push * ay
    qx, qy = lookup(bx, px, py), lookup(by, px, py)
    v = np.tanh(2 * lookup(ink, x + push * qx, y + push * qy))  # more contrast, still -1 to 1
    if hue is None:
        hue = rng.random() + 0.18 * np.clip(qx + ay, -1, 1)  # spread around a random hue
    rgb = hsv_to_rgb(hue + 0.05 * v, saturation, 0.75 + 0.25 * v)
    return Image.fromarray((rgb * 255).astype(np.uint8), "RGB")


class Picture:
    """A picture (or a generated still) behind your text, cropped to fill the window
    and faded with alpha.

    The pixels go over the wire once. An invisible "virtual" placement (U=1, normally
    used for Unicode-placeholder images) keeps them alive: Ghostty's `clear` deletes
    every placement except virtual ones, and only frees images nothing refers to.
    So after the first send, each tick is a ~60-byte re-pin.
    """

    z = -1
    max_width = 1280     # more pixels than this just costs bandwidth behind faded text
    resend_every = 20.0  # seconds; recovers after `reset`, and inside vim/less (alt screen)

    def __init__(self, img, opacity):
        img = img.convert("RGBA")
        img.thumbnail((2 * self.max_width, 2 * self.max_width), Image.Resampling.LANCZOS)
        img.putalpha(img.getchannel("A").point(lambda a: round(a * opacity)))
        self.img = img
        self._png = (None, b"")
        self._sent = (None, 0.0)

    def _png_for(self, cols, rows, cw, ch):
        if self._png[0] != (cols, rows):
            aspect = cols * cw / (rows * ch)
            # Crop to the window's shape, never upscale: the terminal stretches it for free.
            w = min(self.max_width, round(cols * cw),
                    round(min(self.img.width, self.img.height * aspect)))
            h = max(1, round(w / aspect))
            buf = io.BytesIO()
            ImageOps.fit(self.img, (w, h), Image.Resampling.LANCZOS).save(buf, "PNG")
            self._png = ((cols, rows), buf.getvalue())
        return self._png[1]

    def tick(self, cols, rows, cw, ch):
        place = at(0, 0, apc(f"a=p,i={IMAGE_ID},p=1,c={cols},r={rows},z={self.z},C=1,q=2"))
        size, sent_at = self._sent
        if size == (cols, rows) and time.monotonic() - sent_at < self.resend_every:
            return place
        self._sent = ((cols, rows), time.monotonic())
        return (chunked(f"a=t,f=100,i={IMAGE_ID},q=2", self._png_for(cols, rows, cw, ch))
                + apc(f"a=p,U=1,i={IMAGE_ID},p=2,c={cols},r={rows},q=2")  # the keep-alive
                + place)


# --- command line ------------------------------------------------------------

def parse_color(text):
    """A color name, or a hex color like ff8800 -> (hue, saturation). Rainbow's hue is None."""
    name = text.strip().lower()
    if name in COLORS:
        return COLORS[name], SATURATION
    m = re.fullmatch(r"#?([0-9a-f]{6})", name)
    if not m:
        raise argparse.ArgumentTypeError(
            f"use one of {', '.join(COLORS)}, or a hex color like ff8800")
    r, g, b = (int(m.group(1)[i:i + 2], 16) / 255 for i in (0, 2, 4))
    hue, saturation, _ = colorsys.rgb_to_hsv(r, g, b)
    return hue, saturation


def main():
    ap = argparse.ArgumentParser(prog="background.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--still", action="store_true",
                    help="marbled swirls that hold still instead of the moving plasma; "
                         "a new one each run")
    ap.add_argument("--seed", type=int, metavar="N",
                    help="with --still: which one to draw; the same number draws the same "
                         "one (default: random, and printed so you can get it back)")
    ap.add_argument("--image", metavar="PATH", help="show a picture instead of the plasma")
    ap.add_argument("--color", type=parse_color, metavar="COLOR",
                    help=f"color for the plasma or --still: {', '.join(COLORS)}, or a hex "
                         "color like ff8800 (default: rainbow)")
    ap.add_argument("--opacity", type=float, default=0.25,
                    help="1 is solid; lower fades it into your terminal's background "
                         "(default: 0.25)")
    ap.add_argument("--tty", metavar="DEVICE",
                    help="terminal to draw on (default: this one; run `tty` in another "
                         "terminal to get its name)")
    ap.add_argument("--fps", type=float,
                    help="redraw rate (default: 8 for the plasma, 4 for a picture or --still)")
    args = ap.parse_args()
    if args.image and args.still:
        ap.error("pick one of --image and --still")
    if args.image and args.color:
        ap.error("--color is for the plasma and --still; it doesn't apply to --image")
    if args.seed is not None and not args.still:
        ap.error("--seed goes with --still")
    if args.fps is not None and args.fps <= 0:
        ap.error("--fps must be more than 0")
    opacity = max(0.0, min(1.0, args.opacity))

    # With `stty tostop`, a background job writing to its terminal gets SIGTTOU and
    # stops. If the signal is ignored, the kernel lets the write through instead.
    signal.signal(signal.SIGTTOU, signal.SIG_IGN)

    path = args.tty
    if not path:
        try:
            path = os.ttyname(sys.stdout.fileno())
        except OSError:
            sys.exit("background: stdout isn't a terminal; pass --tty "
                     "(run `tty` in the terminal you want)")
    try:
        tty = Tty(path)
    except OSError as e:
        sys.exit(f"background: can't open {path}: {e.strerror}")

    hue, saturation = args.color or parse_color("rainbow")
    if args.image:
        try:
            mode = Picture(ImageOps.exif_transpose(Image.open(args.image)), opacity)
        except OSError as e:
            sys.exit(f"background: can't open {args.image}: {e}")
        interval = 1 / (args.fps or 4)
    elif args.still:
        seed = args.seed
        if seed is None:
            seed = random.randrange(100000)
            print(f"background: still {seed} (--seed {seed} draws it again)", file=sys.stderr)
        mode = Picture(still(seed, hue, saturation), opacity)
        interval = 1 / (args.fps or 4)
    else:
        mode = Plasma(hue, saturation, opacity)
        interval = 1 / (args.fps or 8)

    stopping = []
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, lambda *_: stopping.append(True))

    try:
        while not stopping:
            tty.send(mode.tick(*tty.size()))
            time.sleep(interval)
        tty.send(apc(f"a=d,d=I,i={IMAGE_ID},q=2"))  # remove our image on the way out
    except OSError as e:
        if e.errno not in (errno.EIO, errno.ENXIO):
            raise
        # The terminal went away, so there's nothing left to clean up.


if __name__ == "__main__":
    main()
