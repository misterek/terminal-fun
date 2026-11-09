#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["Pillow>=9.1"]
# ///
"""Quick check that picture.py behaves. It runs against fake terminals, with its own
private list of what's showing, so nothing is drawn on your screen and pictures you
have up are left alone.

    ./check.py        (or: uv run check.py)

It checks that every command is quiet (q=2, so nothing gets typed at your prompt),
that the cursor is always put back, and that the image is deleted on exit; that a
picture keeps its shape, and is sent again when its file changes; that an animated GIF
plays; that a URL works; and that --detach, --list and --stop work, and --detach fails
on a file it can't read.
"""

import fcntl
import functools
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image

SCRIPT = Path(__file__).resolve().parent / "picture.py"
SIZE = (34, 134, 2152, 1184)  # rows, cols, width px, height px: cells of about 16 x 35


def fake_terminal():
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", *SIZE))
    return master, slave


def picture(*args):
    """Run picture.py to completion (for --detach, --list, --stop)."""
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True,
                          text=True, timeout=30, stdin=subprocess.DEVNULL)


def read_for(master, seconds):
    buf, end = b"", time.monotonic() + seconds
    while time.monotonic() < end:
        if select.select([master], [], [], 0.05)[0]:
            try:
                buf += os.read(master, 1 << 20)
            except OSError:
                break
    return buf


class Reader:
    """Reads a fake terminal nonstop in the background, as a real one would."""

    def __init__(self, master):
        self.master, self.buf, self.done = master, b"", threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while not self.done.is_set():
            self.buf += read_for(self.master, 0.1)

    def stop(self):
        self.done.set()
        self.thread.join()
        return self.buf


def problems_in(buf):
    """What's wrong with a stream of output from picture.py."""
    keys = re.findall(rb"\x1b_G([^;\x1b]*)", buf)
    commands = [k for k in keys if not k.startswith(b"m=")]  # skip continuation chunks
    found = []
    if not any(k.startswith(b"a=T,f=100") for k in commands):
        found.append("no picture was sent")
    if not all(b"q=2" in k for k in keys):
        found.append("a command is missing q=2, so the terminal would reply at the prompt")
    if buf.count(b"\x1b7") != buf.count(b"\x1b8"):
        found.append("cursor saves and restores don't pair up")
    if not (commands and re.fullmatch(rb"a=d,d=I,i=\d+,q=2", commands[-1])):
        found.append("the image wasn't deleted at exit")
    return found


def check_live(label, *args, want=(), test=None, meanwhile=None, seconds=2.5):
    """Run it on a fake terminal for a few seconds; meanwhile() runs halfway through, and
    test(output) returns what else is wrong."""
    master, slave = fake_terminal()
    proc = subprocess.Popen([sys.executable, str(SCRIPT), "--tty", os.ttyname(slave), *args],
                            stdin=subprocess.DEVNULL, stderr=subprocess.PIPE)
    buf = read_for(master, seconds / 2)
    if meanwhile:
        meanwhile()
    buf += read_for(master, seconds / 2)
    proc.send_signal(signal.SIGTERM)
    deadline = time.monotonic() + 5
    while proc.poll() is None and time.monotonic() < deadline:
        buf += read_for(master, 0.1)  # keep reading, as a real terminal would
    if proc.poll() is None:
        proc.kill()
        proc.wait()
    buf += read_for(master, 0.3)
    os.close(master)
    os.close(slave)
    err = proc.stderr.read().decode(errors="replace").strip()
    found = problems_in(buf)
    for w in want:
        if not re.search(w, buf):
            found.append(f"never sent {w!r}")
    if test:
        found += test(buf)
    if proc.returncode != 0 or err:
        found.append(f"exit code {proc.returncode}: {err[-300:]}")
    return report(label, found)


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass


def played(buf):
    """A three-frame animation: each frame sent (once is enough for a few seconds), one
    swapped for the next, and all of them deleted at the end."""
    sent = set(re.findall(rb"a=T,f=100,i=(\d+),", buf))
    found = [] if len(sent) == 3 else [f"sent {len(sent)} different frames, not 3"]
    if not re.search(rb"a=d,d=i,i=\d+,p=1,", buf):
        found.append("never swapped one frame for the next")
    if set(re.findall(rb"a=d,d=I,i=(\d+),", buf)) != sent:
        found.append("didn't delete every frame at the end")
    return found


def check_url(tmp):
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=tmp))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        return check_live("a URL", f"http://127.0.0.1:{server.server_address[1]}/wide.png")
    finally:
        server.shutdown()


def check_detach(tmp):
    master, slave = fake_terminal()
    reader = Reader(master)
    found = []
    r = picture(str(Path(tmp) / "wide.png"), "--detach", "--tty", os.ttyname(slave))
    pid = re.search(r"\(id (\d+)\)", r.stdout)
    pid = pid and pid.group(1)
    if r.returncode != 0 or not pid:
        found.append(f"didn't start (code {r.returncode}): {(r.stdout + r.stderr)[-300:]}")
    time.sleep(1)
    listed = picture("--list").stdout
    if pid and pid not in listed:
        found.append(f"--list doesn't show it: {listed!r}")
    stopped = picture("--stop", pid or "all")
    if stopped.returncode != 0:
        found.append(f"--stop failed: {(stopped.stdout + stopped.stderr)[-300:]}")
    time.sleep(0.3)
    found += problems_in(reader.stop())
    if "nothing showing" not in picture("--list").stdout:
        found.append("still listed after --stop")

    r = picture(str(Path(tmp) / "missing.png"), "--detach", "--tty", os.ttyname(slave))
    if r.returncode == 0 or "missing.png" not in r.stderr:
        found.append(f"a missing file: code {r.returncode}, {r.stderr.strip()!r}")
    os.close(master)
    os.close(slave)
    return report("--detach, --list, --stop", found)


def report(label, problems):
    print(f"{'ok  ' if not problems else 'FAIL'}  {label}")
    for p in problems:
        print(f"        {p}")
    return not problems


def main():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_RUNTIME_DIR"] = tmp  # a private list of what's showing
        for k in ("SSH_CONNECTION", "SSH_TTY"):  # as if the terminal were this machine's
            os.environ.pop(k, None)
        wide, tall = Path(tmp) / "wide.png", Path(tmp) / "tall.png"
        Image.new("RGB", (400, 300), (200, 60, 40)).save(wide)
        Image.new("RGB", (300, 400), (40, 90, 200)).save(tall)
        gif = Path(tmp) / "blink.gif"
        red, green, blue = (Image.new("RGB", (60, 40), c)
                            for c in ((200, 0, 0), (0, 200, 0), (0, 0, 200)))
        red.save(gif, save_all=True, append_images=[green, blue], duration=100, loop=0)
        results = [
            # In the default 60x20 cells (of about 16 x 35 px), 400x300 is 929x697 px,
            # so 58x20 cells. Written over with 300x400, it's 522x697 px: 33x20 cells.
            # Sent in a file, with the hidden placement that keeps it through `clear`.
            check_live("keeps its shape, follows the file", str(tall),
                       meanwhile=lambda: Image.open(wide).save(tall),
                       want=(rb"a=T,f=100,i=\d+,p=1,c=33,r=20,z=10,C=1,q=2,t=t;",
                             rb"a=T,f=100,i=\d+,p=1,c=58,r=20,z=10,C=1,q=2,t=t;",
                             rb"a=p,U=1,i=\d+,p=2,")),
            check_live("an animated GIF plays", str(gif), test=played),
            check_url(tmp),
            check_detach(tmp),
        ]
        picture("--stop", "all")  # in case a failed check left one behind
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
