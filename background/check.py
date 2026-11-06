#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["numpy", "Pillow>=9.1"]
# ///
"""Quick check that background.py behaves. It runs against a fake terminal, so
nothing is drawn on your screen.

    ./check.py        (or: uv run check.py)

For each mode it checks that every command is quiet (q=2, so nothing gets typed
at your prompt), that the cursor is always put back, and that the image is
deleted on exit. It also checks that a picture is re-sent after a resize, and
that the script exits quietly when its terminal closes.
"""

import fcntl
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
import time
from pathlib import Path

from PIL import Image

SCRIPT = Path(__file__).resolve().parent / "background.py"
SIZE = (34, 134, 2152, 1184)    # rows, cols, width px, height px
BIGGER = (40, 160, 2560, 1400)


def set_size(fd, size):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", *size))


def start(*args):
    master, slave = pty.openpty()
    set_size(slave, SIZE)
    proc = subprocess.Popen([sys.executable, str(SCRIPT), "--tty", os.ttyname(slave), *args],
                            stdin=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return master, slave, proc


def read_for(master, seconds, then=None):
    """Collect output for a while; optionally call then() half-way through."""
    buf, end = b"", time.monotonic() + seconds
    halfway = time.monotonic() + seconds / 2
    while time.monotonic() < end:
        if then and time.monotonic() > halfway:
            then()
            then = None
        if select.select([master], [], [], 0.05)[0]:
            try:
                buf += os.read(master, 1 << 20)
            except OSError:
                break
    return buf


def stop(master, slave, proc):
    """SIGTERM it, and keep reading meanwhile: a terminal that stops reading would
    leave the script stuck partway through a frame, like a real one would."""
    proc.send_signal(signal.SIGTERM)
    tail, deadline = b"", time.monotonic() + 5
    while proc.poll() is None and time.monotonic() < deadline:
        tail += read_for(master, 0.1)
    if proc.poll() is None:
        proc.kill()
        proc.wait()
    tail += read_for(master, 0.3)
    os.close(master)
    os.close(slave)
    return tail


def check_mode(label, *args, resize=False):
    master, slave, proc = start(*args)
    seconds = 2.5
    buf = read_for(master, seconds, then=(lambda: set_size(slave, BIGGER)) if resize else None)
    buf += stop(master, slave, proc)
    err = proc.stderr.read().decode(errors="replace").strip()

    keys = re.findall(rb"\x1b_G([^;\x1b]*)", buf)
    commands = [k for k in keys if not k.startswith(b"m=")]  # skip continuation chunks
    problems = []
    if proc.returncode != 0:
        problems.append(f"exit code {proc.returncode}: {err[-300:]}")
    if not commands:
        problems.append("sent nothing")
    if not all(b"q=2" in k for k in keys):
        problems.append("a command is missing q=2, so the terminal would reply at the prompt")
    if buf.count(b"\x1b7") != buf.count(b"\x1b8"):
        problems.append("cursor saves and restores don't pair up")
    if not (commands and re.fullmatch(rb"a=d,d=I,i=\d+,q=2", commands[-1])):
        problems.append("the image wasn't deleted at exit")
    if resize and sum(k.startswith(b"a=t") for k in commands) < 2:
        problems.append("the picture wasn't re-sent after a resize")
    report(label, problems, f"{len(buf) / seconds / 1024:.0f} KB/s")
    return not problems


def check_terminal_closes():
    master, slave, proc = start()
    read_for(master, 1.0)
    os.close(master)
    os.close(slave)
    proc.send_signal(signal.SIGHUP)  # what the shell sends its jobs when the window closes
    problems = []
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        problems.append("still running after its terminal closed")
    err = proc.stderr.read().decode(errors="replace").strip()
    if proc.returncode != 0 or err:
        problems.append(f"didn't exit quietly (code {proc.returncode}): {err[-300:]}")
    report("terminal closes", problems)
    return not problems


def report(label, problems, note=""):
    print(f"{'ok  ' if not problems else 'FAIL'}  {label:<26} {note}")
    for p in problems:
        print(f"        {p}")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        picture = Path(tmp) / "gradient.png"  # a generated stand-in for a real photo
        x = Image.linear_gradient("L").resize((900, 600))
        Image.merge("RGB", (x, x.rotate(90).resize((900, 600)), x.transpose(0))).save(picture)

        results = [
            check_mode("plasma"),
            check_mode("plasma --color teal", "--color", "teal", "--opacity", "0.5"),
            check_mode("plasma --color ff8800", "--color", "ff8800"),
            check_mode("--image, with a resize", "--image", str(picture), resize=True),
            check_mode("--still, with a resize", "--still", "--seed", "7", resize=True),
            check_mode("--still --color teal", "--still", "--color", "teal"),
            check_terminal_closes(),
        ]
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
