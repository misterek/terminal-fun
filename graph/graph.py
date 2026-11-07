#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["numpy", "Pillow>=10.1"]
# ///
r"""Pop a live line graph over a corner of your terminal, from a background job.

  ./graph.py --cmd "ping -c1 1.1.1.1 | sed -n 's/.*time=\([0-9.]*\).*/\1/p'" --title Ping --unit ms &
  ./graph.py --promql node_load1 --title "Load average" &
  producer | ./graph.py --stdin --title "Queue depth" &
  ./graph.py ... --at bottom-left --size 50x12   where it goes, and how big (in cells)
  ./graph.py ... --for 10m                       go away by itself after ten minutes
  ./graph.py ... --snapshot graph.png            draw one frame to a file instead
  kill %1                                        stop it; it removes its image on the way out

For agents (a Claude, say) and scripts, which run commands without a terminal of their own:
  ./graph.py ... --detach   check the source once, start drawing in the background, print
                            the graph's id and return. It finds the terminal through the
                            processes that started it, and goes away after 30 minutes
                            unless --for says otherwise (--for 0: never).
  ./graph.py --list         what's showing, and where
  ./graph.py --stop ID      stop one: an id from --list, or a corner of this terminal;
                            --stop all stops every one

Sources (pick one):
  --promql Q   PromQL, with the whole window filled in from the start. Three ways in:
                 Grafana's API: --grafana URL or $GRAFANA_URL, with $GRAFANA_TOKEN (or
                   $GRAFANA_SERVICE_ACCOUNT_TOKEN).
                 A Grafana MCP server: --mcp or $GRAFANA_MCP_COMMAND. Either a command
                   that starts one (like "uvx mcp-grafana"), which gets the same
                   variables, or its URL, with $GRAFANA_MCP_TOKEN if it needs one.
                 Prometheus (VictoriaMetrics, ...) directly: --prom URL, with $PROM_TOKEN
                   if it needs one.
  --cmd C      any shell command, run every --every seconds. It prints one number,
               "label number" lines for several series, or Prometheus-style JSON (a range
               result fills in the whole window). It's told the window to cover in
               $GRAPH_START / $GRAPH_END (unix seconds), $GRAPH_START_ISO / $GRAPH_END_ISO,
               $GRAPH_STEP (seconds) and $GRAPH_WINDOW (like 15m).
  --stdin      numbers, or "label number" lines, as they come in on a pipe.

Or a table instead of a line (--table), sized to what's in it. A cell that changes
lights up for a few seconds, a row that goes away stays a moment, struck through, and
words like Running, Pending or CrashLoopBackOff are colored:
  ./graph.py --table --cmd "kubectl get pods" &       text in columns, as kubectl, docker
                                                      and ps print it, or JSON: a list of
                                                      objects, or one object per line
  ./graph.py --table --promql 'kube_pod_container_status_waiting_reason == 1' &
                                                      a row per series: its labels, and
                                                      its value
  --columns NAME,STATUS picks the columns, and their order.

Needs a terminal with the kitty graphics protocol: made for Ghostty, and kitty should
work too. It draws from a background job the same way ../background does: it never reads
from the terminal, every command is quiet (q=2), each frame is one write that saves and
restores the cursor, and the graph is re-pinned forty times a second, so scrolling or
`clear` only moves or hides it for a moment. Pictures go over in a temporary file the
terminal reads and deletes, so they're small writes; over SSH, where it can't see the
file, the pixels go in the command.
"""

import argparse
import base64
import errno
import fcntl
import io
import itertools
import json
import math
import os
import re
import shlex
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import traceback
from collections import deque
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
from PIL import Image, ImageDraw, ImageFont

IMAGE_ID = 0x47525048  # "GRPH", plus the corner's index: one graph per corner
# Where a graph can go: --at. "fill" is the whole window, for a graph behind the text.
CORNERS = ["top-right", "top-left", "bottom-right", "bottom-left", "center", "fill"]
CHUNK = 4096           # the protocol takes base64 payloads in chunks of at most 4096 bytes
DETACHED_FOR = 1800    # seconds a --detach graph lasts unless --for says otherwise
MCP_PROTOCOL = "2025-06-18"
REPIN_EVERY = 0.025    # seconds; how long the graph can ride along when the text scrolls

# The first font file that loads wins: DejaVu on Linux, Helvetica on macOS. Failing
# those, Pillow's built-in font, which has no bold.
REGULAR = [("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0),
           ("/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf", 0),
           ("/System/Library/Fonts/Helvetica.ttc", 0)]
BOLD = [("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 0),
        ("/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf", 0),
        ("/System/Library/Fonts/Helvetica.ttc", 1)]

# A dark chart palette.
SURFACE = (26, 26, 25)
PANEL_ALPHA = 238  # just shy of opaque: the text underneath is a faint ghost, not noise
BEHIND_OPACITY = 0.4  # --behind: faint enough that the text in front stays easy to read
BORDER = (255, 255, 255, 26)
INK = (240, 239, 236)
INK_2 = (195, 194, 183)
MUTED = (137, 135, 129)
GRID = (44, 44, 42)
AXIS = (56, 56, 53)
SERIES = [(0x39, 0x87, 0xE5), (0xD9, 0x59, 0x26), (0x19, 0x9E, 0x70), (0xC9, 0x85, 0x00),
          (0xD5, 0x51, 0x81), (0x00, 0x83, 0x00), (0x90, 0x85, 0xE9), (0xE6, 0x67, 0x67)]
GOOD, WARNING, CRITICAL = (0x0C, 0xA3, 0x0C), (0xFA, 0xB2, 0x19), (0xD0, 0x3B, 0x3B)

# Tables (--table).
HIGHLIGHT_FOR = 10.0  # seconds a changed cell, or a new row, stays lit up, fading
LINGER = 10.0         # seconds a row that went away stays, struck through
RESEND_EVERY = 10.0   # seconds; a picture that hasn't changed still goes out this often,
                      # so it comes back after `reset`, and shows up inside vim or less.
                      # Not more often: sending a whole picture again may blink.
CHANGE = (0x39, 0x87, 0xE5)  # what a change lights up in
OK_TEXT, WARN_TEXT, BAD_TEXT = (110, 200, 110), (240, 185, 60), (245, 105, 100)
# Words that color a cell in a column named like a status. The worst one in a cell wins.
BAD_WORDS = {"error", "failed", "failure", "crashloopbackoff", "oomkilled", "evicted",
             "imagepullbackoff", "errimagepull", "invalidimagename", "backoff",
             "createcontainererror", "createcontainerconfigerror", "runcontainererror",
             "unhealthy", "notready", "down", "dead", "lost"}
WARN_WORDS = {"pending", "containercreating", "podinitializing", "init", "terminating",
              "waiting", "starting", "restarting", "unknown", "degraded", "paused",
              "exited", "schedulingdisabled", "progressing", "suspended"}
GOOD_WORDS = {"running", "ready", "up", "healthy", "ok", "succeeded", "completed",
              "complete", "active", "bound", "available", "passing", "true"}
STATUS_COLUMN = re.compile(r"status|state|phase|health|ready|reason|condition", re.I)
# Durations, as kubectl ("3d", "5m12s") and docker ("2 hours", "About a minute") print
# them. Time passing isn't news: a cell that changed only in these doesn't light up.
DURATION = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?:ms|[smhdwy])(?:\d+[smhd])*(?!\w)"
                      r"|\b(?:\d+|an?)\s+(?:second|minute|hour|day|week|month|year)s?\b",
                      re.I)
# Right-aligned: 3, 64Mi, 12.5%, 0.46 cores, 120 req/s
NUMERIC = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?\s?[A-Za-z%/]{0,6}")


# --- kitty graphics protocol (the same as ../background) ---------------------

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
# Each graph keeps a small file in a private directory while it runs, so --list and
# --stop can find it, and so a new graph can replace an old one in the same corner of
# the same terminal (two would fight over the same image).

def state_dir():
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    path = os.path.join(base, f"terminal-fun-graph-{os.getuid()}")
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
    """The graphs that are showing now, oldest first. Clears away files left by graphs
    that were killed before they could remove their own."""
    out = []
    folder = state_dir()
    for name in os.listdir(folder):
        if not name.endswith(".json"):
            continue
        path = os.path.join(folder, name)
        try:
            with open(path) as f:
                g = json.load(f)
            alive = bool(g["lstart"]) and ps(g["pid"], "lstart=") == g["lstart"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if alive:
            out.append(g)
        else:
            try:
                os.remove(path)
            except OSError:
                pass
    return sorted(out, key=lambda g: g["started"])


def stop_graphs(graphs, wait=3.0):
    """SIGTERM them and give them a moment to remove their images. One that's stuck (in a
    write to a terminal that stopped reading, say) gets SIGKILL; its image stays until
    it scrolls away or the screen is cleared."""
    def alive():
        return [g for g in graphs if ps(g["pid"], "lstart=") == g["lstart"]]

    for sig in (signal.SIGTERM, signal.SIGKILL):
        for g in alive():
            try:
                os.kill(g["pid"], sig)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline and alive():
            time.sleep(0.05)
        if not alive():
            return
    for g in graphs:  # killed, so it couldn't remove its own file
        try:
            os.remove(os.path.join(state_dir(), f"{g['pid']}.json"))
        except OSError:
            pass


def human(seconds):
    s = max(0, round(seconds))
    if s < 90:
        return f"{s}s"
    if s < 90 * 60:
        return f"{round(s / 60)}m"
    return f"{s / 3600:.1f}h".replace(".0h", "h")


def list_graphs():
    graphs = running()
    if not graphs:
        print("graph: nothing showing")
        return
    now = time.time()
    for g in graphs:
        left = f", stops in {human(g['until'] - now)}" if g.get("until") else ""
        print(f"{g['pid']:>7}  {g['tty']}  {g['at']:<12}  "
              f"up {human(now - g['started'])}{left}  {g['title']}")


def stop_command(target):
    graphs = running()
    if target == "all":
        pick = graphs
    elif target in CORNERS:
        here = find_tty()
        pick = [g for g in graphs if g["at"] == target and g["tty"] == here]
    else:
        pick = [g for g in graphs if str(g["pid"]) == target]
    if not pick:
        sys.exit(f"graph: nothing to stop for {target!r} (see --list)")
    stop_graphs(pick)
    for g in pick:
        print(f"graph: stopped {g['pid']} ({g['title']})")


# --- data ----------------------------------------------------------------------

class Store:
    """Series shared between the fetch thread and the draw loop."""

    def __init__(self, window):
        self.window = window
        self.lock = threading.Lock()
        self.series = {}  # label -> deque[(t, v)]
        self.order = []   # labels in first-seen order: a series keeps its color for life
        self.error = None
        self.updated = None
        self.ended = False  # --stdin's pipe closed: no more data, but nothing broke

    def _note(self, labels):
        for label in labels:
            if label not in self.order:
                self.order.append(label)

    def replace(self, data):
        with self.lock:
            self._note(data)
            self.series = {k: deque(v) for k, v in data.items()}
            self.updated, self.error = time.time(), None

    def add(self, t, values):
        with self.lock:
            self._note(values)
            for label, v in values.items():
                self.series.setdefault(label, deque()).append((t, v))
            for pts in self.series.values():
                while pts and pts[0][0] < t - self.window:
                    pts.popleft()
            self.updated, self.error = t, None

    def fail(self, msg):
        with self.lock:
            self.error = msg

    def end(self):
        with self.lock:
            self.ended = True

    def load(self, path):
        """Start from what the --detach that started this copy already fetched, so the
        graph has its line from the first picture, not after a slow first fetch."""
        try:
            with open(path) as f:
                saved = json.load(f)
            os.remove(path)
            series = {k: deque((t, v) for t, v in pts) for k, pts in saved["series"].items()}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return
        with self.lock:
            self._note(saved.get("order") or list(series))
            self.series, self.updated = series, saved.get("updated")

    def save(self, path):
        series, order, _, updated, _ = self.snapshot()
        with open(path, "w") as f:
            json.dump({"series": series, "order": order, "updated": updated}, f)

    def snapshot(self):
        with self.lock:
            return ({k: list(v) for k, v in self.series.items()}, list(self.order),
                    self.error, self.updated, self.ended)


def parse_lines(text):
    """'4.2' -> {'': 4.2};  'api 4.2\\nweb 9' -> {'api': 4.2, 'web': 9.0}."""
    out = {}
    for line in text.splitlines():
        parts = line.strip().rsplit(None, 1)
        if not parts:
            continue
        try:
            v = float(parts[-1])
        except ValueError:
            continue
        if math.isfinite(v):
            out[parts[0] if len(parts) == 2 else ""] = v
    return out


# --- tables (--table) ------------------------------------------------------------

class TableStore:
    """A table's rows, shared between the fetch thread and the draw loop, and when each
    cell last changed. Rows are told apart by keys the source gives them."""

    def __init__(self):
        self.lock = threading.Lock()
        self.columns, self.rows, self.order = [], {}, []  # rows: key -> row
        self.error = self.updated = self.note = None

    def replace(self, columns, rows, keys, note=None):
        now = time.time()
        with self.lock:
            if not columns:  # nothing printed, so no header either: keep the one we had
                columns = self.columns
            # The first rows, or different columns, are a fresh start: nothing lights up.
            fresh = self.updated is None or bool(self.columns and columns != self.columns)
            old_rows = {} if columns != self.columns else self.rows
            new = {}
            for key, cells in zip(keys, rows):
                old = old_rows.get(key)
                if old is None or old["gone"]:
                    new[key] = {"cells": cells, "changed": [None] * len(cells),
                                "streak": [0] * len(cells), "added": None if fresh else now,
                                "gone": None}
                    continue
                changed, streak = list(old["changed"]), list(old["streak"])
                for i, (a, b) in enumerate(zip(old["cells"], cells)):
                    if DURATION.sub("~", a) == DURATION.sub("~", b):
                        streak[i] = 0
                    else:
                        # A cell that changes every time is a live number, not news.
                        streak[i] += 1
                        if streak[i] <= 2:
                            changed[i] = now
                new[key] = {**old, "cells": cells, "changed": changed, "streak": streak}
            # A row that went away stays a while, where it was.
            order, prev = list(new), None
            for key in self.order if old_rows else []:
                if key in new:
                    prev = key
                    continue
                gone = old_rows[key]["gone"] or now
                if now - gone < LINGER:
                    new[key] = {**old_rows[key], "gone": gone}
                    order.insert(order.index(prev) + 1 if prev is not None else 0, key)
                    prev = key
            self.columns, self.rows, self.order = list(columns), new, order
            self.updated, self.error, self.note = now, None, note

    def fail(self, msg):
        with self.lock:
            self.error = msg

    def snapshot(self):
        """(columns, rows, error, updated, note); rows in order, each a dict of its cells,
        when each changed, and when the row was added or went away."""
        now = time.time()
        with self.lock:
            rows = [self.rows[k] for k in self.order]
            rows = [r for r in rows if not (r["gone"] and now - r["gone"] >= LINGER)]
            return list(self.columns), rows, self.error, self.updated, self.note

    def save(self, path):
        with self.lock:
            keys = [k for k in self.order if not self.rows[k]["gone"]]
            saved = {"columns": self.columns, "keys": keys, "note": self.note,
                     "rows": [self.rows[k]["cells"] for k in keys]}
        with open(path, "w") as f:
            json.dump(saved, f)

    def load(self, path):
        """Start from what the --detach that started this copy already fetched."""
        try:
            with open(path) as f:
                saved = json.load(f)
            os.remove(path)
            self.replace(saved["columns"], saved["rows"], saved["keys"], saved.get("note"))
        except (OSError, ValueError, KeyError, TypeError):
            pass


# Color and other terminal escapes, in case a command prints them anyway.
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def parse_columns(text):
    """Text laid out in columns under a header line, the way kubectl, docker and ps print
    it -> (header, rows), every row as long as the header.

    Columns are cut where every line is blank, so a value can have spaces in it ("Up 2
    hours", "7 (40s ago)"). A stretch with nothing above it in the header belongs to the
    column before it: a value that runs past its header, or the free text in a last
    column. Tab-separated text is split on the tabs.
    """
    # Only spaces come off the end: a trailing tab is an empty last cell.
    lines = [ln.rstrip(" ") for ln in ANSI.sub("", text).splitlines()]
    lines = [ln for ln in lines if ln.strip()]
    if not lines:
        return [], []
    if all("\t" in ln for ln in lines) and len({ln.count("\t") for ln in lines}) == 1:
        cells = [[c.strip() for c in ln.split("\t")] for ln in lines]
        return cells[0], cells[1:]
    lines = [ln.expandtabs() for ln in lines]
    head, body = lines[0], lines[1:]
    width = max(map(len, lines))
    blank = [all(p >= len(ln) or ln[p] == " " for ln in lines) for p in range(width)]
    if body and not body[0].strip("-= ") and len(body[0]) >= len(head.rstrip()):
        body = body[1:]  # a rule under the header (pip list), not a row

    spans, start = [], None  # runs of positions where some line has something
    for p, b in enumerate(blank + [True]):
        if not b and start is None:
            start = p
        elif b and start is not None:
            spans.append([start, p])
            start = None

    def cut(ln, s, e):
        return ln[s:e].strip()

    merged = []
    for s, e in spans:
        if merged and not cut(head, s, e):
            merged[-1][1] = e  # nothing in the header here: the column before runs on
        else:
            merged.append([s, e])

    def empty(s, e):
        return not any(cut(ln, s, e) for ln in body)

    # Two header words one space apart, one of them with nothing under it, are one name:
    # kubectl's "NOMINATED NODE" over "<none>", docker's right-aligned "CONTENT SIZE".
    out = merged[:1]
    for s, e in merged[1:]:
        if s - out[-1][1] == 1 and (empty(s, e) or empty(*out[-1])):
            out[-1][1] = e
        else:
            out.append([s, e])
    return [cut(head, s, e) for s, e in out], [[cut(ln, s, e) for s, e in out] for ln in body]


def flatten(obj, prefix=""):
    """{"metadata": {"name": "x"}} -> {"metadata.name": "x"}"""
    out = {}
    for k, v in obj.items():
        if isinstance(v, dict) and v:
            out.update(flatten(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out


def cell_text(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return fmt(v)
    if isinstance(v, list) and all(not isinstance(x, (list, dict)) for x in v):
        return ", ".join(map(cell_text, v))
    if isinstance(v, (list, dict)):
        return json.dumps(v, separators=(",", ":"))
    return str(v)


def json_table(text, unit=""):
    """A command's JSON -> (columns, rows, keys or None). It takes a list of objects (or
    kubectl's {"items": [...]}), one object per line (docker --format json), or a
    Prometheus-style query result. Nested objects become columns like metadata.name."""
    try:
        docs = [json.loads(text)]
    except ValueError:
        try:
            docs = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
        except ValueError as e:
            raise RuntimeError(f"bad JSON: {e}"[:300]) from None
    if len(docs) == 1:
        result = prom_result(docs[0])
        if result is not None:
            return prom_table(result, unit)
        docs = docs[0]
        if isinstance(docs, dict):
            docs = docs["items"] if isinstance(docs.get("items"), list) else [docs]
    if not isinstance(docs, list) or not all(isinstance(d, dict) for d in docs):
        raise RuntimeError("for a table, the JSON is a list of objects, one per row")
    docs = [flatten(d) for d in docs]
    columns = list(dict.fromkeys(k for d in docs for k in d))
    return columns, [[cell_text(d.get(k)) for k in columns] for d in docs], None


def prom_result(doc):
    """The series in Prometheus-style JSON (a whole API response, its "data", or just the
    result list), or None if doc isn't that."""
    if isinstance(doc, dict) and isinstance(doc.get("data"), dict):
        doc = doc["data"]
    if isinstance(doc, dict) and "resultType" in doc:
        if doc["resultType"] in ("scalar", "string"):
            return [{"metric": {}, "value": doc.get("result")}]
        return doc.get("result") if isinstance(doc.get("result"), list) else []
    if isinstance(doc, list) and doc and all(
            isinstance(s, dict) and "metric" in s and ("value" in s or "values" in s)
            for s in doc):
        return doc
    return None


def prom_table(result, unit=""):
    """Series -> (columns, rows, keys): a row per series, a column for each label that
    tells them apart (every label, for one series), and the value, unless it's the same
    in every row, as with `== 1`."""
    if not result:
        return [], [], []
    metrics = [s.get("metric") or {} for s in result]
    labels = list(dict.fromkeys(k for m in metrics for k in m))
    if len(result) > 1:
        labels = [k for k in labels if len({m.get(k) for m in metrics}) > 1]

    def value(s):
        pts = s.get("values") or [s.get("value")]
        try:
            return fmt(float(pts[-1][1])) + (f" {unit}" if unit else "")
        except (IndexError, TypeError, ValueError):
            return ""

    values = [value(s) for s in result]
    show = len(result) < 2 or len(set(values)) > 1
    columns = ["metric" if k == "__name__" else k for k in labels] + (["value"] if show else [])
    rows = [[str(m.get(k, "")) for k in labels] + ([v] if show else [])
            for m, v in zip(metrics, values)]
    return columns, rows, [json.dumps(m, sort_keys=True) for m in metrics]


def pick_columns(columns, rows, wanted):
    """Just the columns asked for, in that order; names match whatever their case."""
    if not columns:
        return columns, rows
    lower = [c.lower() for c in columns]
    for w in wanted:
        if w.lower() not in lower:
            raise RuntimeError(f"no column {w!r}; there's {', '.join(columns)}"[:300])
    idx = [lower.index(w.lower()) for w in wanted]
    return [columns[i] for i in idx], [[r[i] for i in idx] for r in rows]


def key_width(rows):
    """How many leading columns it takes to tell rows apart: usually one (NAME), two for
    kubectl -A (NAMESPACE, NAME)."""
    width = max(map(len, rows), default=1)
    return next((n for n in range(1, width + 1)
                 if len({tuple(r[:n]) for r in rows}) == len(rows)), width)


class Rows:
    """Hands a source's table to the store: the columns asked for (--columns), and a key
    for each row, so a row that changed can be told from one that's new."""

    def __init__(self, columns=None, unit=""):
        self.columns, self.unit, self.width = columns, unit, None

    def put(self, store, columns, rows, keys=None, note=None):
        if keys is None:
            if self.width is None and rows:
                self.width = key_width(rows)  # once, so the keys don't change under us
            seen, keys = {}, []
            for r in rows:
                k = "\x1f".join(r[:self.width or 1])
                seen[k] = seen.get(k, 0) + 1
                keys.append(f"{k}\x1e{seen[k]}")  # the 2nd of two rows that look the same
        if self.columns:
            columns, rows = pick_columns(columns, rows, self.columns)
        store.replace(columns, rows, keys, note)


def http_json(url, token=None, timeout=10):
    req = Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except HTTPError as e:
        detail = ""
        try:
            detail = json.load(e).get("error") or ""
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code} {detail}".strip()) from None
    except URLError as e:
        raise RuntimeError(str(e.reason)) from None


def series_names(metrics, legend=None):
    if legend:
        return [re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: mt.get(m.group(1), ""), legend)
                for mt in metrics]
    if len(metrics) <= 1:
        return [""] * len(metrics)
    # Name each series by just the labels that tell them apart.
    keys = sorted(k for k in set().union(*metrics) if len({m.get(k) for m in metrics}) > 1)
    if len(keys) == 1:
        return [m.get(keys[0], "") for m in metrics]
    return [", ".join(f"{k}={m[k]}" for k in keys if k in m) for m in metrics]


def prom_series(doc, legend=None):
    """Prometheus-shaped JSON -> (is_range, {name: [(t, v), ...]}).

    Takes a whole API response ({"data": {"result": [...]}}) or just the result list.
    Range results have "values" per series; instant ones have a single "value".
    """
    if isinstance(doc, dict):
        doc = doc.get("data", doc)
    if isinstance(doc, dict):
        doc = doc.get("result")
    if doc is None:
        doc = []
    if not isinstance(doc, list) or not all(
            isinstance(s, dict) and ("values" in s or "value" in s) for s in doc):
        raise ValueError("not Prometheus-style JSON (want a list of {metric, values})")
    out = {}
    for name, s in zip(series_names([s.get("metric", {}) for s in doc], legend), doc):
        pts = [(float(t), float(v)) for t, v in (s.get("values") or [s["value"]])]
        out[name] = [p for p in pts if math.isfinite(p[1])]
    return not doc or any("values" in s for s in doc), out


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def default_prometheus(sources):
    """The uid of Grafana's default Prometheus-type datasource (or its first one)."""
    if isinstance(sources, dict):  # some versions wrap the list
        sources = next((v for v in sources.values() if isinstance(v, list)), [])
    proms = [s for s in sources or [] if isinstance(s, dict) and s.get("type") == "prometheus"]
    if not proms:
        raise RuntimeError("no Prometheus datasource in Grafana; pass --ds UID")
    return next((s for s in proms if s.get("isDefault")), proms[0])["uid"]


def grafana_base(url, token, ds):
    """Grafana's proxy to a Prometheus-type datasource: --ds, or its default one."""
    url = url.rstrip("/")
    ds = ds or default_prometheus(http_json(f"{url}/api/datasources", token))
    return f"{url}/api/datasources/proxy/uid/{ds}"


class PromSource:
    """PromQL over HTTP: through Grafana's datasource proxy, or straight at Prometheus."""

    def __init__(self, base, token, query, window, step, legend, table=None):
        self.base, self.token, self.query = base.rstrip("/"), token, query
        self.window, self.step, self.legend = window, step, legend
        self.table = table  # a Rows, for --table: then it's an instant query

    def __call__(self, store):
        if self.table:
            body = http_json(f"{self.base}/api/v1/query?{urlencode({'query': self.query})}",
                             self.token)
            if body.get("status") != "success":
                raise RuntimeError(body.get("error", "query failed"))
            return self.table.put(store, *prom_table(prom_result(body) or [], self.table.unit))
        end = time.time()
        params = urlencode({"query": self.query, "start": f"{end - self.window:.0f}",
                            "end": f"{end:.0f}", "step": f"{self.step:.0f}"})
        body = http_json(f"{self.base}/api/v1/query_range?{params}", self.token)
        if body.get("status") != "success":
            raise RuntimeError(body.get("error", "query failed"))
        store.replace(prom_series(body, self.legend)[1])


class McpClient:
    """Just enough MCP to call a server's tools: over its stdin and stdout when it's a
    command we start, or over HTTP when it's a URL.

    A command gets our environment, so the usual GRAFANA_URL and token variables reach
    it. A URL gets $GRAFANA_MCP_TOKEN as a bearer token, if that's set. The server (or
    session) is started once and kept for every call after; only after something goes
    wrong does the next call start a fresh one.
    """

    timeout = 30          # seconds for a tool call to answer
    start_timeout = 120   # seconds to start the server: uvx, npx and docker can be slow,
                          # the first time especially

    def __init__(self, target):
        self.url = target if re.match(r"https?://", target) else None
        self.argv = None if self.url else shlex.split(target)
        self.token = os.environ.get("GRAFANA_MCP_TOKEN")
        self.proc = None     # a command: the server we started
        self.session = None  # a URL: the session id the server gave us
        self.ready = False
        self.ids = 0

    def call(self, tool, args):
        """Call a tool and return what it said, decoded from JSON."""
        proc = self.proc
        if not self.ready or (self.argv and proc.poll() is not None):
            proc = self._within(self.start_timeout, self._connect)
        result = self._within(self.timeout, self._request, proc, "tools/call",
                              {"name": tool, "arguments": args})
        text = "".join(c.get("text", "") for c in result.get("content", [])
                       if c.get("type") == "text")
        if result.get("isError"):
            raise RuntimeError(text[:400] or f"{tool} failed")
        if result.get("structuredContent") is not None:
            return result["structuredContent"]
        try:
            return json.loads(text)
        except ValueError:
            raise RuntimeError(f"{tool} didn't return JSON: {text[:200]}") from None

    def _within(self, seconds, fn, *args):
        """fn(*args), but a server that hasn't answered in `seconds` is killed, which ends
        the wait with an error. After any error, the next call starts a fresh server."""
        expired = []
        timer = threading.Timer(seconds, lambda: (expired.append(True), self.close()))
        timer.start()
        try:
            return fn(*args)
        except (OSError, ValueError, RuntimeError) as e:
            self.close()
            if expired:
                raise RuntimeError(f"no answer from the MCP server in {seconds}s") from None
            raise RuntimeError(str(e)) from None
        finally:
            timer.cancel()

    def close(self):
        self.ready, self.session = False, None
        proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            proc.kill()
            proc.wait()

    def _connect(self):
        proc = None
        if self.argv:
            try:
                proc = subprocess.Popen(self.argv, text=True, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            except OSError as e:
                raise RuntimeError(f"can't start the MCP server {self.argv[0]!r}: "
                                   f"{e.strerror}") from None
            self.proc = proc
        self._request(proc, "initialize", {
            "protocolVersion": MCP_PROTOCOL, "capabilities": {},
            "clientInfo": {"name": "graph.py", "version": "1"}})
        self._send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.ready = True
        return proc

    def _request(self, proc, method, params):
        self.ids += 1
        reply = self._send(proc, {"jsonrpc": "2.0", "id": self.ids, "method": method,
                                  "params": params})
        if "error" in reply:
            raise RuntimeError(reply["error"].get("message", "MCP error"))
        return reply.get("result") or {}

    def _send(self, proc, msg):
        """Send one message; for a request, wait for and return its reply."""
        return self._http(msg) if self.url else self._stdio(proc, msg)

    def _stdio(self, proc, msg):
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        while "id" in msg:
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError("the MCP server exited")
            try:
                reply = json.loads(line)
            except ValueError:
                continue  # not for us: a server that logs to stdout
            if isinstance(reply, dict) and reply.get("id") == msg["id"]:  # skip notifications
                return reply
        return None

    def _http(self, msg):
        # MCP's "streamable HTTP": every message is a POST, and the reply comes back
        # either as plain JSON or as a short stream of server-sent events.
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        if self.ready:
            headers["MCP-Protocol-Version"] = MCP_PROTOCOL
        req = Request(self.url, data=json.dumps(msg).encode(), headers=headers, method="POST")
        try:
            resp = urlopen(req, timeout=self.timeout)
        except HTTPError as e:
            hint = " (is GRAFANA_MCP_TOKEN set?)" if e.code in (401, 403) else ""
            raise RuntimeError(f"HTTP {e.code} from the MCP server{hint}") from None
        except URLError as e:
            raise RuntimeError(str(e.reason)) from None
        with resp:
            self.session = resp.headers.get("Mcp-Session-Id") or self.session
            if "id" not in msg:
                return None
            if resp.headers.get_content_type() != "text/event-stream":
                return json.load(resp)
            data = []
            for raw in itertools.chain(resp, [b""]):  # b"" ends an event the stream didn't
                line = raw.decode("utf-8").rstrip("\r\n")
                if line.startswith("data:"):
                    data.append(line[5:][1:] if line[5:6] == " " else line[5:])
                elif not line and data:
                    reply, data = json.loads("\n".join(data)), []
                    if isinstance(reply, dict) and reply.get("id") == msg["id"]:
                        return reply
            raise RuntimeError("the MCP server hung up without answering")


class McpSource:
    """PromQL through a Grafana MCP server: its list_datasources and query_prometheus
    tools (as in grafana/mcp-grafana), for when that's how you reach Grafana."""

    def __init__(self, command, ds, query, window, step, legend, table=None):
        self.client, self.ds, self.query = McpClient(command), ds, query
        self.window, self.step, self.legend = window, step, legend
        self.table = table  # a Rows, for --table: then it's an instant query

    def __call__(self, store):
        if not self.ds:
            self.ds = default_prometheus(self.client.call("list_datasources",
                                                          {"type": "prometheus"}))
        end = time.time()
        if self.table:
            doc = self.client.call("query_prometheus", {
                "datasourceUid": self.ds, "expr": self.query, "queryType": "instant",
                "startTime": iso(end), "endTime": iso(end), "stepSeconds": int(self.step)})
            result = [] if doc in ([], None) else prom_result(doc)
            if result is None:
                raise RuntimeError("query_prometheus didn't answer with Prometheus-style series")
            return self.table.put(store, *prom_table(result, self.table.unit))
        doc = self.client.call("query_prometheus", {
            "datasourceUid": self.ds, "expr": self.query, "queryType": "range",
            "startTime": iso(end - self.window), "endTime": iso(end),
            "stepSeconds": int(self.step)})
        store.replace(prom_series(doc, self.legend)[1])

    def close(self):
        self.client.close()


class CmdSource:
    """Runs a shell command each poll. It may print plain numbers ("4.2", or "label 4.2"
    per series), or Prometheus-style JSON; a range result fills the whole window."""

    def __init__(self, cmd, timeout, window, step, legend, table=None):
        self.cmd, self.timeout = cmd, timeout
        self.window, self.step, self.legend = window, step, legend
        self.table = table  # a Rows, for --table

    def __call__(self, store):
        p = self.run()
        if self.table:
            return self.put_table(store, p)
        out = p.stdout.strip()
        if out[:1] in ("[", "{"):
            try:
                is_range, data = prom_series(json.loads(out), self.legend)
            except ValueError as e:
                raise RuntimeError(f"bad JSON: {e}"[:300]) from None
            if is_range:
                store.replace(data)
            else:
                store.add(time.time(), {k: v[-1][1] for k, v in data.items() if v})
            return
        values = parse_lines(out)
        if not values:
            last = (p.stderr.strip().splitlines() or [f"exit {p.returncode}, no number"])[-1]
            raise RuntimeError(last[:300])
        store.add(time.time(), values)

    def put_table(self, store, p):
        out = p.stdout.strip()
        said = (p.stderr.strip().splitlines() or [""])[-1][:300]
        if not out:
            # Nothing printed. That's an error if it said why; otherwise there are just
            # no rows (grep finding nothing exits 1 quietly; kubectl says "No resources
            # found" and exits 0).
            if p.returncode and said:
                raise RuntimeError(said)
            return self.table.put(store, [], [], note=said or None)
        if out[:1] in ("[", "{"):
            columns, rows, keys = json_table(out, self.table.unit)
        else:
            (columns, rows), keys = parse_columns(out), None
        self.table.put(store, columns, rows, keys, note=None if rows else said or None)

    def run(self):
        end = time.time()
        env = {**os.environ, "GRAPH_START": f"{end - self.window:.0f}", "GRAPH_END": f"{end:.0f}",
               "GRAPH_START_ISO": iso(end - self.window), "GRAPH_END_ISO": iso(end),
               "GRAPH_STEP": f"{self.step:.0f}", "GRAPH_WINDOW": duration(self.window)}
        # stdin=DEVNULL: a child that touched the tty would get the job stopped (SIGTTIN).
        try:
            return subprocess.run(self.cmd, shell=True, stdin=subprocess.DEVNULL, env=env,
                                  capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"no answer in {self.timeout:.0f}s") from None


def poll(source, store, every, stop):
    while not stop.is_set():
        try:
            source(store)
        except Exception as e:  # keep drawing; the panel shows what went wrong
            store.fail(str(e) or type(e).__name__)
        stop.wait(every)


def read_stdin(store, stop, mark_end=True):
    for line in sys.stdin:
        if stop.is_set():
            return
        values = parse_lines(line)
        if values:
            store.add(time.time(), values)
    if mark_end:
        store.end()


# --- drawing -------------------------------------------------------------------

_fonts = {}


def font(px, bold=False):
    key = (round(px), bold)
    if key not in _fonts:
        for path, index in BOLD if bold else REGULAR:
            try:
                _fonts[key] = ImageFont.truetype(path, key[0], index=index)
                break
            except OSError:
                pass
        else:
            _fonts[key] = ImageFont.load_default(size=key[0])
    return _fonts[key]


def fmt(v):
    """Three significant figures, compact past 10,000: 4, 12.5, 0.031, 12.9K."""
    if not math.isfinite(v):
        return "–"
    for div, suffix in ((1e9, "G"), (1e6, "M"), (1e4, "K")):
        if abs(v) >= div:
            return f"{v / div:.3g}{suffix}"
    return f"{v:,.0f}" if abs(v) >= 100 else f"{v:.3g}"


def wrap(d, text, fnt, width):
    """Greedy word wrap by rendered width; overlong words are cut, not overflowed."""
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if d.textlength(trial, font=fnt) <= width:
            cur = trial
            continue
        if cur:
            lines.append(cur)
        while d.textlength(word, font=fnt) > width:
            cut = max(1, int(len(word) * width / d.textlength(word, font=fnt)) - 1)
            lines.append(word[:cut])
            word = word[cut:]
        cur = word
    return lines + ([cur] if cur else [])


def nice_step(span, target):
    raw = span / max(target, 1)
    mag = 10 ** math.floor(math.log10(raw))
    return next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)


def y_range(values, ymin, ymax):
    lo, hi = min(values), max(values)
    if ymin is None:
        ymin = 0.0 if lo >= 0 else lo  # positive data sits on a zero baseline
    if ymax is None:
        ymax = hi if hi > ymin else ymin + 1
        step = nice_step(ymax - ymin, 4)
        ymax = math.ceil(ymax / step * 1.0001) * step  # always a little headroom
        if ymax <= hi:
            ymax += step
        if ymin != 0:
            ymin = math.floor(ymin / step) * step
    else:
        if ymax <= ymin:  # only --ymax given, at or under where the axis would start
            ymin = ymax - 1
        step = nice_step(ymax - ymin, 4)
    ticks, t = [], math.ceil(ymin / step) * step
    while t <= ymax + step * 1e-6:
        ticks.append(t)
        t += step
    return ymin, ymax, ticks, step


TIME_STEPS = [10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400]


def duration(s):
    s = int(s)
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n and s % n == 0:
            return f"{s // n}{unit}"
    return f"{s}s"


class Chart:
    def __init__(self, title, unit, window, ymin, ymax, every, note="", failed="query failed",
                 alpha=PANEL_ALPHA):
        self.title, self.unit, self.window = title, unit, window
        self.ymin, self.ymax, self.every, self.note = ymin, ymax, every, note
        self.failed = failed  # the status line when fetching goes wrong
        self.alpha = alpha    # 0-255, for the whole panel

    def render(self, w, h, ch, snap, now):
        """An RGBA panel w x h pixels; ch is a terminal cell's height in those pixels."""
        series, order, error, updated, ended = snap
        S = 2  # supersample: PIL's lines aren't antialiased, so draw big and shrink
        W, H, u = w * S, h * S, ch * S / 17  # u: one "CSS px" at this cell size
        img = Image.new("RGB", (W, H), SURFACE)
        d = ImageDraw.Draw(img, "RGBA")  # an RGBA brush on an RGB canvas blends
        pad = 0.9 * ch * S
        f_title, f_small = font(0.72 * ch * S, bold=True), font(0.56 * ch * S)
        f_hero, f_tick = font(1.25 * ch * S, bold=True), font(0.52 * ch * S)

        labels = [k for k in order if k in series][:len(SERIES)]
        colors = {k: SERIES[order.index(k) % len(SERIES)] for k in labels}
        single = len(labels) == 1
        latest = {k: series[k][-1][1] for k in labels if series[k]}
        unit = f" {self.unit}" if self.unit else ""

        # Header: title + status line on the left, the headline number on the right.
        y = pad * 0.8
        d.text((pad, y), self.title, font=f_title, fill=INK)
        y2 = y + f_title.size * 1.35
        if error:
            dot, status = CRITICAL, error
        elif ended:
            dot, status = MUTED, "input ended"
        elif updated is None:
            dot, status = MUTED, "loading…"
        else:
            age = max(0, now - updated)
            dot = GOOD if age < self.every * 3 + 5 else WARNING
            status = "live" if age < self.every + 3 else f"updated {human(age)} ago"
        r = f_small.size * 0.28
        cy = y2 + f_small.size * 0.62
        d.ellipse((pad, cy - r, pad + 2 * r, cy + r), fill=dot)
        line = self.failed if error else f"{status}  ·  last {duration(self.window)}"
        if self.note:
            line += f"  ·  via {self.note}"
        d.text((pad + 3.4 * r, y2), line, font=f_small, fill=INK_2 if error else MUTED)
        header_bottom = y2 + f_small.size * 1.6

        if single and labels[0] in latest:
            vals = [v for _, v in series[labels[0]]]
            hero = fmt(latest[labels[0]])
            hx, base = W - pad, y + f_hero.size * 0.78  # number and unit share a baseline
            if unit:
                d.text((hx, base), unit, font=f_small, fill=INK_2, anchor="rs")
                hx -= d.textlength(unit, font=f_small)
            d.text((hx, base), hero, font=f_hero, fill=INK, anchor="rs")
            stats = f"min {fmt(min(vals))}  avg {fmt(sum(vals) / len(vals))}  max {fmt(max(vals))}"
            d.text((W - pad - d.textlength(stats, font=f_small), y2), stats,
                   font=f_small, fill=MUTED)

        # Legend for 2+ series: swatch, name, latest value (identity never by color alone).
        if len(labels) > 1:
            lx, ly = pad, header_bottom
            for k in labels:
                name = k or "(no labels)"
                if len(name) > 28:
                    name = name[:27] + "…"
                val = f"{fmt(latest[k])}{unit}" if k in latest else "–"
                item_w = 2.8 * r + d.textlength(f"{name} {val}", font=f_small) + pad
                if lx + item_w > W - pad and lx > pad:
                    lx, ly = pad, ly + f_small.size * 1.5
                sy = ly + f_small.size * 0.62
                d.rounded_rectangle((lx, sy - u, lx + 2.2 * r + 4 * u, sy + u), radius=u,
                                    fill=colors[k])
                tx = lx + 2.2 * r + 4 * u + 0.5 * r
                d.text((tx, ly), name, font=f_small, fill=INK_2)
                d.text((tx + d.textlength(name + " ", font=f_small), ly), val,
                       font=f_small, fill=INK)
                lx += item_w
            header_bottom = ly + f_small.size * 1.7

        # Plot area.
        all_vals = [v for k in labels for _, v in series[k]]
        x_axis_h = f_tick.size * 1.6
        top, bottom = header_bottom + f_tick.size * 0.6, H - pad * 0.7 - x_axis_h
        if error:
            # Say what broke, wrapped across the plot (over stale data, if there is any).
            lines = wrap(d, error, f_small, W - 2 * pad)[:4]
            box_h = len(lines) * f_small.size * 1.4
            by = (top + bottom) / 2 - box_h / 2 if not all_vals else top
            if all_vals:
                d.rectangle((pad * 0.5, by - f_small.size * 0.4, W - pad * 0.5,
                             by + box_h + f_small.size * 0.2), fill=SURFACE + (225,))
            for i, ln in enumerate(lines):
                d.text((pad, by + i * f_small.size * 1.4), ln, font=f_small, fill=INK_2)
        if not all_vals:
            if not error:
                d.text((W / 2, (top + bottom) / 2), "waiting for data…", font=f_small,
                       fill=MUTED, anchor="mm")
            return self._finish(img, w, h, u)

        lo, hi, ticks, step = y_range(all_vals, self.ymin, self.ymax)
        decimals = 0  # as many as the step needs: 2.5 -> 1, 0.25 -> 2, 50 -> 0
        while decimals < 6 and abs(round(step, decimals) - step) > step * 1e-9:
            decimals += 1
        tick_txt = [fmt(t) if abs(t) >= 1e4 else f"{t:,.{decimals}f}" for t in ticks]
        left = pad + max(d.textlength(t, font=f_tick) for t in tick_txt) + 0.6 * pad
        right = W - pad

        def py(v):
            return bottom - (v - lo) / (hi - lo) * (bottom - top)

        t0 = now - self.window

        def px(t):
            return left + (t - t0) / self.window * (right - left)

        for t, txt in zip(ticks, tick_txt):
            yy = py(t)
            d.line((left, yy, right, yy), fill=AXIS if t == lo else GRID, width=max(1, round(u)))
            d.text((left - 0.5 * pad, yy), txt, font=f_tick, fill=MUTED, anchor="rm")

        # Time ticks: the coarsest step that still leaves ~5 labels.
        tstep = next((s for s in TIME_STEPS if self.window / s <= 5), TIME_STEPS[-1])
        tfmt = "%H:%M:%S" if tstep < 60 else "%H:%M"
        tt = math.ceil(t0 / tstep) * tstep
        while tt <= now:
            xx = px(tt)
            txt = time.strftime(tfmt, time.localtime(tt))
            half = d.textlength(txt, font=f_tick) / 2
            if xx - half >= left - 0.3 * pad and xx + half <= right + 0.3 * pad:
                d.line((xx, bottom, xx, bottom + 0.35 * f_tick.size), fill=AXIS,
                       width=max(1, round(u)))
                d.text((xx, bottom + 0.5 * f_tick.size), txt, font=f_tick, fill=MUTED,
                       anchor="mt")
            tt += tstep

        # Series: a 10% area wash (single series only, where it can't muddy), a 2px
        # line broken across gaps, and a ringed dot on the newest point.
        lw = max(2, round(2 * u))
        for k in labels:
            pts = [(px(t), py(min(max(v, lo), hi))) for t, v in series[k] if t >= t0 - 1]
            if not pts:
                continue
            ts = [t for t, _ in series[k] if t >= t0 - 1]
            gaps = np.diff(ts) if len(ts) > 1 else np.array([self.window])
            gap = max(float(np.median(gaps)) * 3, self.every * 3)
            runs, run = [], [pts[0]]
            for i in range(1, len(pts)):
                if ts[i] - ts[i - 1] > gap:
                    runs.append(run)
                    run = []
                run.append(pts[i])
            runs.append(run)
            c = colors[k]
            for run in runs:
                if single and len(run) > 1:
                    d.polygon(run + [(run[-1][0], bottom), (run[0][0], bottom)],
                              fill=c + (30,))
                if len(run) > 1:
                    d.line(run, fill=c, width=lw, joint="curve")
                for ex, ey in (run[0], run[-1]):  # round caps
                    d.ellipse((ex - lw / 2, ey - lw / 2, ex + lw / 2, ey + lw / 2), fill=c)
            ex, ey = pts[-1]
            ring, rad = 2 * u, 4 * u
            d.ellipse((ex - rad - ring, ey - rad - ring, ex + rad + ring, ey + rad + ring),
                      fill=SURFACE)
            d.ellipse((ex - rad, ey - rad, ex + rad, ey + rad), fill=c)

        return self._finish(img, w, h, u)

    def _finish(self, img, w, h, u):
        return finish(img, w, h, u, self.alpha)


def finish(img, w, h, u, alpha):
    """Round off a panel's corners, give it its border and opacity, and shrink it from
    the size it was drawn at to w x h."""
    W, H = img.size
    radius = 10 * u
    ImageDraw.Draw(img, "RGBA").rounded_rectangle(
        (0, 0, W - 1, H - 1), radius=radius, outline=BORDER, width=max(1, round(1.5 * u)))
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, W - 1, H - 1), radius=radius, fill=alpha)
    img.putalpha(mask)
    return img.resize((w, h), Image.Resampling.LANCZOS)


def squeeze(d, text, fnt, width, middle=True):
    """text, cut with an ellipsis to fit in width: in the middle, so a pod's name keeps
    the end that tells it apart, or at the end."""
    if d.textlength(text, font=fnt) <= width:
        return text

    def keep(n):
        back = n * 2 // 5 if middle else 0
        return text[:n - back] + "…" + (text[len(text) - back:] if back else "")

    lo, hi = 0, len(text) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if d.textlength(keep(mid), font=fnt) <= width:
            lo = mid
        else:
            hi = mid - 1
    return keep(lo)


def cap_widths(widths, avail, floor):
    """Narrow the widest columns, all to one cap, until they fit in avail; none goes
    narrower than floor (unless it already is). None if even that won't fit."""
    if sum(widths) <= avail:
        return list(widths)
    if sum(min(w, floor) for w in widths) > avail:
        return None
    lo, hi = floor, max(widths)
    for _ in range(30):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if sum(min(w, mid) for w in widths) <= avail else (lo, mid)
    return [min(w, lo) for w in widths]


def status_color(text):
    """The color for a cell in a status-like column: by its worst word, or yellow for a
    count that's short, like 0/1 ready. None when there's nothing to say."""
    words = set(re.findall(r"[a-z]+", text.lower()))
    if words & BAD_WORDS:
        return BAD_TEXT
    m = re.fullmatch(r"(\d+)/(\d+)", text.strip())
    if words & WARN_WORDS or (m and int(m.group(1)) < int(m.group(2))):
        return WARN_TEXT
    return OK_TEXT if words & GOOD_WORDS else None


def row_colors(cells, status_cols):
    """{column: color} for a row's status-like cells. A short count is fine in a row that
    says it's done: a finished job's pod is 0/1 ready, and should be."""
    done = any(re.search(r"\b(completed|succeeded)\b", cells[i], re.I) for i in status_cols)
    return {i: status_color(cells[i]) for i in status_cols
            if not (done and re.fullmatch(r"\d+/\d+", cells[i].strip()))}


def glow(t, now):
    """How lit up something that changed at time t is now: fully for 2 seconds, then
    fading out over the rest of HIGHLIGHT_FOR."""
    if t is None:
        return 0.0
    return min(1.0, max(0.0, 1 - (now - t - 2) / (HIGHLIGHT_FOR - 2)))


class Table:
    """A --table panel: rows from a command or a query, sized to what's in them.

    A cell that changes lights up, then fades; so does a new row, all of it; a row that
    goes away stays a few seconds, struck through. In columns named like a status, words
    like Running, Pending and CrashLoopBackOff are colored. fit() lays a snapshot out and
    says how many cells it needs; render() then draws it.
    """

    S = 2  # drawn at twice the size and shrunk, like a Chart, for smooth corners

    def __init__(self, title, every, note="", failed="command failed", alpha=PANEL_ALPHA):
        self.title, self.every, self.note, self.failed = title, every, note, failed
        self.alpha = alpha
        self._plan = None

    def _status(self, error, updated, now):
        if error:
            ago = f"  ·  showing what it said {human(now - updated)} ago" if updated else ""
            return CRITICAL, self.failed + ago
        if updated is None:
            return MUTED, "loading…"
        age = max(0, now - updated)
        dot = GOOD if age < self.every * 3 + 5 else WARNING
        line = "live" if age < self.every + 3 else f"updated {human(age)} ago"
        line += f"  ·  every {duration(self.every)}"
        return dot, line + (f"  ·  via {self.note}" if self.note else "")

    def fit(self, snap, maxc, maxr, cw, ch, now):
        """Lay snap out for a panel of at most maxc x maxr cells of cw x ch pixels, and
        return the (cols, rows) it needs."""
        columns, rows, error, updated, note = snap
        S = self.S
        U, CW = ch * S, cw * S
        d = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        pad = 0.8 * U
        f_title, f_small = font(0.72 * U, bold=True), font(0.56 * U)
        y_status = pad * 0.8 + f_title.size * 1.35
        y_head = y_status + f_small.size * 1.7
        avail_w, avail_h = maxc * CW - 2 * pad, maxr * U

        # Only rows that could be showing count toward the columns' widths (and measuring
        # all of kubectl get pods -A every second would be slow).
        most = int(avail_h / (0.5 * U * 1.6))
        could = [rows[i] for i in self._pick(rows, most, [
            i for i, c in enumerate(columns) if STATUS_COLUMN.search(c)], now)]

        # As big as the text can be; then smaller text; then the widest columns cut down
        # to fit; then columns dropped from the right.
        def measure(size):
            fb, fh = font(size), font(size * 0.86, bold=True)
            return [max([d.textlength(name, font=fh)]
                        + [d.textlength(r["cells"][i], font=fb) for r in could])
                    for i, name in enumerate(columns)]

        size = 0.62 * U
        widths = measure(size)
        need = sum(widths) + 1.1 * size * (len(widths) - 1)
        if need > avail_w:
            size = max(0.5 * U, size * avail_w / need)
            widths = measure(size)
        gap = 1.1 * size
        shown, caps = len(columns), []
        while shown:
            caps = cap_widths(widths[:shown], avail_w - gap * (shown - 1), 3.5 * size)
            if caps is not None:
                break
            shown -= 1
        caps = caps or []
        table_w = sum(caps) + gap * max(0, shown - 1)
        count_w = d.textlength("  999 rows", font=f_small)
        status_w = 2 * f_small.size + d.textlength(self._status(error, updated, now)[1],
                                                   font=f_small)
        # No rows: say why not, as wide as it takes.
        if rows:
            msg = []
        elif error:
            msg = wrap(d, error, f_small, avail_w)[:3]
        else:
            msg = ["waiting for data…" if updated is None else note or "no rows"]
        msg = [squeeze(d, m, f_small, avail_w, middle=False) for m in msg]
        inner = min(avail_w, max([table_w, status_w, 20 * CW - 2 * pad,
                                  d.textlength(self.title, font=f_title) + count_w]
                                 + [d.textlength(m, font=f_small) for m in msg]))

        # Rows: as many as fit, saving a line for "+N more" if they don't all fit.
        row_h, line_h = size * 1.6, f_small.size * 1.45
        y_rows = y_head + row_h * 1.15
        err = wrap(d, error, f_small, inner)[:2] if error and rows else []

        def room(extra_lines):
            free = avail_h - y_rows - pad * 0.6 - (len(err) + extra_lines) * line_h
            return max(1, int(free / row_h))

        n = room(1 if shown < len(columns) else 0)
        if len(rows) > n:
            n = room(1)
        status_cols = [i for i in range(shown) if STATUS_COLUMN.search(columns[i])]
        keep = self._pick(rows, n, status_cols, now)
        more = sum(1 for i in set(range(len(rows))) - set(keep) if not rows[i]["gone"])
        foot = err + ([""] if more or shown < len(columns) else [])
        body = row_h * len(keep) if rows else line_h * (len(msg) + 0.5)
        h = y_rows + body + len(foot) * line_h + pad * 0.6
        numeric = [any(r["cells"][i] for r in rows) and
                   all(NUMERIC.fullmatch(r["cells"][i]) for r in rows if r["cells"][i])
                   for i in range(shown)]
        self._plan = dict(h=h, pad=pad, size=size, gap=gap, caps=caps, shown=shown,
                          hidden=len(columns) - shown, y_status=y_status, y_head=y_head,
                          y_rows=y_rows, row_h=row_h, line_h=line_h, keep=keep, more=more,
                          err=err, msg=msg, status_cols=status_cols, numeric=numeric,
                          y_foot=y_rows + body)
        return (min(maxc, math.ceil((inner + 2 * pad) / CW)),
                min(maxr, max(4, math.ceil(h / U))))

    def _pick(self, rows, n, status_cols, now):
        """The rows to show when they don't all fit: the ones with news (a problem, a
        change, a row coming or going) first, then the rest from the top, in order."""
        if len(rows) <= n:
            return list(range(len(rows)))

        def news(r):
            colors = row_colors(r["cells"], status_cols).values()
            return bool(r["gone"] or glow(r["added"], now)
                        or any(glow(t, now) for t in r["changed"])
                        or WARN_TEXT in colors or BAD_TEXT in colors)

        return sorted(sorted(range(len(rows)), key=lambda i: (not news(rows[i]), i))[:n])

    def render(self, w, h, ch, snap, now):
        """The panel, w x h pixels, laid out by the fit() before it."""
        columns, rows, error, updated, note = snap
        p, S = dict(self._plan), self.S
        W, H, u = w * S, h * S, ch * S / 17
        # Whole cells leave some room over: split it between the top and the bottom.
        dy = max(0.0, (H - p["h"]) / 2)
        for k in ("y_status", "y_head", "y_rows", "y_foot"):
            p[k] += dy
        img = Image.new("RGB", (W, H), SURFACE)
        d = ImageDraw.Draw(img, "RGBA")
        pad, size, caps, shown = p["pad"], p["size"], p["caps"], p["shown"]
        f_title, f_small = font(0.72 * ch * S, bold=True), font(0.56 * ch * S)
        f_body, f_head = font(size), font(size * 0.86, bold=True)
        lw = max(1, round(1.5 * u))

        # The title, with how many rows on the right, and the status line under it.
        live = sum(1 for r in rows if not r["gone"])
        count = f"{live} row{'' if live == 1 else 's'}" if columns else ""
        base = dy + pad * 0.8 + f_title.size * 0.8
        d.text((W - pad, base), count, font=f_small, fill=MUTED, anchor="rs")
        room = W - 2 * pad - d.textlength(count + "  ", font=f_small)
        d.text((pad, base), squeeze(d, self.title, f_title, room, middle=False),
               font=f_title, fill=INK, anchor="ls")
        dot, line = self._status(error, updated, now)
        r = f_small.size * 0.28
        cy = p["y_status"] + f_small.size * 0.62
        d.ellipse((pad, cy - r, pad + 2 * r, cy + r), fill=dot)
        d.text((pad + 3.4 * r, p["y_status"]),
               squeeze(d, line, f_small, W - 2 * pad - 3.4 * r, middle=False),
               font=f_small, fill=INK_2 if error else MUTED)

        # Column names, and a hairline under them.
        xs = [pad + sum(caps[:i]) + p["gap"] * i for i in range(shown)]
        right = xs[-1] + caps[-1] if shown else W - pad
        mid = p["y_head"] + p["row_h"] / 2
        for i in range(shown):
            name = squeeze(d, columns[i], f_head, caps[i])
            at_x, anchor = (xs[i] + caps[i], "rm") if p["numeric"][i] else (xs[i], "lm")
            d.text((at_x, mid), name, font=f_head, fill=MUTED, anchor=anchor)
        if shown:
            hy = p["y_rows"] - p["row_h"] * 0.08
            d.line((pad, hy, right, hy), fill=AXIS, width=lw)

        stale = 150 if error else 255  # a table that may be out of date is dimmed
        for n, i in enumerate(p["keep"]):
            row, y = rows[i], p["y_rows"] + n * p["row_h"]
            mid = y + p["row_h"] / 2
            if n % 2:
                d.rectangle((pad * 0.5, y, W - pad * 0.5, y + p["row_h"]),
                            fill=(255, 255, 255, 6))
            band = (y + 1.5 * u, y + p["row_h"] - 1.5 * u)
            colors = {} if row["gone"] else row_colors(row["cells"], p["status_cols"])
            g = 0 if row["gone"] else glow(row["added"], now)
            if g:
                d.rounded_rectangle((pad * 0.5, band[0], W - pad * 0.5, band[1]),
                                    radius=4 * u, fill=CHANGE + (round(70 * g),))
            for j in range(shown):
                text = squeeze(d, row["cells"][j], f_body, caps[j])
                tw = d.textlength(text, font=f_body)
                x0 = xs[j] + caps[j] - tw if p["numeric"][j] else xs[j]
                g = 0 if row["gone"] else glow(row["changed"][j], now)
                if g and text:
                    d.rounded_rectangle((x0 - 0.35 * size, band[0], x0 + tw + 0.35 * size,
                                         band[1]), radius=4 * u, fill=CHANGE + (round(100 * g),))
                color = MUTED if row["gone"] else colors.get(j) or (INK if j == 0 else INK_2)
                d.text((x0, mid), text, font=f_body, fill=color + (stale,), anchor="lm")
            if row["gone"]:
                d.line((pad, mid, right, mid), fill=MUTED + (220,), width=lw)

        # No rows: why not. Then the footer: what went wrong, and what didn't fit.
        y = p["y_rows"] + p["line_h"] * 0.25
        for ln in p["msg"]:
            d.text((pad, y), ln, font=f_small, fill=INK_2 if error else MUTED)
            y += p["line_h"]
        y = p["y_foot"] + p["line_h"] * 0.15
        for ln in p["err"]:
            d.text((pad, y), ln, font=f_small, fill=INK_2)
            y += p["line_h"]
        more = []
        if p["more"]:
            more.append(f"+{p['more']} more row{'' if p['more'] == 1 else 's'}")
        if p["hidden"]:
            more.append(f"+{p['hidden']} more column{'' if p['hidden'] == 1 else 's'}")
        if more:
            d.text((pad, y), "  ·  ".join(more), font=f_small, fill=MUTED)
        return finish(img, w, h, u, self.alpha)


def png(img):
    """A 256-color PNG: about a fifth the size of a full-color one, and a chart's flat
    colors look the same. Small frames matter on macOS, which can split a big write and
    let other output land in the middle of it."""
    buf = io.BytesIO()
    img.quantize(256, method=Image.Quantize.FASTOCTREE).save(buf, "PNG")
    return buf.getvalue()


# --- placement -----------------------------------------------------------------

class Panel:
    def __init__(self, chart, store, size, corner, density, behind=False):
        self.chart, self.store, self.corner, self.density = chart, store, corner, density
        # z >= 0 draws over the text. z < 0 draws under it, but still over the cells'
        # background colors, so it shows through apps that paint their own (vim, htop).
        self.z = -1 if behind else 10
        self.want_cols, self.want_rows = size
        self.image_id = IMAGE_ID + CORNERS.index(corner)
        # (number, PNG, (cols, rows) it covers): the newest picture, from the drawing thread
        self.frame = None
        self._size = None  # the terminal's size, for the drawing thread
        self._sent = None  # the number of the last picture sent
        # Pictures go over in a temporary file that the terminal reads and deletes (t=t),
        # so sending one is a write of ~150 bytes, not tens of KB. macOS can split a big
        # write, and output from another program that lands inside a picture command
        # ends it early: the rest of the picture then shows up as text. Over SSH the
        # terminal can't see our files, so there the pixels go in the command itself.
        self.via_file = not (os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"))
        self._files = []  # (path, when sent): files the terminal hasn't taken yet

    def max_cells(self, cols, rows):
        """The most cells the panel can cover, in a window this size."""
        if self.corner == "fill":
            return cols, rows
        return max(20, min(self.want_cols, cols - 2)), max(6, min(self.want_rows, rows - 4))

    def place(self, cols, rows, c, r):
        """Where the top-left cell of a c x r panel goes."""
        if self.corner == "fill":
            return 0, 0
        col = {"top-left": 1, "bottom-left": 1, "center": (cols - c) // 2}.get(self.corner,
                                                                              cols - c - 1)
        row = {"bottom-right": rows - r - 4, "bottom-left": rows - r - 4,
               "center": (rows - r) // 2}.get(self.corner, 0)
        return max(0, col), max(0, row)

    def render(self, cols, rows, cw, ch, now):
        """-> (the picture, the (cols, rows) it covers)."""
        k = self.density
        snap = self.store.snapshot()
        c, r = self.max_cells(cols, rows)
        if hasattr(self.chart, "fit"):  # a table: only as big as what's in it
            c, r = self.chart.fit(snap, c, r, cw * k, ch * k, now)
        return self.chart.render(round(c * cw * k), round(r * ch * k), ch * k, snap, now), (c, r)

    def first_frame(self, size):
        """Draw the first picture now, so a graph that replaces another one shows up the
        moment the old one goes."""
        self._size = size
        img, cells = self.render(*size, time.time())
        self.frame = (0, png(img), cells)
        self._drawn = (int(time.time()), size)  # so the thread doesn't draw it again

    def start_drawing(self, stop):
        """Draw in a thread of its own. Drawing takes a tenth of a second or so, and
        re-pinning mustn't wait for it, or the graph rides along with any text that
        scrolls meanwhile."""
        threading.Thread(target=self._draw, args=(stop,), daemon=True).start()

    def _draw(self, stop):
        drawn, number, complained = getattr(self, "_drawn", None), 0, False
        sent_at = time.monotonic()
        while not stop.is_set():
            size = self._size
            # A new second (or a resize): fresh pixels, so the time axis slides along.
            stamp = size and (int(time.time()), size)
            if stamp and stamp != drawn:
                drawn = stamp
                try:
                    img, cells = self.render(*size, time.time())
                    data, old = png(img), self.frame
                    # The same picture needn't go out again (a table, mostly), except now
                    # and then, which brings it back after `reset` or inside vim.
                    if (not old or (data, cells) != old[1:]
                            or time.monotonic() - sent_at >= RESEND_EVERY):
                        number, sent_at = number + 1, time.monotonic()
                        self.frame = (number, data, cells)
                except Exception:  # a bug: say so once, and keep trying each second
                    if not complained:
                        complained = True
                        traceback.print_exc()
            stop.wait(0.02)

    def transmit(self, keys, data):
        """The command that sends a picture: by temporary file, or failing that, in
        base64 chunks."""
        if self.via_file:
            try:
                fd, path = tempfile.mkstemp(prefix="tty-graphics-protocol-graph-",
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
        from then on, send the pixels themselves, starting with this picture again."""
        now = time.monotonic()
        if any(now - t > 5 and os.path.exists(p) for p, t in self._files):
            self.via_file, self._sent = False, None
            self.close()
        self._files = [(p, t) for p, t in self._files if now - t <= 5]

    def close(self):
        for path, _ in self._files:
            try:
                os.remove(path)
            except OSError:
                pass
        self._files = []

    def tick(self, cols, rows, cw, ch):
        self._size = (cols, rows, cw, ch)
        if self._files:
            self.check_files()
        frame = self.frame
        c, r = frame[2] if frame else self.max_cells(cols, rows)
        col, row = self.place(cols, rows, c, r)
        keys = f"i={self.image_id},p=1,c={c},r={r},z={self.z},C=1,q=2"
        if frame and frame[0] != self._sent:
            self._sent = frame[0]
            # Plus a hidden second placement (U=1, a "virtual" one, shown only where
            # special placeholder characters are, so never here). `clear` and full-screen
            # redraws delete the visible placement and free any picture nothing else
            # refers to; this keeps ours, so the next re-pin brings it straight back.
            body = (self.transmit(keys, frame[1])
                    + apc(f"a=p,U=1,i={self.image_id},p=2,c={c},r={r},q=2"))
        else:
            # Same pixels: re-pin placement 1 so scrolling can't carry it off.
            body = apc(f"a=p,{keys}")
        return at(col, row, body)


# --- command line --------------------------------------------------------------

def parse_size(s):
    m = re.fullmatch(r"(\d+)x(\d+)", s)
    if not m:
        raise argparse.ArgumentTypeError("size is COLSxROWS, e.g. 64x15")
    return int(m.group(1)), int(m.group(2))


def parse_duration(s):
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", s)
    if not m:
        raise argparse.ArgumentTypeError("durations look like 90, 90s, 15m, 2h, 1d")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def make_source(args, every, step):
    """-> (fetch function or None for --stdin, the "via ..." note). Raises RuntimeError
    when it can't be set up."""
    table = Rows(args.columns, args.unit) if args.table else None
    if args.promql:
        # The most specific wins: --prom, then --grafana, then an MCP server (--mcp or
        # $GRAFANA_MCP_COMMAND), then $GRAFANA_URL.
        mcp = args.mcp or (None if args.grafana else os.environ.get("GRAFANA_MCP_COMMAND"))
        if args.prom:
            base, token, note = args.prom, os.environ.get("PROM_TOKEN"), "prometheus"
        elif mcp:
            return McpSource(mcp, args.ds, args.promql, args.window, step,
                             args.legend, table), "grafana mcp"
        else:
            url = args.grafana or os.environ.get("GRAFANA_URL")
            token = (os.environ.get("GRAFANA_TOKEN")
                     or os.environ.get("GRAFANA_SERVICE_ACCOUNT_TOKEN"))
            if not url:
                raise RuntimeError("--promql needs a way in: set GRAFANA_URL (and "
                                   "GRAFANA_TOKEN), or GRAFANA_MCP_COMMAND, or pass "
                                   "--grafana, --mcp or --prom")
            base, note = grafana_base(url, token, args.ds), "grafana"
        return PromSource(base, token, args.promql, args.window, step, args.legend,
                          table), note
    if args.cmd:
        return CmdSource(args.cmd, max(10, every * 3), args.window, step, args.legend,
                         table), args.cmd.split()[0]
    return None, "stdin"


def detach(path, lifetime, store):
    """Start a copy of ourselves drawing in the background, in a session of its own with
    no stdin or stdout, so whatever ran us gets its prompt back. It starts from the data
    in `store`. Returns its pid once it has registered, so --list and --stop see it
    straight away."""
    seed = os.path.join(state_dir(), f"seed-{os.getpid()}.json")
    store.save(seed)
    argv = [a for a in sys.argv[1:] if a != "--detach"]
    log_path = os.path.join(state_dir(), "detached.log")
    with open(log_path, "a") as log:
        child = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), *argv, "--tty", path,
             "--for", str(lifetime), "--seed", seed],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log,
            start_new_session=True)
    registered = os.path.join(state_dir(), f"{child.pid}.json")
    deadline = time.monotonic() + 30
    while not os.path.exists(registered):
        if child.poll() is not None or time.monotonic() > deadline:
            try:
                os.remove(seed)
            except OSError:
                pass
            try:
                with open(log_path) as f:
                    tail = f.read().strip().splitlines()[-1:]
            except OSError:
                tail = []
            sys.exit(f"graph: the background copy didn't start{': ' + tail[0] if tail else ''}")
        time.sleep(0.05)
    return child.pid


def main():
    ap = argparse.ArgumentParser(prog="graph.py", description=__doc__, allow_abbrev=False,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--promql", metavar="QUERY", help="a PromQL query, through Grafana or --prom")
    src.add_argument("--cmd", metavar="SHELL",
                     help="a shell command that prints numbers (or, with --table, a table)")
    src.add_argument("--stdin", action="store_true", help="numbers coming in on a pipe")
    ap.add_argument("--table", action="store_true",
                    help="a table instead of a graph, from --cmd or --promql")
    ap.add_argument("--columns", type=lambda s: [c.strip() for c in s.split(",") if c.strip()],
                    metavar="A,B,...", help="with --table: which columns, in this order")
    ap.add_argument("--title", help="panel title (default: the query or command)")
    ap.add_argument("--unit", default="", help="shown after values, e.g. ms, %%, req/s")
    ap.add_argument("--window", type=parse_duration, default=900,
                    help="how much history to show (default: 15m)")
    ap.add_argument("--every", type=parse_duration,
                    help="how often to fetch (default: 30s for --promql, 2s for --cmd, 5s "
                         "for --cmd with --table)")
    ap.add_argument("--step", type=parse_duration,
                    help="--promql resolution (default: window/240, at least 15s)")
    ap.add_argument("--legend", help="series names, Grafana style: '{{instance}}'")
    ap.add_argument("--grafana", metavar="URL", help="Grafana to query (default: $GRAFANA_URL)")
    ap.add_argument("--ds", metavar="UID", help="Grafana datasource (default: its default "
                                                "Prometheus one)")
    ap.add_argument("--mcp", metavar="COMMAND",
                    help="query through a Grafana MCP server: a command that starts one, "
                         "like 'uvx mcp-grafana', or its http(s) URL (default: "
                         "$GRAFANA_MCP_COMMAND)")
    ap.add_argument("--prom", metavar="URL", help="query Prometheus (or VictoriaMetrics, ...) "
                                                  "directly instead of through Grafana")
    ap.add_argument("--ymin", type=float, help="bottom of the y axis (default: 0, or the data)")
    ap.add_argument("--ymax", type=float, help="top of the y axis (default: fit the data)")
    ap.add_argument("--at", choices=CORNERS, default="top-right",
                    help="where on the screen: a corner, center, or fill (the whole "
                         "window) (default: top-right)")
    ap.add_argument("--behind", action="store_true",
                    help="draw behind the text instead of over it, faded (see --opacity)")
    ap.add_argument("--opacity", type=float,
                    help=f"1 is solid; lower fades it (default: {PANEL_ALPHA / 255:.2f}, or "
                         f"{BEHIND_OPACITY} with --behind)")
    ap.add_argument("--size", type=parse_size,
                    help="COLSxROWS, shrunk to fit the window (default: 64x15; for a "
                         "--table, the most it grows to, 80x20)")
    ap.add_argument("--density", type=float,
                    help="pixels drawn per pixel the terminal reports (default: 2, sharp "
                         "on Retina; 1 with --behind, where it looks the same faded)")
    ap.add_argument("--tty", metavar="DEVICE",
                    help="terminal to draw on (default: this one, or the one the process "
                         "that started this is running in)")
    ap.add_argument("--for", dest="lifetime", type=parse_duration, metavar="DURATION",
                    help="go away after this long; 0 means never (default: never, or 30m "
                         "with --detach)")
    ap.add_argument("--snapshot", metavar="PNG", help="draw one frame to a file and exit")
    ap.add_argument("--detach", action="store_true",
                    help="check the source once, start drawing in the background, print "
                         "the id and return")
    ap.add_argument("--list", action="store_true", help="list the graphs that are showing")
    ap.add_argument("--seed", help=argparse.SUPPRESS)  # from --detach: data to start from
    ap.add_argument("--stop", metavar="ID",
                    help="stop a graph: an id from --list, a corner of this terminal, or all")
    args = ap.parse_args()

    if args.list:
        return list_graphs()
    if args.stop:
        return stop_command(args.stop)
    if not (args.promql or args.cmd or args.stdin):
        ap.error("pick a source: --promql, --cmd or --stdin (or --list / --stop)")
    if args.detach and args.stdin:
        ap.error("--stdin can't --detach: the background copy can't read your pipe")
    if args.detach and args.snapshot:
        ap.error("pick one of --detach and --snapshot")
    if (args.grafana or args.ds or args.prom or args.mcp) and not args.promql:
        ap.error("--grafana, --mcp, --prom and --ds go with --promql")
    if sum(map(bool, (args.grafana, args.mcp, args.prom))) > 1:
        ap.error("pick one of --grafana, --mcp and --prom")
    if args.density is not None and args.density <= 0:
        ap.error("--density must be more than 0")
    if args.opacity is not None and not 0 < args.opacity <= 1:
        ap.error("--opacity is more than 0, and at most 1")
    if args.ymin is not None and args.ymax is not None and args.ymax <= args.ymin:
        ap.error("--ymax must be more than --ymin")
    if args.every is not None and args.every <= 0:
        ap.error("--every must be more than 0")
    if args.table and args.stdin:
        ap.error("--table takes --cmd or --promql")
    if args.table and (args.behind or args.legend or args.ymin is not None
                       or args.ymax is not None):
        ap.error("--behind, --legend, --ymin and --ymax are for graphs, not --table "
                 "(text behind text is hard to read; --columns picks a table's columns)")
    if args.columns and not args.table:
        ap.error("--columns goes with --table")

    # Prometheus usually scrapes every 15-60s, so asking for the whole window more often
    # than every 30s mostly gets the same numbers back. A table from a command (kubectl,
    # say) is gentler on whatever it asks at 5s.
    every = args.every or (30 if args.promql else 5 if args.table else 2)
    panel_size = args.size or ((80, 20) if args.table else (64, 15))
    step = args.step or max(15, round(args.window / 240))
    lifetime = args.lifetime if args.lifetime is not None else (
        DETACHED_FOR if args.detach else 0)
    title = args.title or args.promql or args.cmd or "stdin"
    if len(title) > 48:
        title = title[:47] + "…"
    store = TableStore() if args.table else Store(args.window)
    if args.seed:
        store.load(args.seed)
    stop = threading.Event()
    try:
        source, note = make_source(args, every, step)
    except RuntimeError as e:
        sys.exit(f"graph: {e}")
    opacity = args.opacity or (BEHIND_OPACITY if args.behind else PANEL_ALPHA / 255)
    failed = "command failed" if args.cmd else "query failed"
    if args.table:
        chart = Table(title, every, note, failed, round(255 * opacity))
    else:
        chart = Chart(title, args.unit, args.window, args.ymin, args.ymax, every, note,
                      failed, round(255 * opacity))
    # Behind the text, faded, half the pixels look the same, and a whole-window graph
    # then costs no more than a corner one.
    density = args.density or (1.0 if args.behind else 2.0)
    panel = Panel(chart, store, panel_size, args.at, density, args.behind)

    path = args.tty or find_tty()
    if args.snapshot:
        try:
            size = Tty(path).size() if path else (134, 34, 8.0, 17.4)
        except OSError:
            size = (134, 34, 8.0, 17.4)
        if source:
            try:
                source(store)  # one fetch is all a snapshot needs
            except Exception as e:  # draw the error, same as live
                store.fail(str(e) or type(e).__name__)
            finally:
                close(source)
        else:
            read_stdin(store, stop, mark_end=False)  # all of it: that's the picture
        img, _ = panel.render(*size, time.time())
        img.save(args.snapshot)
        # Say what's in it, so whoever asked can tell without opening the picture.
        if args.table:
            columns, rows, error, _, _ = store.snapshot()
            what = f"{len(rows)} rows, {len(columns)} columns"
        else:
            series, _, error, _, _ = store.snapshot()
            what = f"{len(series)} series, {sum(map(len, series.values()))} points"
        print(f"{args.snapshot}: {img.width}x{img.height}, {what}"
              + (f"; failed: {error}" if error else ""))
        return

    if not path:
        sys.exit("graph: can't find a terminal to draw on; pass --tty "
                 "(run `tty` in the terminal you want)")
    try:
        tty = Tty(path)
    except OSError as e:
        sys.exit(f"graph: can't open {path}: {e.strerror}")

    if args.detach:
        try:
            source(store)  # fail now, while someone is around to read why
        except Exception as e:
            sys.exit(f"graph: {e}")
        finally:
            close(source)
        pid = detach(path, lifetime, store)
        print(f"graph: showing {title!r} at the {args.at} of {path} "
              f"{'for ' + human(lifetime) if lifetime else 'until stopped'} (id {pid})")
        print(f"graph: stop it with: {os.path.basename(__file__)} --stop {pid}")
        return

    # One graph per place on a terminal: this one replaces whatever is there, but only
    # now that its first picture is ready, so the gap is as short as can be. (With
    # --detach this happens in the copy, after the source checked out: a check that
    # fails leaves the old graph alone.)
    panel.first_frame(tty.size())
    stop_graphs([g for g in running() if g["tty"] == path and g["at"] == args.at])

    # With `stty tostop`, a background job writing to its terminal gets SIGTTOU and
    # stops. If the signal is ignored, the kernel lets the write through instead.
    signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    stopping = []
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, lambda *_: stopping.append(True))

    if source:
        fetch = threading.Thread(target=poll, args=(source, store, every, stop), daemon=True)
    else:
        fetch = threading.Thread(target=read_stdin, args=(store, stop), daemon=True)
    registration = register(path, args.at, title, time.time() + lifetime if lifetime else None)
    fetch.start()
    panel.start_drawing(stop)
    deadline = time.monotonic() + lifetime if lifetime else math.inf
    try:
        while not stopping and time.monotonic() < deadline:
            tty.send(panel.tick(*tty.size()))
            time.sleep(REPIN_EVERY)
        tty.send(apc(f"a=d,d=I,i={panel.image_id},q=2"))  # remove our image on the way out
    except OSError as e:
        if e.errno not in (errno.EIO, errno.ENXIO, errno.ENOTTY):
            raise
        # The terminal went away (macOS says ENOTTY once the shell that owned it has
        # exited), so there's nothing left to clean up.
    finally:
        stop.set()
        close(source)
        panel.close()  # pictures the terminal never took
        try:
            os.remove(registration)
        except OSError:
            pass


def close(source):
    """Shut down whatever a source keeps running (an MCP server)."""
    if hasattr(source, "close"):
        source.close()


if __name__ == "__main__":
    main()
