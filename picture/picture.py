#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["Pillow>=9.1"]
# ///
r"""Pop a picture up in a corner of your terminal, from a background job.

  ./picture.py cat.jpg &
  ./picture.py plot.png --at bottom-left --size 40x16 &   where it goes, and the most it
                                                         covers (in cells)
  ./picture.py https://example.com/radar.png --every 5m & fetch it again every 5 minutes
  ./picture.py cat.jpg --for 10m &                        go away by itself after ten
                                                         minutes
  kill %1                                                 stop it; it removes its
                                                         picture on the way out

A file is watched: write a new picture over it (a script redrawing a chart, say) and the
new one shows within a second. Anything Pillow can open works: PNG, JPEG, GIF, WebP, and
so on. An animated GIF (or PNG, or WebP) plays, over and over. The picture keeps its
shape, as big as fits in --size.

For agents (a Claude, say) and scripts, which run commands without a terminal of their own:
  ./picture.py plot.png --detach   load it once, start showing it in the background,
                                   print its id and return. It finds the terminal
                                   through the processes that started it, and goes away
                                   after 30 minutes unless --for says otherwise (--for 0:
                                   never).
  ./picture.py --list              what's showing, and where
  ./picture.py --stop ID           stop one: an id from --list, or a corner of this
                                   terminal; --stop all stops every one

Needs a terminal with the kitty graphics protocol: made for Ghostty, and kitty should
work too. It draws the same way ../graph does: it never reads from the terminal, every
command is quiet (q=2), each frame is one write that saves and restores the cursor, and
the picture is re-pinned forty times a second, so scrolling or `clear` only moves or
hides it for a moment.
"""

import argparse
import base64
import errno
import fcntl
import hashlib
import io
import json
import math
import os
import re
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import traceback
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from PIL import Image, ImageChops, ImageDraw, ImageOps

IMAGE_ID = 0x50494354  # "PICT", plus the corner's index: one picture per corner
CORNERS = ["top-right", "top-left", "bottom-right", "bottom-left", "center"]
CHUNK = 4096           # the protocol takes base64 payloads in chunks of at most 4096 bytes
DETACHED_FOR = 1800    # seconds a --detach picture lasts unless --for says otherwise
REPIN_EVERY = 0.025    # seconds; how long the picture can ride along when the text scrolls
RESEND_EVERY = 10.0    # seconds; the same picture goes out again this often, so it comes
                       # back after `reset`, and shows up inside vim or less
BEHIND_OPACITY = 0.4   # --behind: faint enough that the text in front stays easy to read
MAX_BYTES = 50 << 20   # a URL that sends more than this isn't a picture
MAX_SIDE = 4096        # bigger pictures are shrunk to this when they load; no corner needs more
# Animations (GIF, and animated PNG and WebP):
MAX_FRAMES = 500             # more than this, and every other frame (or third...) is kept
FRAME_MEMORY = 256 << 20     # bytes of frames kept here, unpacked; more frames, smaller each
TERMINAL_MEMORY = 128 << 20  # bytes of frames the terminal keeps for us, unpacked. Ghostty
                             # and kitty keep 320 MB of pictures per screen, for everyone.


# --- kitty graphics protocol (the same as ../graph) ----------------------------

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


def find_tty():
    """The terminal to draw on: ours, or else the nearest parent process's.

    A command that an agent or an editor runs usually has no terminal of its own, but
    the program that started it (Claude Code, say) is running in one.
    """
    for fd in (1, 2, 0):
        try:
            return os.ttyname(fd)
        except OSError:
            pass
    pid = os.getpid()  # our own controlling terminal first, then our parents'
    while pid > 1:
        fields = ps(pid, "ppid=,tty=").split()
        if len(fields) != 2:
            return None
        if fields[1] not in ("?", "??", "-") and os.path.exists("/dev/" + fields[1]):
            return "/dev/" + fields[1]
        pid = int(fields[0])
    return None


def ps(pid, fields):
    try:
        return subprocess.run(["ps", "-o", fields, "-p", str(pid)], capture_output=True,
                              text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


# --- what's showing ------------------------------------------------------------
#
# Each picture keeps a small file in a private directory while it shows, so --list and
# --stop can find it, and so a new picture can replace an old one in the same corner of
# the same terminal (two would fight over the same image).

def state_dir():
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    path = os.path.join(base, f"terminal-fun-picture-{os.getuid()}")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def register(tty_path, corner, title, until):
    path = os.path.join(state_dir(), f"{os.getpid()}.json")
    info = {"pid": os.getpid(), "tty": tty_path, "at": corner, "title": title,
            "started": time.time(), "until": until,
            "lstart": ps(os.getpid(), "lstart=")}  # tells a reused pid from ours
    with open(path + ".tmp", "w") as f:
        json.dump(info, f)
    os.replace(path + ".tmp", path)
    return path


def running():
    """The pictures that are showing now, oldest first. Clears away files left by ones
    that were killed before they could remove their own."""
    out = []
    folder = state_dir()
    for name in os.listdir(folder):
        if not name.endswith(".json"):
            continue
        path = os.path.join(folder, name)
        try:
            with open(path) as f:
                p = json.load(f)
            alive = bool(p["lstart"]) and ps(p["pid"], "lstart=") == p["lstart"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if alive:
            out.append(p)
        else:
            try:
                os.remove(path)
            except OSError:
                pass
    return sorted(out, key=lambda p: p["started"])


def stop_pictures(pictures, wait=3.0):
    """SIGTERM them and give them a moment to remove their images. One that's stuck (in a
    write to a terminal that stopped reading, say) gets SIGKILL; its image stays until
    it scrolls away or the screen is cleared."""
    def alive():
        return [p for p in pictures if ps(p["pid"], "lstart=") == p["lstart"]]

    for sig in (signal.SIGTERM, signal.SIGKILL):
        for p in alive():
            try:
                os.kill(p["pid"], sig)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline and alive():
            time.sleep(0.05)
        if not alive():
            return
    for p in pictures:  # killed, so it couldn't remove its own file
        try:
            os.remove(os.path.join(state_dir(), f"{p['pid']}.json"))
        except OSError:
            pass


def human(seconds):
    s = max(0, round(seconds))
    if s < 90:
        return f"{s}s"
    if s < 90 * 60:
        return f"{round(s / 60)}m"
    return f"{s / 3600:.1f}h".replace(".0h", "h")


def list_pictures():
    pictures = running()
    if not pictures:
        print("picture: nothing showing")
        return
    now = time.time()
    for p in pictures:
        left = f", stops in {human(p['until'] - now)}" if p.get("until") else ""
        print(f"{p['pid']:>7}  {p['tty']}  {p['at']:<12}  "
              f"up {human(now - p['started'])}{left}  {p['title']}")


def stop_command(target):
    pictures = running()
    if target == "all":
        pick = pictures
    elif target in CORNERS:
        here = find_tty()
        pick = [p for p in pictures if p["at"] == target and p["tty"] == here]
    else:
        pick = [p for p in pictures if str(p["pid"]) == target]
    if not pick:
        sys.exit(f"picture: nothing to stop for {target!r} (see --list)")
    stop_pictures(pick)
    for p in pick:
        print(f"picture: stopped {p['pid']} ({p['title']})")


# --- the picture ---------------------------------------------------------------

class Source:
    """Where the picture comes from: a file, looked at for changes, or an http(s) URL."""

    def __init__(self, where):
        self.where = where
        self.is_url = bool(re.match(r"https?://", where, re.I))
        self._stamp = None   # a file's (mtime, size, inode) when it last loaded
        self._digest = None  # a hash of the bytes that last loaded

    def label(self):
        name = self.where if self.is_url else os.path.basename(self.where) or self.where
        return name if len(name) <= 48 else name[:47] + "…"

    def load(self):
        """The picture's frames (see decode), if it's new since the last load; otherwise
        None. Raises RuntimeError when it can't be read."""
        stamp = None
        try:
            if self.is_url:
                data = self._fetch()
            else:
                st = os.stat(self.where)
                stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
                if stamp == self._stamp:
                    return None
                with open(self.where, "rb") as f:
                    data = f.read()
        except OSError as e:
            raise RuntimeError(e.strerror or str(e)) from None
        digest = hashlib.sha256(data).digest()
        if digest == self._digest:
            self._stamp = stamp
            return None
        img = decode(data)  # before taking note of it: a file caught halfway through
        self._stamp, self._digest = stamp, digest  # being written is tried again
        return img

    def _fetch(self):
        req = Request(self.where, headers={"User-Agent": "terminal-fun-picture"})
        try:
            with urlopen(req, timeout=30) as r:
                data = r.read(MAX_BYTES + 1)
        except HTTPError as e:
            raise RuntimeError(f"HTTP {e.code} {e.reason}") from None
        except URLError as e:
            raise RuntimeError(str(e.reason)) from None
        if len(data) > MAX_BYTES:
            raise RuntimeError(f"more than {MAX_BYTES >> 20} MB: not a picture")
        return data


def decode(data):
    """Bytes -> the picture's frames: [[an RGBA image, the seconds it shows], ...]. A still
    is one frame, shown for None seconds: for good. A still is turned the right way up
    and kept to MAX_SIDE; an animation's frames get smaller the more of them there are."""
    try:
        img = Image.open(io.BytesIO(data))
        # Only these animate. A camera's JPEG (MPO) or a TIFF can hold more than one
        # picture too, but those aren't frames.
        if not (getattr(img, "is_animated", False) and img.format in ("GIF", "PNG", "WEBP")):
            img.load()
            img = ImageOps.exif_transpose(img).convert("RGBA")
            img.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
            return [[img, None]]
        n = img.n_frames
        keep = math.ceil(n / MAX_FRAMES)  # every keep-th frame; the ones between add their time
        side = max(64, min(MAX_SIDE, math.isqrt(FRAME_MEMORY // 4 // math.ceil(n / keep))))
        frames = []
        for i in range(n):
            img.seek(i)  # Pillow puts each frame together from the ones before it
            ms = img.info.get("duration") or 0
            seconds = (ms if ms > 10 else 100) / 1000  # 0 or 10 ms means 100, as in browsers
            if i % keep == 0:
                frame = img.convert("RGBA")
                frame.thumbnail((side, side), Image.Resampling.LANCZOS)
                frames.append([frame, seconds])
            else:
                frames[-1][1] += seconds
        return frames
    except Exception as e:  # Pillow raises all sorts, for all sorts of files
        raise RuntimeError(f"not a picture Pillow can read ({e})") from None


def corners(size, radius, opacity):
    """An alpha mask the size of a picture: its corners rounded off, smoothly, and the
    rest at opacity."""
    S = 4  # draw it big and shrink it, for smooth edges
    radius = min(radius, size[0] / 2, size[1] / 2)
    mask = Image.new("L", (size[0] * S, size[1] * S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, mask.width - 1, mask.height - 1),
                                           radius=radius * S, fill=round(255 * opacity))
    return mask.resize(size, Image.Resampling.BOX)


# --- placement -----------------------------------------------------------------

class Panel:
    def __init__(self, source, size, corner, density, opacity, behind=False):
        self.source, self.corner = source, corner
        self.density, self.opacity = density, opacity
        # z >= 0 draws over the text. z < 0 draws under it, but still over the cells'
        # background colors, so it shows through apps that paint their own (vim, htop).
        self.z = -1 if behind else 10
        self.want_cols, self.want_rows = size
        # Each frame of an animation is an image of its own, sent once and then shown in
        # turn, so playing it sends no pixels. Frame i is image base_id + i.
        self.base_id = IMAGE_ID + (CORNERS.index(corner) << 12)
        self.picture = None  # the newest picture that loaded: its frames, from decode
        self._version = 0    # goes up by one each time a new picture loads
        # (number, [(PNG, seconds), ...], (cols, rows) they cover): the newest rendering,
        # from the work thread
        self.frame = None
        self._size = None    # the terminal's size, for the work thread
        self._number = None  # the rendering being shown
        self._sent_at = []   # when each of its frames was last sent (None: not yet)
        self._index = 0      # the frame showing
        self._next_at = 0.0  # when the next one is due
        self._shown = None   # the image showing (placement 1), if any
        self._ids = 0        # images base_id ... base_id + _ids - 1 may be in the terminal
        # Pictures go over in a temporary file that the terminal reads and deletes (t=t),
        # so sending one is a write of ~150 bytes, not hundreds of KB. Over SSH the
        # terminal can't see our files, so there the pixels go in the command itself.
        self.via_file = not (os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"))
        self._files = []  # (path, when sent): files the terminal hasn't taken yet

    def max_cells(self, cols, rows):
        """The most cells the picture can cover, in a window this size."""
        return max(4, min(self.want_cols, cols - 2)), max(2, min(self.want_rows, rows - 4))

    def place(self, cols, rows, c, r):
        """Where the top-left cell of a c x r picture goes."""
        col = {"top-left": 1, "bottom-left": 1, "center": (cols - c) // 2}.get(self.corner,
                                                                              cols - c - 1)
        row = {"bottom-right": rows - r - 4, "bottom-left": rows - r - 4,
               "center": (rows - r) // 2}.get(self.corner, 0)
        return max(0, col), max(0, row)

    def render(self, cols, rows, cw, ch):
        """-> ([(PNG, seconds it shows), ...] for each frame, the (cols, rows) they cover)."""
        frames = self.picture
        img = frames[0][0]
        most_c, most_r = self.max_cells(cols, rows)
        # As big as fits without changing its shape, in the terminal's pixels; then the
        # cells that takes, rounded up. What's left over is a transparent margin of
        # less than a cell, which keeps the picture's shape exactly.
        fit = min(most_c * cw / img.width, most_r * ch / img.height)
        c = min(most_c, max(1, math.ceil(img.width * fit / cw - 1e-6)))
        r = min(most_r, max(1, math.ceil(img.height * fit / ch - 1e-6)))
        # Pixels sent per terminal pixel: density, but never more than the picture has.
        # The terminal stretches it for free.
        k = min(self.density, 1 / fit)
        if len(frames) > 1:
            # The terminal keeps every frame, unpacked, at four bytes a pixel.
            k = min(k, math.sqrt(TERMINAL_MEMORY / (4 * len(frames) * c * cw * r * ch)))
        W, H = max(1, round(c * cw * k)), max(1, round(r * ch * k))
        w = min(W, max(1, round(img.width * fit * k)))
        h = min(H, max(1, round(img.height * fit * k)))
        # Corners rounded like graph's panels: 10 px for a 17 px cell.
        mask = corners((w, h), 10 / 17 * ch * k, self.opacity)
        out = []
        for frame, seconds in frames:
            pic = (frame.copy() if frame.size == (w, h)
                   else frame.resize((w, h), Image.Resampling.LANCZOS))
            pic.putalpha(ImageChops.multiply(pic.getchannel("A"), mask))
            canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            canvas.paste(pic, ((W - w) // 2, (H - h) // 2))
            buf = io.BytesIO()
            canvas.save(buf, "PNG")
            out.append((buf.getvalue(), seconds))
        return out, (c, r)

    def first_frame(self, picture, size):
        """Draw the first rendering now, so a picture that replaces another one shows up
        the moment the old one goes."""
        self.picture, self._size = picture, size
        frames, cells = self.render(*size)
        self.frame = (0, frames, cells)

    def start(self, stop, every):
        """Look for a new picture, and draw frames, in a thread of its own. Drawing a big
        picture takes a moment, and re-pinning mustn't wait for it, or the picture rides
        along with any text that scrolls meanwhile."""
        threading.Thread(target=self._work, args=(stop, every), daemon=True).start()

    def _work(self, stop, every):
        drawn = (self._version, self._size)  # first_frame drew this one
        looked = time.monotonic()
        failures, complained = 0, False
        while not stop.is_set():
            if every and time.monotonic() - looked >= every:
                looked = time.monotonic()
                try:
                    img = self.source.load()
                    failures = 0
                    if img:
                        self.picture, self._version = img, self._version + 1
                except RuntimeError as e:
                    # Say so once it lasts: a file caught halfway through being written
                    # reads fine a moment later.
                    failures += 1
                    if failures == 2:
                        print(f"picture: {self.source.where}: {e} (still showing the last "
                              "one)", file=sys.stderr)
            stamp = (self._version, self._size)
            try:
                if stamp != drawn:  # a new picture, or the window changed size
                    drawn = stamp
                    frames, cells = self.render(*stamp[1])
                    self.frame = (self.frame[0] + 1, frames, cells)
            except Exception:  # a bug: say so once, and keep showing the last rendering
                if not complained:
                    complained = True
                    traceback.print_exc()
            stop.wait(0.05)

    def transmit(self, keys, data):
        """The command that sends a picture: by temporary file, or failing that, in
        base64 chunks."""
        if self.via_file:
            try:
                fd, path = tempfile.mkstemp(prefix="tty-graphics-protocol-picture-",
                                            suffix=".png")
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                self._files.append((path, time.monotonic()))
                return apc(f"a=T,f=100,{keys},t=t", base64.standard_b64encode(path.encode()))
            except OSError:
                self.via_file = False
        return chunked(f"a=T,f=100,{keys}", data)

    def check_files(self):
        """A terminal that can read our files deletes each one as it takes it. One that's
        still there after a few seconds means it can't (it's on another machine, say):
        from then on, send the pixels themselves, every frame again."""
        now = time.monotonic()
        if any(now - t > 5 and os.path.exists(p) for p, t in self._files):
            self.via_file, self._sent_at = False, [None] * len(self._sent_at)
            self.close()
        self._files = [(p, t) for p, t in self._files if now - t <= 5]

    def close(self):
        for path, _ in self._files:
            try:
                os.remove(path)
            except OSError:
                pass
        self._files = []

    def forget(self, keep=0):
        """Commands that delete our images from frame `keep` on, pixels and all: the
        frames an animation no longer has, or (keep=0) everything, on the way out."""
        out = b"".join(apc(f"a=d,d=I,i={self.base_id + i},q=2") for i in range(keep, self._ids))
        if self._shown is not None and self._shown >= self.base_id + keep:
            self._shown = None
        self._ids = keep
        return out

    def tick(self, cols, rows, cw, ch):
        self._size = (cols, rows, cw, ch)
        if self._files:
            self.check_files()
        number, frames, (c, r) = self.frame
        now = time.monotonic()
        out = b""
        if number != self._number:  # a new picture, or a new size: every frame goes again
            out = self.forget(len(frames))  # if there were more before
            self._number, self._sent_at = number, [None] * len(frames)
            self._index, self._next_at = 0, now + (frames[0][1] or 0)
        elif len(frames) > 1 and now >= self._next_at:
            if now - self._next_at > 2:  # we were stopped a while (^Z, a sleeping laptop)
                self._next_at = now
            while now >= self._next_at:  # skip frames that are already over
                self._index = (self._index + 1) % len(frames)
                self._next_at += frames[self._index][1]
        image_id = self.base_id + self._index
        col, row = self.place(cols, rows, c, r)
        keys = f"i={image_id},p=1,c={c},r={r},z={self.z},C=1,q=2"
        sent = self._sent_at[self._index]
        # Each frame goes out the first time it's shown, and again when it's shown 10
        # seconds or more after that. That brings it back after `reset`, or inside vim
        # (a separate screen, with its own images).
        if sent is None or now - sent >= RESEND_EVERY:
            self._sent_at[self._index] = now
            # Plus a hidden second placement (U=1, a "virtual" one, shown only where
            # special placeholder characters are, so never here). `clear` and full-screen
            # redraws delete the visible placement and free any picture nothing else
            # refers to; this keeps ours, so the next re-pin brings it straight back.
            body = (self.transmit(keys, frames[self._index][0])
                    + apc(f"a=p,U=1,i={image_id},p=2,c={c},r={r},q=2"))
        else:
            # Pixels it already has: re-pin placement 1 so scrolling can't carry it off.
            body = apc(f"a=p,{keys}")
        if self._shown not in (None, image_id):
            # The frame before, now that this one is over it. Only its placement: its
            # pixels stay for next time round.
            body += apc(f"a=d,d=i,i={self._shown},p=1,q=2")
        self._shown = image_id
        return out + at(col, row, body)


# --- command line --------------------------------------------------------------

def parse_size(s):
    m = re.fullmatch(r"(\d+)x(\d+)", s)
    if not m:
        raise argparse.ArgumentTypeError("size is COLSxROWS, e.g. 60x20")
    return int(m.group(1)), int(m.group(2))


def parse_duration(s):
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", s)
    if not m:
        raise argparse.ArgumentTypeError("durations look like 90, 90s, 15m, 2h, 1d")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def detach(path, lifetime):
    """Start a copy of ourselves showing the picture in the background, in a session of
    its own with no stdin or stdout, so whatever ran us gets its prompt back. Returns its
    pid once it has registered, so --list and --stop see it straight away."""
    argv = [a for a in sys.argv[1:] if a != "--detach"]
    log_path = os.path.join(state_dir(), "detached.log")
    with open(log_path, "a") as log:
        child = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), *argv, "--tty", path,
             "--for", str(lifetime)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log,
            start_new_session=True)
    registered = os.path.join(state_dir(), f"{child.pid}.json")
    deadline = time.monotonic() + 30
    while not os.path.exists(registered):
        if child.poll() is not None or time.monotonic() > deadline:
            try:
                with open(log_path) as f:
                    tail = f.read().strip().splitlines()[-1:]
            except OSError:
                tail = []
            sys.exit(f"picture: the background copy didn't start"
                     f"{': ' + tail[0] if tail else ''}")
        time.sleep(0.05)
    return child.pid


def main():
    ap = argparse.ArgumentParser(prog="picture.py", description=__doc__, allow_abbrev=False,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", nargs="?", metavar="FILE|URL",
                    help="the picture: a file (watched for changes) or an http(s) URL")
    ap.add_argument("--at", choices=CORNERS, default="top-right",
                    help="where on the screen: a corner, or center (default: top-right)")
    ap.add_argument("--size", type=parse_size, default=(60, 20),
                    help="COLSxROWS: the most it covers. It keeps its shape, and shrinks "
                         "to fit the window (default: 60x20)")
    ap.add_argument("--every", type=parse_duration, metavar="DURATION",
                    help="how often to look for a new picture (default: every second for "
                         "a file; a URL is fetched once unless this is set)")
    ap.add_argument("--behind", action="store_true",
                    help="draw behind the text instead of over it, faded (see --opacity)")
    ap.add_argument("--opacity", type=float,
                    help=f"1 is solid (the default); lower fades it ({BEHIND_OPACITY} with "
                         "--behind)")
    ap.add_argument("--density", type=float,
                    help="the most pixels sent per pixel the terminal reports (default: 2, "
                         "sharp on Retina; 1 with --behind)")
    ap.add_argument("--tty", metavar="DEVICE",
                    help="terminal to draw on (default: this one, or the one the process "
                         "that started this is running in)")
    ap.add_argument("--for", dest="lifetime", type=parse_duration, metavar="DURATION",
                    help="go away after this long; 0 means never (default: never, or 30m "
                         "with --detach)")
    ap.add_argument("--detach", action="store_true",
                    help="load the picture once, start showing it in the background, print "
                         "the id and return")
    ap.add_argument("--list", action="store_true", help="list the pictures that are showing")
    ap.add_argument("--stop", metavar="ID",
                    help="stop a picture: an id from --list, a corner of this terminal, or "
                         "all")
    args = ap.parse_args()

    if args.list:
        return list_pictures()
    if args.stop:
        return stop_command(args.stop)
    if not args.source:
        ap.error("which picture? give a file or a URL (or --list / --stop)")
    if args.density is not None and args.density <= 0:
        ap.error("--density must be more than 0")
    if args.opacity is not None and not 0 < args.opacity <= 1:
        ap.error("--opacity is more than 0, and at most 1")
    if args.every is not None and args.every <= 0:
        ap.error("--every must be more than 0")

    source = Source(args.source)
    every = args.every or (None if source.is_url else 1.0)
    lifetime = args.lifetime if args.lifetime is not None else (
        DETACHED_FOR if args.detach else 0)
    opacity = args.opacity or (BEHIND_OPACITY if args.behind else 1.0)
    # Behind the text, faded, half the pixels look the same.
    density = args.density or (1.0 if args.behind else 2.0)

    path = args.tty or find_tty()
    if not path:
        sys.exit("picture: can't find a terminal to draw on; pass --tty "
                 "(run `tty` in the terminal you want)")
    try:
        tty = Tty(path)
    except OSError as e:
        sys.exit(f"picture: can't open {path}: {e.strerror}")
    try:
        picture = source.load()  # fail now, while someone is around to read why
    except RuntimeError as e:
        sys.exit(f"picture: {args.source}: {e}")

    if args.detach:
        pid = detach(path, lifetime)
        print(f"picture: showing {source.label()!r} at the {args.at} of {path} "
              f"{'for ' + human(lifetime) if lifetime else 'until stopped'} (id {pid})")
        print(f"picture: stop it with: {os.path.basename(__file__)} --stop {pid}")
        return

    # One picture per place on a terminal: this one replaces whatever is there, but only
    # now that its first frame is ready, so the gap is as short as can be.
    panel = Panel(source, args.size, args.at, density, opacity, args.behind)
    panel.first_frame(picture, tty.size())
    stop_pictures([p for p in running() if p["tty"] == path and p["at"] == args.at])

    # With `stty tostop`, a background job writing to its terminal gets SIGTTOU and
    # stops. If the signal is ignored, the kernel lets the write through instead.
    signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    stopping = []
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, lambda *_: stopping.append(True))

    stop = threading.Event()
    registration = register(path, args.at, source.label(),
                            time.time() + lifetime if lifetime else None)
    panel.start(stop, every)
    deadline = time.monotonic() + lifetime if lifetime else math.inf
    try:
        while not stopping and time.monotonic() < deadline:
            tty.send(panel.tick(*tty.size()))
            time.sleep(REPIN_EVERY)
        tty.send(panel.forget())  # remove our images on the way out
    except OSError as e:
        if e.errno not in (errno.EIO, errno.ENXIO, errno.ENOTTY):
            raise
        # The terminal went away (macOS says ENOTTY once the shell that owned it has
        # exited), so there's nothing left to clean up.
    finally:
        stop.set()
        panel.close()  # pictures the terminal never took
        try:
            os.remove(registration)
        except OSError:
            pass


if __name__ == "__main__":
    main()
