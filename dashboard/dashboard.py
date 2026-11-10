#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["numpy", "Pillow>=10.1"]
# ///
r"""Show a Grafana dashboard in your terminal, full screen.

  ./dashboard.py node                  the dashboard called "node" (or a list to pick
                                       from, when more than one matches)
  ./dashboard.py 'https://grafana.example.com/d/abc123/node?var-job=api&from=now-6h'
                                       a dashboard's link, pasted from your browser: its
                                       variables and time range come along
  ./dashboard.py abc123                a dashboard's uid
  ./dashboard.py                       a list of every dashboard, to pick from
  ./dashboard.py node --snapshot node.png   draw it once, into a picture, and exit

It reads the dashboard the way Grafana's own page does, has Grafana run each panel's
queries, and draws the panels itself, where the dashboard puts them. A panel it doesn't
know how to draw keeps its place, as an empty frame with its title.

Three ways in to Grafana (a flag wins over an environment variable):
  Grafana's API: --grafana URL or $GRAFANA_URL, with $GRAFANA_TOKEN (or
    $GRAFANA_SERVICE_ACCOUNT_TOKEN).
  A Grafana MCP server: --mcp or $GRAFANA_MCP_COMMAND. Either a command that starts one
    (like "uvx mcp-grafana"), which gets the same variables, or its URL, with
    $GRAFANA_MCP_TOKEN if it needs one. Its query tools are Prometheus-only, so panels on
    other datasources say so instead of drawing.
  Grafana's gcx CLI, logged in already: --gcx. It's also what's used when none of the
    above is set and gcx is installed.

Two looks: --theme graph (the default, ../graph's dark style) and --theme space. Every
choice about how things look is in the Theme class below, so a new look is a subclass
of it, added to THEMES.

Needs a terminal with the kitty graphics protocol: made for Ghostty, and kitty should
work too.
"""

import argparse
import base64
import calendar
import colorsys
import concurrent.futures
import copy
import datetime
import fcntl
import io
import itertools
import json
import math
import os
import queue
import re
import select
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import traceback
import unicodedata
from decimal import ROUND_HALF_UP, Decimal
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.request import Request, urlopen

import numpy as np
from PIL import Image, ImageDraw, ImageFont

IMAGE_ID = 0x44410000  # "DA": panels' pictures count up from here, two ids each
CHUNK = 4096           # the protocol takes base64 payloads in chunks of at most 4096 bytes
MCP_PROTOCOL = "2025-06-18"
GRID_COLS = 24         # Grafana's grid: 24 columns across,
GRID_ROW = 30          # rows 30 CSS pixels high,
GRID_GAP = 8           # 8 pixels apart
CSS_PER_CELL = 17      # graph's yardstick: a cell 17 pixels high counts as 17 CSS pixels
SUPERSAMPLE = 2        # drawn twice as big, then shrunk: PIL's lines aren't antialiased
DEFAULT_REFRESH = 30   # seconds between refreshes, when the dashboard doesn't say
PROMETHEUS_TYPES = {"prometheus", "victoriametrics-metrics-datasource"}

# The first font file that loads wins: DejaVu on Linux, Helvetica on macOS. Failing
# those, Pillow's built-in font, which has no bold.
REGULAR = [("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0),
           ("/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf", 0),
           ("/System/Library/Fonts/Helvetica.ttc", 0)]
BOLD = [("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 0),
        ("/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf", 0),
        ("/System/Library/Fonts/Helvetica.ttc", 1)]
MONO = [("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 0),
        ("/usr/share/fonts/dejavu-sans-mono-fonts/DejaVuSansMono.ttf", 0),
        ("/System/Library/Fonts/Menlo.ttc", 0)]

# Grafana's named colors, which dashboards use for thresholds, overrides and mappings:
# each hue in five shades, from super-light to dark.
GRAFANA_HUES = {
    "red": ("#FFA6B0", "#FF7383", "#F2495C", "#E02F44", "#C4162A"),
    "orange": ("#FFCB7D", "#FFB357", "#FF9830", "#FF780A", "#FA6400"),
    "yellow": ("#FFF899", "#FFEE52", "#FADE2A", "#F2CC0C", "#E0B400"),
    "green": ("#C8F2C2", "#96D98D", "#73BF69", "#56A64B", "#37872D"),
    "blue": ("#C0D8FF", "#8AB8FF", "#5794F2", "#3274D9", "#1F60C4"),
    "purple": ("#DEB6F2", "#CA95E5", "#B877D9", "#A352CC", "#8F3BB8"),
}
SHADES = [("super-light-", 0), ("semi-dark-", 3), ("light-", 1), ("dark-", 4), ("", 2)]
CSS_COLORS = {"white": (255, 255, 255), "black": (0, 0, 0), "gray": (128, 128, 128),
              "grey": (128, 128, 128)}


# --- the look --------------------------------------------------------------------

def hex_rgb(s):
    s = s.lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def mix(a, b, t):
    """a, moved a fraction t of the way to b."""
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


class Theme:
    """Every choice about how things look, in one place, so a new look is one subclass
    away (SpaceTheme is one). This is graph's dark style."""

    name = "graph"
    screen = (14, 14, 13)            # behind the panels, in a --snapshot
    surface = (26, 26, 25)           # a panel's background
    panel_alpha = 238                # 0-255, for a whole panel
    empty_alpha = 110                # a panel we don't draw: a fainter frame
    border = (255, 255, 255, 26)
    focus = (0x39, 0x87, 0xE5)       # the ring around the panel the keys act on
    ink = (240, 239, 236)            # titles and values
    ink_2 = (195, 194, 183)          # secondary text
    muted = (137, 135, 129)          # axis labels, hints
    grid = (44, 44, 42)
    axis = (56, 56, 53)
    series = [(0x39, 0x87, 0xE5), (0xD9, 0x59, 0x26), (0x19, 0x9E, 0x70), (0xC9, 0x85, 0x00),
              (0xD5, 0x51, 0x81), (0x00, 0x83, 0x00), (0x90, 0x85, 0xE9), (0xE6, 0x67, 0x67)]
    good, warning, critical = (0x0C, 0xA3, 0x0C), (0xFA, 0xB2, 0x19), (0xD0, 0x3B, 0x3B)
    radius = 10                      # panel corners, in CSS pixels
    regular, bold, mono = REGULAR, BOLD, MONO

    def __init__(self):
        # Grafana's hues, in this theme's own colors: a dashboard that says "red" for
        # trouble gets this theme's red.
        pick = lambda i: self.series[i % len(self.series)]  # a theme may have few colors
        self.hues = {"green": self.good, "red": self.critical, "yellow": self.warning,
                     "orange": pick(1), "blue": pick(0), "purple": pick(6)}

    def hue(self, name, shade):
        base = self.hues.get(name) or hex_rgb(GRAFANA_HUES[name][2])
        return {0: mix(base, (255, 255, 255), 0.55), 1: mix(base, (255, 255, 255), 0.3),
                3: mix(base, (0, 0, 0), 0.15), 4: mix(base, (0, 0, 0), 0.3)}.get(shade, base)

    def color(self, spec, index=0):
        """An RGB color from what Grafana writes: a named color ("green",
        "semi-dark-red"), hex, rgb()/rgba(), or nothing (the index-th series color)."""
        if not isinstance(spec, str) or not spec.strip():
            return self.series[index % len(self.series)]
        s = spec.strip().lower()
        if s in ("text", "primary"):
            return self.ink
        if s == "transparent":
            return self.surface
        if re.fullmatch(r"#(?:[0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})", s):
            return hex_rgb(s[:7] if len(s) > 4 else s)
        m = re.fullmatch(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+).*\)", s)
        if m:
            return tuple(min(255, int(float(x))) for x in m.groups())
        for prefix, shade in SHADES:
            if s.startswith(prefix) and s[len(prefix):] in GRAFANA_HUES:
                return self.hue(s[len(prefix):], shade)
        return CSS_COLORS.get(s, self.series[index % len(self.series)])

    def backdrop(self, w, h):
        """A picture for behind everything, w x h pixels, or None for the terminal's own
        background."""
        return None


class SpaceTheme(Theme):
    """Deep blue-black, neon series, and stars behind it all."""

    name = "space"
    screen = (5, 6, 18)
    surface = (12, 14, 34)
    panel_alpha = 215
    border = (120, 140, 255, 60)
    focus = (0x7D, 0xF9, 0xFF)
    ink = (232, 236, 255)
    ink_2 = (178, 186, 230)
    muted = (110, 118, 170)
    grid = (28, 32, 66)
    axis = (44, 50, 96)
    series = [(0x7D, 0xF9, 0xFF), (0xFF, 0x6E, 0xC7), (0xB8, 0xFF, 0x6E), (0xFF, 0xD1, 0x66),
              (0xA0, 0x8C, 0xFF), (0x5E, 0xE6, 0xA8), (0xFF, 0x8A, 0x5C), (0x6E, 0xA8, 0xFF)]
    good, warning, critical = (0x5E, 0xE6, 0xA8), (0xFF, 0xD1, 0x66), (0xFF, 0x4F, 0x8B)

    def backdrop(self, w, h):
        rng = np.random.default_rng(7)
        img = Image.new("RGB", (w, h), self.screen)
        d = ImageDraw.Draw(img)
        for _ in range(w * h // 900):
            x, y = rng.uniform(0, w), rng.uniform(0, h)
            r = rng.choice([0.6, 0.8, 1.0, 1.6], p=[0.5, 0.3, 0.15, 0.05])
            c = mix(self.screen, (230, 235, 255), rng.uniform(0.3, 1.0))
            d.ellipse((x - r, y - r, x + r, y + r), fill=c)
        return img


THEMES = {"graph": Theme, "space": SpaceTheme}
_fonts = {}


def font(theme, px, bold=False, mono=False):
    paths = theme.mono if mono else theme.bold if bold else theme.regular
    # FreeType gives up ("division by zero") on absurd sizes, which fitting a short
    # value into a big panel can ask for.
    key = (id(paths), max(6, min(3000, round(px))))
    if key not in _fonts:
        for path, index in paths:
            try:
                _fonts[key] = ImageFont.truetype(path, key[1], index=index)
                break
            except OSError:
                pass
        else:
            _fonts[key] = ImageFont.load_default(size=key[1])
    return _fonts[key]


# --- kitty graphics protocol (the same as ../graph) -----------------------------------

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


def png(img, small=False):
    """PNG bytes, quick to make. small: 256 colors, about a fifth the size, for when the
    pixels have to go through the terminal itself (over SSH)."""
    buf = io.BytesIO()
    if small:
        img.quantize(256, method=Image.Quantize.FASTOCTREE).save(buf, "PNG")
    else:
        img.save(buf, "PNG", compress_level=1)
    return buf.getvalue()


# --- talking to Grafana ---------------------------------------------------------------
#
# Everything dashboard.py needs from Grafana is a handful of calls: a dashboard, a
# search, the datasources, a query, a library panel, and a few Prometheus lookups for
# variables. RestGrafana makes them as HTTP requests, sent straight or through gcx;
# McpGrafana makes them with an MCP server's tools.

class Unsupported(RuntimeError):
    """A dashboard dashboard.py can't draw (yet), and why."""


def api_message(text):
    """The interesting part of an error body Grafana sent: its "message", or the text."""
    try:
        doc = json.loads(text)
    except (TypeError, ValueError):
        return (text or "").strip()[:300]
    if isinstance(doc, dict):
        return str(doc.get("message") or doc.get("error") or text).strip()[:300]
    return str(doc)[:300]


def http_request(base, token=None):
    """A request(method, path, body) that talks to Grafana's HTTP API at base."""
    base = base.rstrip("/")

    def request(method, path, body=None):
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            with urlopen(Request(base + path, data=data, headers=headers, method=method),
                         timeout=60) as r:
                return json.load(r)
        except HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            try:  # a query that failed still says which of its parts did and didn't
                doc = json.loads(text)
                if isinstance(doc, dict) and "results" in doc:
                    return doc
            except ValueError:
                pass
            hint = " (is GRAFANA_TOKEN set?)" if e.code in (401, 403) and not token else ""
            raise RuntimeError(f"HTTP {e.code}: {api_message(text)}{hint}") from None
        except URLError as e:
            raise RuntimeError(f"can't reach Grafana at {base}: {e.reason}") from None
        except (OSError, ValueError) as e:
            raise RuntimeError(f"Grafana at {base}: {e}") from None

    return request


def gcx_request(context=None):
    """A request(method, path, body) that goes through `gcx api`, which signs it in with
    gcx's own login, so no token passes through here."""

    def request(method, path, body=None):
        argv = ["gcx", "api", path, "-o", "json", "--no-color"]
        if context:
            argv += ["--context", context]
        if body is not None:
            argv += ["-d", "@-"]  # the body on stdin, not the command line
        if method not in ("GET", "POST") or (method == "POST" and body is None):
            argv += ["-X", method]
        try:
            p = subprocess.run(argv, input=json.dumps(body) if body is not None else None,
                               capture_output=True, text=True, timeout=90)
        except subprocess.TimeoutExpired:
            raise RuntimeError("gcx didn't answer in 90s") from None
        except OSError as e:
            raise RuntimeError(f"can't run gcx: {e.strerror}") from None
        out = p.stdout.strip()
        doc = None
        try:
            doc = json.loads(out) if out else None
        except ValueError:
            pass
        if p.returncode == 0 and not (isinstance(doc, dict) and doc.get("type") == "gcx.error"):
            if doc is None and out:
                raise RuntimeError(f"gcx didn't answer with JSON: {out[:200]}")
            return doc
        # gcx's errors look like {"type": "gcx.error", "error": {"summary": "HTTP 404",
        # "details": "<Grafana's body>"}}.
        err = (doc or {}).get("error") if isinstance(doc, dict) else None
        if not err:
            try:
                err = json.loads(p.stderr).get("error")
            except (ValueError, AttributeError):
                err = None
        if isinstance(err, dict):
            details = err.get("details") or ""
            try:
                inner = json.loads(details)
                if isinstance(inner, dict) and "results" in inner:
                    return inner
            except (TypeError, ValueError):
                pass
            raise RuntimeError(f"{err.get('summary', 'gcx failed')}: {api_message(details)}"
                               if details else err.get("summary", "gcx failed"))
        raise RuntimeError((p.stderr or out or f"gcx exited with {p.returncode}").strip()[:300])

    return request


def query_results(doc):
    """/api/ds/query's answer -> {refId: (frames, error or None)}."""
    out = {}
    for ref, res in ((doc or {}).get("results") or {}).items():
        res = res or {}
        err = res.get("error")
        if not err and res.get("status") and int(res["status"]) >= 400:
            err = f"status {res['status']}"
        out[ref] = (read_frames(res.get("frames")), err)
    return out


class RestGrafana:
    def __init__(self, request, via):
        self.request, self.via = request, via
        self._ds = None
        self._lock = threading.Lock()

    def dashboard(self, uid):
        """(the dashboard's JSON, its meta) for a uid."""
        try:
            doc = self.request("GET", f"/api/dashboards/uid/{quote(uid, safe='')}")
        except RuntimeError as e:
            # Grafana 13 sends a dashboard stored in its newer format only from its newer
            # API, and says so here.
            if re.search(r"v2|api ?version|not supported|dashboard\.grafana\.app", str(e), re.I):
                raise Unsupported(V2_MESSAGE) from None
            if "404" in str(e):
                raise RuntimeError(f"no dashboard with uid {uid}") from None
            raise
        if not isinstance(doc, dict):
            raise RuntimeError("Grafana didn't send a dashboard")
        return doc.get("dashboard") or {}, doc.get("meta") or {}

    def search(self, text=""):
        """Dashboards whose title has text in it: [{uid, title, folder}]."""
        q = urlencode({"type": "dash-db", "query": text, "limit": 5000})
        hits = self.request("GET", f"/api/search?{q}") or []
        return [{"uid": h["uid"], "title": h.get("title") or "", "folder": h.get("folderTitle") or ""}
                for h in hits if isinstance(h, dict) and h.get("uid")]

    def datasources(self):
        """Every datasource: [{uid, name, type, isDefault, jsonData}]. From the settings
        Grafana's page loads, which any viewer may read; /api/datasources needs more."""
        with self._lock:
            if self._ds is None:
                try:
                    settings = self.request("GET", "/api/frontend/settings") or {}
                    out = []
                    for name, s in (settings.get("datasources") or {}).items():
                        if isinstance(s, dict) and s.get("uid"):
                            out.append({"uid": s["uid"], "name": s.get("name") or name,
                                        "type": s.get("type") or "",
                                        "isDefault": bool(s.get("isDefault")),
                                        "jsonData": s.get("jsonData") or {}})
                    if not any(d["isDefault"] for d in out):
                        default = settings.get("defaultDatasource")
                        for d in out:
                            d["isDefault"] = d["name"] == default
                    self._ds = out
                except RuntimeError:
                    self._ds = [d for d in self.request("GET", "/api/datasources") or []
                                if isinstance(d, dict)]
            return self._ds

    def query(self, queries, start, end):
        """Run queries (Grafana's own query JSON, datasource and all) over start..end
        (unix seconds) -> {refId: (frames, error)}."""
        body = {"queries": queries, "from": str(int(start * 1000)), "to": str(int(end * 1000))}
        return query_results(self.request("POST", "/api/ds/query", body))

    def library_panel(self, uid):
        doc = self.request("GET", f"/api/library-elements/{quote(uid, safe='')}") or {}
        return (doc.get("result") or {}).get("model") or {}

    # Prometheus lookups, for variables' queries: through the datasource's own API.
    def prom_label_names(self, ds, match, start, end):
        return rest_prom_label_names(self.request, ds, match, start, end)

    def prom_label_values(self, ds, label, match, start, end):
        return rest_prom_label_values(self.request, ds, label, match, start, end)

    def prom_series(self, ds, match, start, end):
        return rest_prom_series(self.request, ds, match, start, end)

    def prom_instant(self, ds, expr, t):
        return rest_prom_instant(self.request, ds, expr, t)

    def close(self):
        pass


V2_MESSAGE = ("this dashboard is in Grafana's newer format (V2: tabs, auto grids), which "
              "dashboard.py can't draw yet")


class McpClient:
    """Just enough MCP to call a server's tools: over its stdin and stdout when it's a
    command we start, or over HTTP when it's a URL. The same as ../graph's.

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
            "clientInfo": {"name": "dashboard.py", "version": "1"}})
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


def listed(doc):
    """A list from a tool that may wrap its list in an object."""
    if isinstance(doc, dict):
        doc = next((v for v in doc.values() if isinstance(v, list)), [])
    return [x for x in doc or [] if isinstance(x, dict)]


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


class McpGrafana:
    """Grafana through an MCP server's tools, by grafana/mcp-grafana's names. Its query
    tools are one per kind of datasource; this uses the Prometheus one, so other panels
    say they can't be drawn this way."""

    def __init__(self, target):
        self.client, self.via = McpClient(target), "MCP"
        self._lock = threading.Lock()  # one conversation with the server at a time
        self._ds = None

    def call(self, tool, args):
        with self._lock:
            return self.client.call(tool, args)

    def dashboard(self, uid):
        doc = self.call("get_dashboard_by_uid", {"uid": uid})
        if isinstance(doc, dict) and isinstance(doc.get("dashboard"), dict):
            return doc["dashboard"], doc.get("meta") or {}
        return doc if isinstance(doc, dict) else {}, {}

    def search(self, text=""):
        return [{"uid": h["uid"], "title": h.get("title") or "", "folder": h.get("folderTitle") or ""}
                for h in listed(self.call("search_dashboards", {"query": text})) if h.get("uid")]

    def datasources(self):
        if self._ds is None:
            self._ds = [{"uid": d.get("uid"), "name": d.get("name") or "", "type": d.get("type") or "",
                         "isDefault": bool(d.get("isDefault")), "jsonData": d.get("jsonData") or {}}
                        for d in listed(self.call("list_datasources", {})) if d.get("uid")]
        return self._ds

    def query(self, queries, start, end):
        out = {}
        for q in queries:
            ref, ds = q.get("refId") or "A", q.get("datasource") or {}
            if ds.get("type") not in PROMETHEUS_TYPES:
                out[ref] = ([], f"can't run {ds.get('type') or 'these'} queries through MCP")
                continue
            instant = bool(q.get("instant")) and not q.get("range")
            try:
                doc = self.call("query_prometheus", {
                    "datasourceUid": ds.get("uid"), "expr": q.get("expr") or "",
                    "queryType": "instant" if instant else "range",
                    "startTime": iso(end if instant else start), "endTime": iso(end),
                    "stepSeconds": max(1, int((q.get("intervalMs") or 15000) / 1000))})
                out[ref] = (prom_frames(doc, ref, q.get("legendFormat"), q.get("expr")), None)
            except (RuntimeError, ValueError) as e:
                out[ref] = ([], str(e))
        return out

    def library_panel(self, uid):
        raise RuntimeError("library panels can't be read through MCP")

    # Prometheus lookups, for variables' queries, with the server's label tools. There's
    # no tool for series, so an instant query of the selector stands in.
    def _labels_args(self, ds, match, start, end):
        args = {"datasourceUid": ds["uid"], "startRfc3339": iso(start), "endRfc3339": iso(end),
                "limit": 5000}
        sel = mcp_selector(match)
        if sel:
            args["matches"] = [sel]
        return args

    def prom_label_names(self, ds, match, start, end):
        return strings(self.call("list_prometheus_label_names", self._labels_args(ds, match, start, end)))

    def prom_label_values(self, ds, label, match, start, end):
        args = dict(self._labels_args(ds, match, start, end), labelName=label)
        return strings(self.call("list_prometheus_label_values", args))

    def prom_series(self, ds, match, start, end):
        return [labels for labels, _ in self.prom_instant(ds, match, end) if labels is not None]

    def prom_instant(self, ds, expr, t):
        doc = self.call("query_prometheus", {"datasourceUid": ds["uid"], "expr": expr,
                                             "queryType": "instant", "endTime": iso(t)})
        data = doc.get("data", doc) if isinstance(doc, dict) else doc
        if isinstance(data, dict):
            data = data.get("result", [])
        if isinstance(data, list) and len(data) == 2 and not isinstance(data[0], dict):
            return [(None, float(data[1]))]  # a scalar: [time, "value"]
        out = []
        for s in data or []:
            if isinstance(s, dict) and s.get("value"):
                out.append((s.get("metric") or {}, float(s["value"][1])))
        return out

    def close(self):
        self.client.close()


def strings(doc):
    """A list of strings from a tool that may wrap its list in an object."""
    if isinstance(doc, dict):
        doc = next((v for v in doc.values() if isinstance(v, list)), [])
    return [str(x) for x in doc or []]


def mcp_selector(match):
    """A PromQL selector like up{job=~"api|web"} as mcp-grafana's label filters, or None
    when it's something more than a selector."""
    if not match:
        return None
    m = re.fullmatch(r"\s*([A-Za-z_:][\w:]*)?\s*(?:\{(.*)\})?\s*", match, re.S)
    if not m:
        return None
    filters = [{"name": "__name__", "value": m.group(1), "type": "="}] if m.group(1) else []
    body = (m.group(2) or "").strip().rstrip(",")
    for lm in re.finditer(r'\s*(\w+)\s*(=~|!~|!=|=)\s*"((?:[^"\\]|\\.)*)"\s*,?', body):
        filters.append({"name": lm.group(1), "value": lm.group(3).replace('\\"', '"'),
                        "type": lm.group(2)})
    return {"filters": filters} if filters else None


def connect(args, link_base=None):
    """The way in to Grafana: a flag, then an environment variable, then gcx."""
    token = os.environ.get("GRAFANA_TOKEN") or os.environ.get("GRAFANA_SERVICE_ACCOUNT_TOKEN")
    if args.mcp:
        return McpGrafana(args.mcp)
    if args.gcx:
        return RestGrafana(gcx_request(args.gcx_context), "gcx")
    url = args.grafana or os.environ.get("GRAFANA_URL")
    if url:
        return RestGrafana(http_request(url, token), urlsplit(url).netloc or url)
    if os.environ.get("GRAFANA_MCP_COMMAND"):
        return McpGrafana(os.environ["GRAFANA_MCP_COMMAND"])
    if link_base and token:
        return RestGrafana(http_request(link_base, token), urlsplit(link_base).netloc)
    if shutil.which("gcx"):
        return RestGrafana(gcx_request(args.gcx_context), "gcx")
    if link_base:
        return RestGrafana(http_request(link_base), urlsplit(link_base).netloc)
    sys.exit("dashboard: no way in to Grafana. Set GRAFANA_URL and GRAFANA_TOKEN, or "
             "GRAFANA_MCP_COMMAND, or install gcx and log in (see --help)")


# --- data ------------------------------------------------------------------------------
#
# Query results come back as Grafana's data frames: a frame is a table of fields
# (columns), each with a name, a type, maybe labels, and its values. Panels draw from
# these, whichever way in they came.

class Field:
    def __init__(self, name, type, values, labels=None, config=None):
        self.name, self.type, self.values = name, type, values
        self.labels, self.config = labels or {}, config or {}


class Frame:
    def __init__(self, fields, name="", ref_id="", meta=None):
        self.fields, self.name, self.ref_id, self.meta = fields, name or "", ref_id or "", meta or {}

    def time_field(self):
        return next((f for f in self.fields if f.type == "time"), None)

    def length(self):
        return max((len(f.values) for f in self.fields), default=0)


def read_frames(frames):
    """Grafana's frame JSON -> [Frame]. Times are turned into unix seconds."""
    out = []
    for fr in frames or []:
        schema, data = fr.get("schema") or {}, fr.get("data") or {}
        values, entities = data.get("values") or [], data.get("entities") or []
        fields = []
        for i, fs in enumerate(schema.get("fields") or []):
            vals = list(values[i]) if i < len(values) and values[i] is not None else []
            # JSON can't say NaN or Infinity, so the frame lists where they go.
            ent = entities[i] if i < len(entities) else None
            for key, special in (("NaN", math.nan), ("Inf", math.inf), ("NegInf", -math.inf)):
                for j in (ent or {}).get(key) or []:
                    if j < len(vals):
                        vals[j] = special
            kind = fs.get("type") or "other"
            if kind == "time":
                vals = [v / 1000 if isinstance(v, (int, float)) else None for v in vals]
            fields.append(Field(fs.get("name") or "", kind, vals, fs.get("labels"), fs.get("config")))
        out.append(Frame(fields, schema.get("name"), schema.get("refId"), schema.get("meta")))
    return out


def format_labels(labels, skip_name=True):
    inner = ", ".join(f'{k}="{v}"' for k, v in sorted(labels.items())
                      if not (skip_name and k == "__name__"))
    return f"{{{inner}}}" if inner else ""


def legend_name(metric, legend, expr):
    """A Prometheus series' name the way Grafana makes it: legendFormat's {{label}}
    filled in, or else the metric name and its labels."""
    if legend and legend != "__auto":
        return re.sub(r"\{\{\s*([^}\s]+)\s*\}\}", lambda m: str(metric.get(m.group(1), "")), legend)
    name = metric.get("__name__", "")
    labels = format_labels(metric)
    return (name + labels) or expr or "Value"


def prom_frames(doc, ref, legend=None, expr=""):
    """Prometheus-style JSON (a matrix or a vector) -> frames, one per series, the way
    Grafana's own Prometheus datasource makes them."""
    if isinstance(doc, dict):
        doc = doc.get("data", doc)
    if isinstance(doc, dict):
        doc = doc.get("result", [])
    out = []
    for s in doc or []:
        if not isinstance(s, dict):
            continue
        pts = s.get("values") or ([s["value"]] if s.get("value") else [])
        metric = s.get("metric") or {}
        times = [float(t) for t, _ in pts]
        vals = []
        for _, v in pts:
            try:
                vals.append(float(v))
            except (TypeError, ValueError):
                vals.append(None)
        out.append(Frame([Field("Time", "time", times),
                          Field("Value", "number", vals, metric,
                                {"displayNameFromDS": legend_name(metric, legend, expr)})],
                         ref_id=ref))
    return out


# --- what Grafana's field config says ---------------------------------------------------
#
# A panel's fieldConfig has defaults (unit, decimals, min, max, thresholds, colors,
# value mappings, and the panel type's own "custom" settings) and overrides that apply
# to fields matching a rule. These turn that into the settings for one field.

def merge(into, more):
    """Deep-merge dict more into dict into."""
    for k, v in (more or {}).items():
        if isinstance(v, dict) and isinstance(into.get(k), dict):
            merge(into[k], v)
        else:
            into[k] = copy.deepcopy(v)
    return into


def set_path(conf, path, value):
    """conf["custom"]["lineWidth"] = value, for path "custom.lineWidth"."""
    keys = path.split(".")
    for k in keys[:-1]:
        conf = conf.setdefault(k, {})
        if not isinstance(conf, dict):
            return
    if value is None:
        conf.pop(keys[-1], None)
    else:
        conf[keys[-1]] = copy.deepcopy(value)


def field_name(field, frame, frames):
    """A field's display name before any displayName setting, the way Grafana names
    one, give or take: the frame's name, the field's, and the labels that tell it apart."""
    if field.config.get("displayNameFromDS"):
        return field.config["displayNameFromDS"]
    parts = []
    values_in_frame = sum(1 for f in frame.fields if f.type != "time")
    if frame.name and (values_in_frame == 1 or field.name in ("Value", "")):
        parts.append(frame.name)
    if field.name and field.name != "Value" and field.name != frame.name:
        parts.append(field.name)
    if field.labels:
        keys = {k for fr in frames for f in fr.fields for k in f.labels}
        if len(keys) == 1 and len(field.labels) == 1:
            parts.append(str(next(iter(field.labels.values()))))
        else:
            parts.append(format_labels(field.labels, skip_name=False))
    return " ".join(parts) or field.name or "Value"


def matches(matcher, name, field, frame):
    kind, opt = (matcher or {}).get("id"), (matcher or {}).get("options")
    if kind == "byName":
        return opt in (name, field.name)
    if kind == "byRegexp":
        # Written as a JavaScript regex, maybe with its slashes and flags: /.*Tx.*/i
        m = re.fullmatch(r"/(.*)/([a-z]*)", str(opt), re.S)
        try:
            rx = re.compile(m.group(1), re.I if "i" in m.group(2) else 0) if m else re.compile(str(opt))
            return bool(rx.fullmatch(name) if not m else rx.search(name))
        except re.error:
            return False
    if kind == "byFrameRefID":
        return opt == frame.ref_id
    if kind == "byType":
        return opt == field.type
    if kind == "byNames":
        names = (opt or {}).get("names") or []
        hit = name in names or field.name in names
        return not hit if (opt or {}).get("mode") == "exclude" else hit
    return False


def field_template(text, name, field, frame):
    """displayName settings can say ${__field.labels.instance} and friends."""
    def sub(m):
        key = m.group(1)
        if key.startswith("__field.labels."):
            return str(field.labels.get(key[len("__field.labels."):], ""))
        return {"__field.name": field.name, "__series.name": frame.name or name,
                "__field.displayName": name, "__field.labels": format_labels(field.labels),
                "__field.displayNameFromDS": field.config.get("displayNameFromDS", "")}.get(key, m.group(0))
    return re.sub(r"\$\{([\w.]+)(?::\w+)?\}", sub, text)


# The color mode a panel type uses when its dashboard doesn't say: what Grafana's own
# panel plugins default to. Anything else defaults to thresholds.
DEFAULT_COLOR_MODE = {"timeseries": "palette-classic", "graph": "palette-classic",
                      "trend": "palette-classic", "barchart": "palette-classic",
                      "piechart": "palette-classic", "histogram": "palette-classic",
                      "xychart": "palette-classic", "state-timeline": "continuous-GrYlRd",
                      "status-history": "continuous-GrYlRd"}


def field_config(panel, field, frame, frames):
    """(the settings for one field, its display name): what the datasource said about
    it, then the panel's defaults, then each override that matches it."""
    fc = panel.get("fieldConfig") or {}
    conf = merge({k: v for k, v in field.config.items()}, fc.get("defaults") or {})
    if not (conf.get("color") or {}).get("mode"):
        conf.setdefault("color", {})["mode"] = DEFAULT_COLOR_MODE.get(panel.get("type"), "thresholds")
    name = field_name(field, frame, frames)
    for ov in fc.get("overrides") or []:
        if matches(ov.get("matcher"), name, field, frame):
            for prop in ov.get("properties") or []:
                if prop.get("id"):
                    set_path(conf, prop["id"], prop.get("value"))
    if conf.get("displayName"):
        name = field_template(str(conf["displayName"]), name, field, frame)
    return conf, name


def threshold_color(conf, value, theme, lo=None, hi=None):
    """The color of the threshold step value falls in. Percentage thresholds are of the
    field's min..max, or else of the data's range (lo..hi), or else of 0..100."""
    th = conf.get("thresholds") or {}
    steps = th.get("steps") or [{"value": None, "color": "green"}, {"value": 80, "color": "red"}]
    v = value
    if th.get("mode") == "percentage" and value is not None:
        lo = conf["min"] if conf.get("min") is not None else lo
        hi = conf["max"] if conf.get("max") is not None else hi
        if lo is None or hi is None or hi == lo:
            lo, hi = 0, 100
        v = (value - lo) / (hi - lo) * 100
    color = steps[0].get("color")
    for st in steps:
        sv = st.get("value")
        if sv is None or (v is not None and isinstance(sv, (int, float)) and v >= sv):
            color = st.get("color")
    return theme.color(color)


CONTINUOUS = {  # Grafana's continuous color schemes, as stops
    "continuous-GrYlRd": ["green", "yellow", "red"], "continuous-RdYlGr": ["red", "yellow", "green"],
    "continuous-BlYlRd": ["blue", "yellow", "red"], "continuous-YlRd": ["yellow", "red"],
    "continuous-BlPu": ["blue", "purple"], "continuous-YlBl": ["yellow", "blue"],
    "continuous-blues": ["super-light-blue", "dark-blue"], "continuous-reds": ["super-light-red", "dark-red"],
    "continuous-greens": ["super-light-green", "dark-green"],
    "continuous-purples": ["super-light-purple", "dark-purple"],
}


def value_color(conf, value, theme, index=0, lo=None, hi=None):
    """A field's color for a value, by its color mode: thresholds, a fixed color, the
    series palette, or a continuous scheme between min and max."""
    mode = (conf.get("color") or {}).get("mode") or "thresholds"
    if mode in ("fixed", "shades"):
        return theme.color((conf.get("color") or {}).get("fixedColor"), index)
    if mode == "thresholds":
        return threshold_color(conf, value, theme, lo, hi)
    if mode in CONTINUOUS and value is not None:
        lo = conf.get("min", lo if lo is not None else 0)
        hi = conf.get("max", hi if hi is not None else 100)
        t = 0.0 if hi == lo else min(1.0, max(0.0, (value - lo) / (hi - lo)))
        stops = [theme.color(c) for c in CONTINUOUS[mode]]
        k = min(len(stops) - 2, int(t * (len(stops) - 1)))
        return mix(stops[k], stops[k + 1], t * (len(stops) - 1) - k)
    return theme.series[index % len(theme.series)]


def map_value(conf, value):
    """A value mapping's {text, color} for value, or None: "1" -> "UP", 0-50 -> "low",
    null -> "N/A", and so on."""
    for m in conf.get("mappings") or []:
        kind, opt = m.get("type"), m.get("options") or {}
        if kind == "value":
            key = (str(int(value)) if isinstance(value, float) and value.is_integer()
                   else str(value).lower() if isinstance(value, bool) else str(value))
            if key in opt:
                return opt[key]
        elif kind == "range" and isinstance(value, (int, float)) and value == value:
            lo, hi = opt.get("from"), opt.get("to")
            if (lo is None or value >= lo) and (hi is None or value <= hi):
                return opt.get("result") or {}
        elif kind == "regex" and value is not None:
            try:
                if re.search(str(opt.get("pattern", "")), str(value)):
                    return opt.get("result") or {}
            except re.error:
                pass
        elif kind == "special":
            match = opt.get("match")
            hit = ((match in ("null", "null+nan") and value is None)
                   or (match in ("nan", "null+nan") and isinstance(value, float) and value != value)
                   or (match == "true" and value is True) or (match == "false" and value is False)
                   or (match == "empty" and value == ""))
            if hit:
                return opt.get("result") or {}
    return None


def numbers(values):
    return [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v)]


def reduce_values(values, calc):
    """One number from a field's values, the way Grafana's reducers make it: lastNotNull,
    mean, max, ... None when there's nothing to make it from."""
    nums = numbers(values)
    if calc == "last":
        v = values[-1] if values else None
        return v if isinstance(v, (int, float)) else None
    if calc == "first":
        v = values[0] if values else None
        return v if isinstance(v, (int, float)) else None
    if calc == "count":
        return float(len(values))
    if not nums:
        return None
    if calc in ("lastNotNull", "current"):
        return nums[-1]
    if calc in ("firstNotNull",):
        return nums[0]
    if calc == "min":
        return min(nums)
    if calc == "max":
        return max(nums)
    if calc in ("mean", "avg"):
        return sum(nums) / len(nums)
    if calc in ("sum", "total"):
        return sum(nums)
    if calc == "range":
        return max(nums) - min(nums)
    if calc == "diff":
        return nums[-1] - nums[0]
    if calc == "diffperc":
        return (nums[-1] - nums[0]) / nums[0] * 100 if nums[0] else None
    if calc == "delta":  # increase, counting a drop as a counter reset
        total = 0.0
        for a, b in zip(nums, nums[1:]):
            total += b - a if b >= a else b
        return total
    if calc == "median":
        s = sorted(nums)
        n = len(s)
        return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
    if calc in ("stdDev", "variance"):
        mean = sum(nums) / len(nums)
        var = sum((x - mean) ** 2 for x in nums) / len(nums)
        return math.sqrt(var) if calc == "stdDev" else var
    if calc == "distinctCount":
        return float(len(set(nums)))
    if calc == "changeCount":
        return float(sum(1 for a, b in zip(nums, nums[1:]) if a != b))
    if calc == "step":
        return min((b - a for a, b in zip(nums, nums[1:])), default=None)
    return nums[-1]


CALC_NAMES = {"lastNotNull": "Last *", "last": "Last", "firstNotNull": "First *", "first": "First",
              "min": "Min", "max": "Max", "mean": "Mean", "sum": "Total", "count": "Count",
              "range": "Range", "delta": "Delta", "diff": "Difference", "median": "Median",
              "stdDev": "StdDev", "variance": "Variance", "diffperc": "Difference %",
              "distinctCount": "Distinct", "changeCount": "Changes"}


# --- units: Grafana's value formats ---------------------------------------------
#
# Every formatter gives (text, suffix): the number, with any prefix such as "$", and
# the unit, with the leading space Grafana puts there, so a stat panel can draw it
# smaller.

LN10 = 2.302585092994046  # JavaScript's Math.LN10, so digit counts match Grafana
INF = float("inf")

SI_PREFIXES = ["f", "p", "n", "µ", "m", "", "k", "M", "G", "T", "P", "E", "Z", "Y"]
SI_BASE = SI_PREFIXES.index("")
BIN_PREFIXES = ["", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi", "Yi"]
ALIASES = {"farenheit": "fahrenheit"}  # the old spelling still works


# --- numbers -----------------------------------------------------------------

def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _pow(base, exp):
    """base ** exp as a float. Overflow gives infinity, as in JavaScript."""
    try:
        return float(base) ** exp
    except OverflowError:
        return INF


def _round(x):
    """JavaScript's Math.round: halves go up, even for negatives."""
    f = math.floor(x)
    return f + 1 if x - f >= 0.5 else f


def _digits(x):
    """The shortest digits that name a positive number, and where the point goes.

    Returns (digits, point), meaning 0.<digits> times 10 to the power of point.
    """
    mant, _, exp = repr(x).partition("e")
    int_part, _, frac = mant.partition(".")
    digits = int_part + frac
    point = len(int_part) + int(exp or 0)
    stripped = digits.lstrip("0")
    point -= len(digits) - len(stripped)
    return stripped.rstrip("0") or "0", point


def _num_str(x):
    """JavaScript's String(number), with its rules for when to use an exponent."""
    if x != x:
        return "NaN"
    if x in (INF, -INF):
        return "Infinity" if x > 0 else "-Infinity"
    if x == 0:
        return "0"
    sign = "-" if x < 0 else ""
    digits, n = _digits(abs(x))
    k = len(digits)
    if k <= n <= 21:
        body = digits + "0" * (n - k)
    elif 0 < n <= 21:
        body = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        body = "0." + "0" * -n + digits
    else:
        e = n - 1
        mantissa = digits if k == 1 else digits[0] + "." + digits[1:]
        body = mantissa + "e" + ("+" if e >= 0 else "-") + str(abs(e))
    return sign + body


def _auto_decimals(value):
    """How many decimals a value gets when none are asked for: about three digits."""
    if value % 1 == 0 or value == 0:
        return 0
    absv = abs(value)
    log10 = math.floor(math.log(absv) / LN10)
    dec = -log10 + 1
    norm = absv / _pow(10, -dec)
    if norm > 2.25:
        dec += 1
    return max(0, dec)


def _to_fixed(value, decimals=None):
    """The number with `decimals` places, or as many as it needs."""
    if decimals is None:
        decimals = _auto_decimals(value)
    else:
        decimals = _clamp(int(decimals), 0, 100)
    if value == 0:
        return "%.*f" % (decimals, 0.0)
    factor = _pow(10, max(0, decimals)) if decimals else 1.0
    scaled = value * factor
    if scaled in (INF, -INF) or scaled != scaled:
        rounded = scaled
    else:
        rounded = float(_round(scaled))
    formatted = _num_str(rounded / factor)
    if "e" in formatted:
        return formatted
    dot = formatted.find(".")
    precision = 0 if dot == -1 else len(formatted) - dot - 1
    if precision < decimals:
        return (formatted if precision else formatted + ".") + "0" * (decimals - precision)
    return formatted


def _plain(value, decimals, unit):
    return _to_fixed(value, decimals), unit


def _scaled_unit(value, decimals, unit):
    """A value already divided down, with its unit. Plural for more than one."""
    suffix = unit
    if abs(value) > 1 and unit in (" min", " hour", " day", " week", " year"):
        suffix = unit + "s"
    return _to_fixed(value, decimals), suffix


# --- scaling units: k, M, Ki, and so on -------------------------------------

def _scaled(factor, ext, offset=0):
    """Divide by `factor` until the number is small, and name the step taken."""
    def fmt(value, decimals=None):
        si = 0 if value == 0 else math.floor(math.log10(abs(value)) / math.log10(factor))
        suffix = ext[_clamp(offset + si, 0, len(ext) - 1)]
        scale = _pow(factor, _clamp(si, -offset, len(ext) - offset - 1))
        return _to_fixed(value / scale, decimals), suffix
    return fmt


def _si(unit, offset=0):
    """Powers of 1000 around the unit: 1.23 mF, 2 kW. offset 2 starts at k."""
    return _scaled(1000, [" " + p + unit for p in SI_PREFIXES], SI_BASE + offset)


def _bin(unit, offset=0):
    """Powers of 1024: 1.50 GiB. offset 3 starts at Gi."""
    return _scaled(1024, [" " + p + unit for p in BIN_PREFIXES], offset)


def _count(symbol):
    """Counts: 1.23M req/s, with K, M, B and T."""
    scaler = _scaled(1000, ["", "K", "M", "B", "T"])

    def fmt(value, decimals=None):
        text, suffix = scaler(value, decimals)
        return text, suffix + " " + symbol
    return fmt


# --- plain units and the arithmetic formats -----------------------------------

def _fixed(unit):
    """A number with its unit after it, like 42 ms."""
    def fmt(value, decimals=None):
        return _to_fixed(value, decimals), (" " + unit if unit else "")
    return fmt


def _bool(true_text, false_text):
    def fmt(value, decimals=None):
        return (true_text if value else false_text), ""
    return fmt


def _percent(value, decimals=None):
    """0 to 100, already a percentage."""
    return _to_fixed(value, decimals), "%"


def _percent_unit(value, decimals=None):
    """0.0 to 1.0, shown as a percentage."""
    return _to_fixed(100 * value, decimals), "%"


def _ordinal(value, decimals=None):
    """1st, 2nd, 3rd: whole numbers only, English rules."""
    n = int(_round(value))
    a = abs(n)
    if a % 10 == 1 and a % 100 != 11:
        suffix = "st"
    elif a % 10 == 2 and a % 100 != 12:
        suffix = "nd"
    elif a % 10 == 3 and a % 100 != 13:
        suffix = "rd"
    else:
        suffix = "th"
    return str(n), suffix


def _sci(value, decimals=None):
    """Scientific notation, 1e+3. Without decimals, as many digits as needed."""
    sign = "-" if value < 0 else ""
    if decimals is None and value != 0:
        digits, point = _digits(abs(value))
        mantissa = digits if len(digits) == 1 else digits[0] + "." + digits[1:]
        exp = point - 1
    elif value == 0:
        mantissa, exp = "0", 0
    else:
        raw = "%.*e" % (_clamp(int(decimals), 0, 100), abs(value))
        mantissa, _, exp = raw.partition("e")
        exp = int(exp)
    return sign + mantissa + "e" + ("+" if exp >= 0 else "-") + str(abs(exp)), ""


def _string(value, decimals=None):
    return _num_str(value), ""


# --- money -------------------------------------------------------------------

def _currency(symbol, as_suffix):
    """Money, scaled: $1.53K. The symbol goes in front, or after with as_suffix."""
    scaler = _scaled(1000, ["", "K", "M", "B", "T"])

    def fmt(value, decimals=None):
        negative = value < 0
        text, suffix = scaler(abs(value), decimals)
        if as_suffix:
            prefix, suffix = "", suffix + symbol
        else:
            prefix = symbol
        if negative:
            prefix = "-" + prefix
        return prefix + text, suffix
    return fmt


def _full_currency(symbol, as_suffix):
    """Money, not scaled: $1,234,567. Commas, and two decimals if asked for."""
    def fmt(value, decimals=None):
        if decimals is None:
            text = format(Decimal(repr(value)).quantize(Decimal("0.1"), ROUND_HALF_UP), ",f")
            if "." in text:
                text = text.rstrip("0").rstrip(".")
        else:
            places = _clamp(int(decimals), 0, 100)
            quantum = Decimal(1).scaleb(-places)
            text = format(Decimal(repr(value)).quantize(quantum, ROUND_HALF_UP), ",f")
        if as_suffix:
            return text, symbol
        return symbol + text, ""
    return fmt


# --- time --------------------------------------------------------------------

def _duration(size, decimals, scale):
    """Grafana's toDuration: "1 day, 12 hours, 0 minutes", from a count of `scale`."""
    units = [("year", 31536000), ("month", 2592000), ("week", 604800), ("day", 86400),
             ("hour", 3600), ("minute", 60), ("second", 1), ("millisecond", 0.001)]
    intervals = dict(units)
    if size == 0:
        return "0", " " + scale + "s"
    if size < 0:
        text, suffix = _duration(-size, decimals, scale)
        return text, (suffix or "") + " ago"

    size *= intervals[scale] * 1000  # to milliseconds, so the arithmetic stays exact
    strings = []
    started = False  # once one unit is printed, the smaller ones follow
    left = 0 if decimals is None else int(decimals)  # how many more units to print
    i = 0
    while i < len(units) and left >= 0:
        name, seconds = units[i]
        interval = seconds * 1000
        value = size / interval
        if value >= 1 or started:
            started = True
            whole = math.floor(value)
            strings.append("%d %s%s" % (whole, name, "s" if whole != 1 else ""))
            size = math.fmod(size, interval)
            left -= 1
        i += 1

    if not strings:
        # Under a millisecond: show the fraction, so it isn't blank.
        value = _to_fixed(size, decimals)
        if float(value) == 0:
            value = _to_fixed(size)
        strings.append("%s %s" % (value, "millisecond" if float(value) == 1 else "milliseconds"))
    return ", ".join(strings), ""


def _duration_ms(size, decimals=None):
    return _duration(size, decimals, "millisecond")


def _duration_s(size, decimals=None):
    return _duration(size, decimals, "second")


def _hms(size):
    """00:00:01, hours, minutes and seconds with no days."""
    hours = math.floor(size / 3600)
    minutes = math.floor(math.fmod(size, 3600) / 60)
    seconds = math.floor(math.fmod(math.fmod(size, 3600), 60))
    return "%02d:%02d:%02d" % (hours, minutes, seconds)


def _duration_hms(size, decimals=None):
    if size < 0:
        text, suffix = _duration_hms(-size)
        return text, suffix + " ago"
    return _hms(size), ""


def _duration_dhms(size, decimals=None):
    if size < 0:
        text, suffix = _duration_dhms(-size)
        return text, suffix + " ago"
    days = math.floor(size / 86400)
    day_text = "%d d " % days if days > 0 else ""
    return day_text + _hms(size - days * 86400), ""


def _clock(size, decimals=None):
    """Grafana's clock: 59s:999ms, 59m:59s, or 1h:59m:59s:999ms past an hour."""
    ms = int(size)  # JavaScript's Date drops the fraction too
    secs, msec = divmod(ms, 1000)
    if size < 1000:
        return "%03dms" % msec, ""
    if size < 60000:
        if decimals == 0:
            return "%02ds" % (secs % 60), ""
        return "%02ds:%03dms" % (secs % 60, msec), ""
    minutes, seconds = (secs // 60) % 60, secs % 60
    if size < 3600000:
        if decimals == 0:
            return "%02dm" % minutes, ""
        if decimals == 1:
            return "%02dm:%02ds" % (minutes, seconds), ""
        return "%02dm:%02ds:%03dms" % (minutes, seconds, msec), ""
    hours = "%dh" % math.floor(size / 3600000)
    if decimals == 0:
        return hours, ""
    if decimals == 1:
        return hours + ":%02dm" % minutes, ""
    if decimals == 2:
        return hours + ":%02dm:%02ds" % (minutes, seconds), ""
    return hours + ":%02dm:%02ds:%03dms" % (minutes, seconds, msec), ""


def _nanoseconds(size, decimals=None):
    a = abs(size)
    if a < 1000:
        return _plain(size, decimals, " ns")
    if a < 1000000:
        return _scaled_unit(size / 1000, decimals, " µs")
    if a < 1000000000:
        return _scaled_unit(size / 1000000, decimals, " ms")
    if a < 60000000000:
        return _scaled_unit(size / 1000000000, decimals, " s")
    if a < 3600000000000:
        return _scaled_unit(size / 60000000000, decimals, " min")
    if a < 86400000000000:
        return _scaled_unit(size / 3600000000000, decimals, " hour")
    return _scaled_unit(size / 86400000000000, decimals, " day")


def _microseconds(size, decimals=None):
    a = abs(size)
    if a < 1000:
        return _plain(size, decimals, " µs")
    if a < 1000000:
        return _scaled_unit(size / 1000, decimals, " ms")
    return _scaled_unit(size / 1000000, decimals, " s")


def _milliseconds(size, decimals=None):
    a = abs(size)
    if a < 1000:
        return _plain(size, decimals, " ms")
    if a < 60000:
        return _scaled_unit(size / 1000, decimals, " s")
    if a < 3600000:
        return _scaled_unit(size / 60000, decimals, " min")
    if a < 86400000:
        return _scaled_unit(size / 3600000, decimals, " hour")
    if a < 31536000000:
        return _scaled_unit(size / 86400000, decimals, " day")
    return _scaled_unit(size / 31536000000, decimals, " year")


def _seconds(size, decimals=None):
    a = abs(size)
    if size == 0:
        return "0", " s"
    if a < 0.000001:
        return _scaled_unit(size * 1e9, decimals, " ns")
    if a < 0.001:
        return _scaled_unit(size * 1e6, decimals, " µs")
    if a < 1:
        return _scaled_unit(size * 1e3, decimals, " ms")
    if a < 60:
        return _plain(size, decimals, " s")
    if a < 3600:
        return _scaled_unit(size / 60, decimals, " min")
    if a < 86400:
        return _scaled_unit(size / 3600, decimals, " hour")
    if a < 604800:
        return _scaled_unit(size / 86400, decimals, " day")
    if a < 31536000:
        return _scaled_unit(size / 604800, decimals, " week")
    return _scaled_unit(size / 3.15569e7, decimals, " year")


def _minutes(size, decimals=None):
    a = abs(size)
    if a < 60:
        return _plain(size, decimals, " min")
    if a < 1440:
        return _scaled_unit(size / 60, decimals, " hour")
    if a < 10080:
        return _scaled_unit(size / 1440, decimals, " day")
    if a < 604800:
        return _scaled_unit(size / 10080, decimals, " week")
    return _scaled_unit(size / 525948, decimals, " year")


def _hours(size, decimals=None):
    a = abs(size)
    if a < 24:
        return _plain(size, decimals, " hour")
    if a < 168:
        return _scaled_unit(size / 24, decimals, " day")
    if a < 8760:
        return _scaled_unit(size / 168, decimals, " week")
    return _scaled_unit(size / 8760, decimals, " year")


def _days(size, decimals=None):
    a = abs(size)
    if a < 7:
        return _scaled_unit(size, decimals, " day")
    if a < 365:
        return _scaled_unit(size / 7, decimals, " week")
    return _scaled_unit(size / 365, decimals, " year")


def _time_ticks(size, decimals=None):
    return _seconds(size / 100, decimals)


def _clock_ms(size, decimals=None):
    return _clock(size, decimals)


def _clock_s(size, decimals=None):
    return _clock(size * 1000, decimals)


# A date-time pattern, in moment.js style: YYYY-MM-DD HH:mm:ss.
_TOKEN = re.compile(r"\[([^\]]*)\]|\\(.)|(YYYY|YY|MMMM|MMM|MM|M|DD|Do|D|dddd|ddd|dd|d"
                    r"|HH|H|hh|h|mm|m|ss|s|SSS|a|A|X)")
_WEEKDAYS_SHORT = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"]


def _format_time(ms, pattern):
    """Format epoch milliseconds in local time, like moment's format()."""
    secs, msec = divmod(int(ms), 1000)  # floor, so negative times land as moment does
    try:
        t = datetime.datetime.fromtimestamp(secs)
    except (OverflowError, OSError, ValueError):
        return "Invalid date"
    h12 = t.hour % 12 or 12
    weekday = (t.weekday() + 1) % 7
    values = {
        "YYYY": "%04d" % t.year, "YY": "%02d" % (t.year % 100),
        "MMMM": t.strftime("%B"), "MMM": t.strftime("%b"),
        "MM": "%02d" % t.month, "M": str(t.month),
        "DD": "%02d" % t.day, "D": str(t.day), "Do": str(t.day) + _ordinal(t.day)[1],
        "dddd": t.strftime("%A"), "ddd": t.strftime("%a"),
        "dd": _WEEKDAYS_SHORT[weekday], "d": str(weekday),
        "HH": "%02d" % t.hour, "H": str(t.hour),
        "hh": "%02d" % h12, "h": str(h12),
        "mm": "%02d" % t.minute, "m": str(t.minute),
        "ss": "%02d" % t.second, "s": str(t.second),
        "SSS": "%03d" % msec,
        "a": "am" if t.hour < 12 else "pm", "A": "AM" if t.hour < 12 else "PM",
        "X": str(secs),
    }

    def sub(m):
        if m.group(1) is not None:
            return m.group(1)
        if m.group(2) is not None:
            return m.group(2)
        return values[m.group(3)]
    return _TOKEN.sub(sub, pattern)


def _date_format(pattern, today_pattern=None):
    """A date-time in local time. With today_pattern, today gets that shorter one."""
    def fmt(value, decimals=None):
        if today_pattern is not None:
            try:
                same_day = datetime.datetime.fromtimestamp(int(value) // 1000).date() \
                    == datetime.date.today()
            except (OverflowError, OSError, ValueError):
                same_day = False
            if same_day:
                return _format_time(value, today_pattern), ""
        return _format_time(value, pattern), ""
    return fmt


def _time_ago(value, decimals=None):
    """Moment's English fromNow: a few seconds ago, 7 days ago, in 2 hours."""
    diff = value - time.time() * 1000
    size = abs(diff)
    secs = _round(size / 1000)
    mins = _round(size / 60000)
    hours = _round(size / 3600000)
    days = _round(size / 86400000)
    months = _round(days * 4800 / 146097)  # moment's days-to-months
    if secs < 45:
        text = "a few seconds"
    elif mins <= 1:
        text = "a minute"
    elif mins < 45:
        text = "%d minutes" % mins
    elif hours <= 1:
        text = "an hour"
    elif hours < 22:
        text = "%d hours" % hours
    elif days <= 1:
        text = "a day"
    elif days < 26:
        text = "%d days" % days
    elif months <= 1:
        text = "a month"
    elif months < 11:
        text = "%d months" % months
    else:
        years = _round(days / 365)
        text = "a year" if years <= 1 else "%d years" % years
    return (("in " + text) if diff > 0 else (text + " ago")), ""


# --- the table of unit ids -----------------------------------------------------

# Grafana's special formatters, by the name the table uses.
SPECIAL_UNITS = {
    "stringFormater": _string,
    "toPercent": _percent,
    "toPercentUnit": _percent_unit,
    "toOrdinal": _ordinal,
    "sci": _sci,
    "dateTimeAsIso": _date_format("YYYY-MM-DD HH:mm:ss"),
    "dateTimeAsIsoNoDateIfToday": _date_format("YYYY-MM-DD HH:mm:ss", "HH:mm:ss"),
    "dateTimeAsUS": _date_format("MM/DD/YYYY h:mm:ss a"),
    "dateTimeAsUSNoDateIfToday": _date_format("MM/DD/YYYY h:mm:ss a", "h:mm:ss a"),
    "dateTimeSystemFormatter": _date_format("YYYY-MM-DD HH:mm:ss"),
    "dateTimeFromNow": _time_ago,
    "toNanoSeconds": _nanoseconds,
    "toMicroSeconds": _microseconds,
    "toMilliSeconds": _milliseconds,
    "toSeconds": _seconds,
    "toMinutes": _minutes,
    "toHours": _hours,
    "toDays": _days,
    "toDurationInMilliseconds": _duration_ms,
    "toDurationInSeconds": _duration_s,
    "toDurationInHoursMinutesSeconds": _duration_hms,
    "toDurationInDaysHoursMinutesSeconds": _duration_dhms,
    "toTimeTicks": _time_ticks,
    "toClockMilliseconds": _clock_ms,
    "toClockSeconds": _clock_s,
}

# Grafana's unit id: (display name, how to format it).
UNIT_FORMATS = {
    "none": ("Number", ('fixed', '')),
    "string": ("String", ('special', 'stringFormater')),
    "short": ("short", ('scaled', 1000, ('', ' K', ' Mil', ' Bil', ' Tri', ' Quadr', ' Quint', ' Sext', ' Sept'))),
    "sishort": ("SI short", ('si', '', 0)),
    "percent": ("Percent (0-100)", ('special', 'toPercent')),
    "percentunit": ("Percent (0.0-1.0)", ('special', 'toPercentUnit')),
    "humidity": ("Humidity (%H)", ('fixed', '%H')),
    "dB": ("Decibel", ('fixed', 'dB')),
    "candela": ("Candela (cd)", ('si', 'cd', 0)),
    "sci": ("Scientific notation", ('special', 'sci')),
    "ordinal": ("Ordinal (1st, 2nd, 3rd)", ('special', 'toOrdinal')),
    "pixel": ("Pixels", ('fixed', 'px')),
    "accMS2": ("Meters/sec²", ('fixed', 'm/sec²')),
    "accFS2": ("Feet/sec²", ('fixed', 'f/sec²')),
    "accG": ("G unit", ('fixed', 'g')),
    "degree": ("Degrees (°)", ('fixed', '°')),
    "radian": ("Radians", ('fixed', 'rad')),
    "grad": ("Gradian", ('fixed', 'grad')),
    "arcmin": ("Arc Minutes", ('fixed', 'arcmin')),
    "arcsec": ("Arc Seconds", ('fixed', 'arcsec')),
    "areaM2": ("Square Meters (m²)", ('fixed', 'm²')),
    "areaF2": ("Square Feet (ft²)", ('fixed', 'ft²')),
    "areaMI2": ("Square Miles (mi²)", ('fixed', 'mi²')),
    "acres": ("Acres (ac)", ('fixed', 'ac')),
    "hectares": ("Hectares (ha)", ('fixed', 'ha')),
    "flops": ("FLOP/s", ('si', 'FLOPS', 0)),
    "mflops": ("MFLOP/s", ('si', 'FLOPS', 2)),
    "gflops": ("GFLOP/s", ('si', 'FLOPS', 3)),
    "tflops": ("TFLOP/s", ('si', 'FLOPS', 4)),
    "pflops": ("PFLOP/s", ('si', 'FLOPS', 5)),
    "eflops": ("EFLOP/s", ('si', 'FLOPS', 6)),
    "zflops": ("ZFLOP/s", ('si', 'FLOPS', 7)),
    "yflops": ("YFLOP/s", ('si', 'FLOPS', 8)),
    "ppm": ("parts-per-million (ppm)", ('fixed', 'ppm')),
    "conppb": ("parts-per-billion (ppb)", ('fixed', 'ppb')),
    "conngm3": ("nanogram per cubic meter (ng/m³)", ('fixed', 'ng/m³')),
    "conngNm3": ("nanogram per normal cubic meter (ng/Nm³)", ('fixed', 'ng/Nm³')),
    "conμgm3": ("microgram per cubic meter (μg/m³)", ('fixed', 'μg/m³')),
    "conμgNm3": ("microgram per normal cubic meter (μg/Nm³)", ('fixed', 'μg/Nm³')),
    "conmgm3": ("milligram per cubic meter (mg/m³)", ('fixed', 'mg/m³')),
    "conmgNm3": ("milligram per normal cubic meter (mg/Nm³)", ('fixed', 'mg/Nm³')),
    "congm3": ("gram per cubic meter (g/m³)", ('fixed', 'g/m³')),
    "congNm3": ("gram per normal cubic meter (g/Nm³)", ('fixed', 'g/Nm³')),
    "conmgdL": ("milligrams per decilitre (mg/dL)", ('fixed', 'mg/dL')),
    "conmmolL": ("millimoles per litre (mmol/L)", ('fixed', 'mmol/L')),
    "currencyUSD": ("Dollars ($)", ('currency', '$', False)),
    "currencyGBP": ("Pounds (£)", ('currency', '£', False)),
    "currencyEUR": ("Euro (€)", ('currency', '€', False)),
    "currencyJPY": ("Yen (¥)", ('currency', '¥', False)),
    "currencyRUB": ("Rubles (₽)", ('currency', '₽', False)),
    "currencyUAH": ("Hryvnias (₴)", ('currency', '₴', False)),
    "currencyBRL": ("Real (R$)", ('currency', 'R$', False)),
    "currencyDKK": ("Danish Krone (kr)", ('currency', 'kr', True)),
    "currencyISK": ("Icelandic Króna (kr)", ('currency', 'kr', True)),
    "currencyNOK": ("Norwegian Krone (kr)", ('currency', 'kr', True)),
    "currencySEK": ("Swedish Krona (kr)", ('currency', 'kr', True)),
    "currencyCZK": ("Czech koruna (czk)", ('currency', 'czk', False)),
    "currencyCHF": ("Swiss franc (CHF)", ('currency', 'CHF', False)),
    "currencyPLN": ("Polish Złoty (PLN)", ('currency', 'PLN', False)),
    "currencyBTC": ("Bitcoin (฿)", ('currency', '฿', False)),
    "currencymBTC": ("Milli Bitcoin (฿)", ('currency', 'mBTC', False)),
    "currencyμBTC": ("Micro Bitcoin (฿)", ('currency', 'μBTC', False)),
    "currencyZAR": ("South African Rand (R)", ('currency', 'R', False)),
    "currencyINR": ("Indian Rupee (₹)", ('currency', '₹', False)),
    "currencyKRW": ("South Korean Won (₩)", ('currency', '₩', False)),
    "currencyIDR": ("Indonesian Rupiah (Rp)", ('currency', 'Rp', False)),
    "currencyPHP": ("Philippine Peso (PHP)", ('currency', 'PHP', False)),
    "currencyVND": ("Vietnamese Dong (VND)", ('currency', 'đ', True)),
    "currencyTRY": ("Turkish Lira (₺)", ('currency', '₺', True)),
    "currencyMYR": ("Malaysian Ringgit (RM)", ('currency', 'RM', False)),
    "currencyXPF": ("CFP franc (XPF)", ('currency', 'XPF', False)),
    "currencyBGN": ("Bulgarian Lev (BGN)", ('currency', 'BGN', False)),
    "currencyPYG": ("Guaraní (₲)", ('currency', '₲', False)),
    "currencyUYU": ("Uruguay Peso (UYU)", ('currency', 'UYU', False)),
    "currencyILS": ("Israeli New Shekels (₪)", ('currency', '₪', False)),
    "bytes": ("bytes(IEC)", ('bin', 'B', 0)),
    "decbytes": ("bytes(SI)", ('si', 'B', 0)),
    "bits": ("bits(IEC)", ('bin', 'b', 0)),
    "decbits": ("bits(SI)", ('si', 'b', 0)),
    "kbytes": ("kibibytes", ('bin', 'B', 1)),
    "deckbytes": ("kilobytes", ('si', 'B', 1)),
    "mbytes": ("mebibytes", ('bin', 'B', 2)),
    "decmbytes": ("megabytes", ('si', 'B', 2)),
    "gbytes": ("gibibytes", ('bin', 'B', 3)),
    "decgbytes": ("gigabytes", ('si', 'B', 3)),
    "tbytes": ("tebibytes", ('bin', 'B', 4)),
    "dectbytes": ("terabytes", ('si', 'B', 4)),
    "pbytes": ("pebibytes", ('bin', 'B', 5)),
    "decpbytes": ("petabytes", ('si', 'B', 5)),
    "pps": ("packets/sec", ('si', 'p/s', 0)),
    "binBps": ("bytes/sec(IEC)", ('bin', 'B/s', 0)),
    "Bps": ("bytes/sec(SI)", ('si', 'B/s', 0)),
    "binbps": ("bits/sec(IEC)", ('bin', 'b/s', 0)),
    "bps": ("bits/sec(SI)", ('si', 'b/s', 0)),
    "KiBs": ("kibibytes/sec", ('bin', 'B/s', 1)),
    "Kibits": ("kibibits/sec", ('bin', 'b/s', 1)),
    "KBs": ("kilobytes/sec", ('si', 'B/s', 1)),
    "Kbits": ("kilobits/sec", ('si', 'b/s', 1)),
    "MiBs": ("mebibytes/sec", ('bin', 'B/s', 2)),
    "Mibits": ("mebibits/sec", ('bin', 'b/s', 2)),
    "MBs": ("megabytes/sec", ('si', 'B/s', 2)),
    "Mbits": ("megabits/sec", ('si', 'b/s', 2)),
    "GiBs": ("gibibytes/sec", ('bin', 'B/s', 3)),
    "Gibits": ("gibibits/sec", ('bin', 'b/s', 3)),
    "GBs": ("gigabytes/sec", ('si', 'B/s', 3)),
    "Gbits": ("gigabits/sec", ('si', 'b/s', 3)),
    "TiBs": ("tebibytes/sec", ('bin', 'B/s', 4)),
    "Tibits": ("tebibits/sec", ('bin', 'b/s', 4)),
    "TBs": ("terabytes/sec", ('si', 'B/s', 4)),
    "Tbits": ("terabits/sec", ('si', 'b/s', 4)),
    "PiBs": ("pebibytes/sec", ('bin', 'B/s', 5)),
    "Pibits": ("pebibits/sec", ('bin', 'b/s', 5)),
    "PBs": ("petabytes/sec", ('si', 'B/s', 5)),
    "Pbits": ("petabits/sec", ('si', 'b/s', 5)),
    "dateTimeAsIso": ("Datetime ISO", ('special', 'dateTimeAsIso')),
    "dateTimeAsIsoNoDateIfToday": ("Datetime ISO (No date if today)", ('special', 'dateTimeAsIsoNoDateIfToday')),
    "dateTimeAsUS": ("Datetime US", ('special', 'dateTimeAsUS')),
    "dateTimeAsUSNoDateIfToday": ("Datetime US (No date if today)", ('special', 'dateTimeAsUSNoDateIfToday')),
    "dateTimeAsSystem": ("Datetime default", ('special', 'dateTimeSystemFormatter')),
    "dateTimeFromNow": ("From Now", ('special', 'dateTimeFromNow')),
    "watt": ("Watt (W)", ('si', 'W', 0)),
    "kwatt": ("Kilowatt (kW)", ('si', 'W', 1)),
    "megwatt": ("Megawatt (MW)", ('si', 'W', 2)),
    "gwatt": ("Gigawatt (GW)", ('si', 'W', 3)),
    "mwatt": ("Milliwatt (mW)", ('si', 'W', -1)),
    "Wm2": ("Watt per square meter (W/m²)", ('si', 'W/m²', 0)),
    "voltamp": ("Volt-Ampere (VA)", ('si', 'VA', 0)),
    "kvoltamp": ("Kilovolt-Ampere (kVA)", ('si', 'VA', 1)),
    "voltampreact": ("Volt-Ampere reactive (VAr)", ('si', 'VAr', 0)),
    "kvoltampreact": ("Kilovolt-Ampere reactive (kVAr)", ('si', 'VAr', 1)),
    "watth": ("Watt-hour (Wh)", ('si', 'Wh', 0)),
    "watthperkg": ("Watt-hour per Kilogram (Wh/kg)", ('si', 'Wh/kg', 0)),
    "kwatth": ("Kilowatt-hour (kWh)", ('si', 'Wh', 1)),
    "kwattm": ("Kilowatt-min (kWm)", ('si', 'W-Min', 1)),
    "mwatth": ("Megawatt-hour (MWh)", ('si', 'Wh', 2)),
    "amph": ("Ampere-hour (Ah)", ('si', 'Ah', 0)),
    "kamph": ("Kiloampere-hour (kAh)", ('si', 'Ah', 1)),
    "mamph": ("Milliampere-hour (mAh)", ('si', 'Ah', -1)),
    "joule": ("Joule (J)", ('si', 'J', 0)),
    "ev": ("Electron volt (eV)", ('si', 'eV', 0)),
    "amp": ("Ampere (A)", ('si', 'A', 0)),
    "kamp": ("Kiloampere (kA)", ('si', 'A', 1)),
    "mamp": ("Milliampere (mA)", ('si', 'A', -1)),
    "volt": ("Volt (V)", ('si', 'V', 0)),
    "kvolt": ("Kilovolt (kV)", ('si', 'V', 1)),
    "mvolt": ("Millivolt (mV)", ('si', 'V', -1)),
    "dBm": ("Decibel-milliwatt (dBm)", ('fixed', 'dbm')),
    "mohm": ("Milliohm (mΩ)", ('si', 'Ω', -1)),
    "ohm": ("Ohm (Ω)", ('si', 'Ω', 0)),
    "kohm": ("Kiloohm (kΩ)", ('si', 'Ω', 1)),
    "Mohm": ("Megaohm (MΩ)", ('si', 'Ω', 2)),
    "farad": ("Farad (F)", ('si', 'F', 0)),
    "µfarad": ("Microfarad (µF)", ('si', 'F', -2)),
    "nfarad": ("Nanofarad (nF)", ('si', 'F', -3)),
    "pfarad": ("Picofarad (pF)", ('si', 'F', -4)),
    "ffarad": ("Femtofarad (fF)", ('si', 'F', -5)),
    "henry": ("Henry (H)", ('si', 'H', 0)),
    "mhenry": ("Millihenry (mH)", ('si', 'H', -1)),
    "µhenry": ("Microhenry (µH)", ('si', 'H', -2)),
    "lumens": ("Lumens (Lm)", ('si', 'Lm', 0)),
    "flowgpm": ("Gallons/min (gpm)", ('fixed', 'gpm')),
    "flowcms": ("Cubic meters/sec (cms)", ('fixed', 'cms')),
    "flowcfs": ("Cubic feet/sec (cfs)", ('fixed', 'cfs')),
    "flowcfm": ("Cubic feet/min (cfm)", ('fixed', 'cfm')),
    "litreh": ("Litre/hour", ('fixed', 'L/h')),
    "flowlpm": ("Litre/min (L/min)", ('fixed', 'L/min')),
    "flowmlpm": ("milliLitre/min (mL/min)", ('fixed', 'mL/min')),
    "lux": ("Lux (lx)", ('fixed', 'lux')),
    "forceNm": ("Newton-meters (Nm)", ('si', 'Nm', 0)),
    "forcekNm": ("Kilonewton-meters (kNm)", ('si', 'Nm', 1)),
    "forceN": ("Newtons (N)", ('si', 'N', 0)),
    "forcekN": ("Kilonewtons (kN)", ('si', 'N', 1)),
    "Hs": ("hashes/sec", ('si', 'H/s', 0)),
    "KHs": ("kilohashes/sec", ('si', 'H/s', 1)),
    "MHs": ("megahashes/sec", ('si', 'H/s', 2)),
    "GHs": ("gigahashes/sec", ('si', 'H/s', 3)),
    "THs": ("terahashes/sec", ('si', 'H/s', 4)),
    "PHs": ("petahashes/sec", ('si', 'H/s', 5)),
    "EHs": ("exahashes/sec", ('si', 'H/s', 6)),
    "massmg": ("milligram (mg)", ('si', 'g', -1)),
    "massg": ("gram (g)", ('si', 'g', 0)),
    "masslb": ("pound (lb)", ('fixed', 'lb')),
    "masskg": ("kilogram (kg)", ('si', 'g', 1)),
    "masst": ("metric ton (t)", ('fixed', 't')),
    "lengthmm": ("millimeter (mm)", ('si', 'm', -1)),
    "lengthcm": ("centimeter (cm)", ('fixed', 'cm')),
    "lengthin": ("inch (in)", ('fixed', 'in')),
    "lengthft": ("feet (ft)", ('fixed', 'ft')),
    "lengthm": ("meter (m)", ('si', 'm', 0)),
    "lengthkm": ("kilometer (km)", ('si', 'm', 1)),
    "lengthmi": ("mile (mi)", ('fixed', 'mi')),
    "pressurembar": ("Millibars", ('si', 'bar', -1)),
    "pressurebar": ("Bars", ('si', 'bar', 0)),
    "pressurekbar": ("Kilobars", ('si', 'bar', 1)),
    "pressurepa": ("Pascals", ('si', 'Pa', 0)),
    "pressurehpa": ("Hectopascals", ('fixed', 'hPa')),
    "pressurekpa": ("Kilopascals", ('fixed', 'kPa')),
    "pressurehg": ("Inches of mercury", ('fixed', '"Hg')),
    "pressurepsi": ("PSI", ('scaled', 1000, ('psi', 'ksi', 'Mpsi'))),
    "radbq": ("Becquerel (Bq)", ('si', 'Bq', 0)),
    "radci": ("curie (Ci)", ('si', 'Ci', 0)),
    "radgy": ("Gray (Gy)", ('si', 'Gy', 0)),
    "radrad": ("rad", ('si', 'rad', 0)),
    "radsv": ("Sievert (Sv)", ('si', 'Sv', 0)),
    "radmsv": ("milliSievert (mSv)", ('si', 'Sv', -1)),
    "radusv": ("microSievert (µSv)", ('si', 'Sv', -2)),
    "radrem": ("rem", ('si', 'rem', 0)),
    "radexpckg": ("Exposure (C/kg)", ('si', 'C/kg', 0)),
    "radr": ("roentgen (R)", ('si', 'R', 0)),
    "radsvh": ("Sievert/hour (Sv/h)", ('si', 'Sv/h', 0)),
    "radmsvh": ("milliSievert/hour (mSv/h)", ('si', 'Sv/h', -1)),
    "radusvh": ("microSievert/hour (µSv/h)", ('si', 'Sv/h', -2)),
    "rotrpm": ("Revolutions per minute (rpm)", ('fixed', 'rpm')),
    "rothz": ("Hertz (Hz)", ('si', 'Hz', 0)),
    "rotkhz": ("Kilohertz (kHz)", ('si', 'Hz', 1)),
    "rotmhz": ("Megahertz (MHz)", ('si', 'Hz', 2)),
    "rotghz": ("Gigahertz (GHz)", ('si', 'Hz', 3)),
    "rotrads": ("Radians per second (rad/s)", ('fixed', 'rad/s')),
    "rotdegs": ("Degrees per second (°/s)", ('fixed', '°/s')),
    "celsius": ("Celsius (°C)", ('fixed', '°C')),
    "fahrenheit": ("Fahrenheit (°F)", ('fixed', '°F')),
    "kelvin": ("Kelvin (K)", ('fixed', 'K')),
    "hertz": ("Hertz (1/s)", ('si', 'Hz', 0)),
    "ns": ("nanoseconds (ns)", ('special', 'toNanoSeconds')),
    "µs": ("microseconds (µs)", ('special', 'toMicroSeconds')),
    "ms": ("milliseconds (ms)", ('special', 'toMilliSeconds')),
    "s": ("seconds (s)", ('special', 'toSeconds')),
    "m": ("minutes (m)", ('special', 'toMinutes')),
    "h": ("hours (h)", ('special', 'toHours')),
    "d": ("days (d)", ('special', 'toDays')),
    "dtdurationms": ("duration (ms)", ('special', 'toDurationInMilliseconds')),
    "dtdurations": ("duration (s)", ('special', 'toDurationInSeconds')),
    "dthms": ("duration (hh:mm:ss)", ('special', 'toDurationInHoursMinutesSeconds')),
    "dtdhms": ("duration (d hh:mm:ss)", ('special', 'toDurationInDaysHoursMinutesSeconds')),
    "timeticks": ("Timeticks (s/100)", ('special', 'toTimeTicks')),
    "clockms": ("clock (ms)", ('special', 'toClockMilliseconds')),
    "clocks": ("clock (s)", ('special', 'toClockSeconds')),
    "cps": ("counts/sec (cps)", ('count', 'c/s')),
    "ops": ("ops/sec (ops)", ('count', 'ops/s')),
    "reqps": ("requests/sec (rps)", ('count', 'req/s')),
    "rps": ("reads/sec (rps)", ('count', 'rd/s')),
    "wps": ("writes/sec (wps)", ('count', 'wr/s')),
    "iops": ("I/O ops/sec (iops)", ('count', 'io/s')),
    "eps": ("events/sec (eps)", ('count', 'evt/s')),
    "mps": ("messages/sec (mps)", ('count', 'msg/s')),
    "recps": ("records/sec (rps)", ('count', 'rec/s')),
    "rowsps": ("rows/sec (rps)", ('count', 'rows/s')),
    "cpm": ("counts/min (cpm)", ('count', 'c/m')),
    "opm": ("ops/min (opm)", ('count', 'ops/m')),
    "reqpm": ("requests/min (rpm)", ('count', 'req/m')),
    "rpm": ("reads/min (rpm)", ('count', 'rd/m')),
    "wpm": ("writes/min (wpm)", ('count', 'wr/m')),
    "epm": ("events/min (epm)", ('count', 'evts/m')),
    "mpm": ("messages/min (mpm)", ('count', 'msgs/m')),
    "recpm": ("records/min (rpm)", ('count', 'rec/m')),
    "rowspm": ("rows/min (rpm)", ('count', 'rows/m')),
    "velocityms": ("meters/second (m/s)", ('fixed', 'm/s')),
    "velocitykmh": ("kilometers/hour (km/h)", ('fixed', 'km/h')),
    "velocitymph": ("miles/hour (mph)", ('fixed', 'mph')),
    "velocityknot": ("knot (kn)", ('fixed', 'kn')),
    "mlitre": ("millilitre (mL)", ('si', 'L', -1)),
    "litre": ("litre (L)", ('si', 'L', 0)),
    "m3": ("cubic meter", ('fixed', 'm³')),
    "Nm3": ("Normal cubic meter", ('fixed', 'Nm³')),
    "dm3": ("cubic decimeter", ('fixed', 'dm³')),
    "gallons": ("gallons", ('fixed', 'gal')),
    "bool": ("True / False", ('bool', 'True', 'False')),
    "bool_yes_no": ("Yes / No", ('bool', 'Yes', 'No')),
    "bool_on_off": ("On / Off", ('bool', 'On', 'Off')),
}


def _make(spec):
    """Turn a table entry into a formatter."""
    kind = spec[0]
    if kind == "fixed":
        return _fixed(spec[1])
    if kind == "si":
        return _si(spec[1], spec[2])
    if kind == "bin":
        return _bin(spec[1], spec[2])
    if kind == "count":
        return _count(spec[1])
    if kind == "currency":
        return _currency(spec[1], spec[2])
    if kind == "scaled":
        return _scaled(spec[1], list(spec[2]))
    if kind == "bool":
        return _bool(spec[1], spec[2])
    return SPECIAL_UNITS[spec[1]]


_FORMATTERS = {}


def _custom(unit):
    """The prefix:, suffix:, si:, count:, currency:, time: and bool: forms."""
    key, sep, sub = unit.partition(":")
    if not key or not sep:
        return None
    if key == "prefix":
        return lambda value, decimals=None: (sub + _to_fixed(value, decimals), "")
    if key == "suffix":
        return _fixed(sub)
    if key == "time":
        return _date_format(sub)
    if key == "si":
        offset = _si_offset(sub[:1])
        return _si(sub if offset == 0 else sub[1:], offset)
    if key == "count":
        return _count(sub)
    if key == "currency":
        parts = sub.split(":")
        if parts[0] == "financial" and len(parts) >= 2:
            if not parts[1]:
                return _fixed("")
            return _full_currency(parts[1], len(parts) > 2 and parts[2] == "suffix")
        return _currency(sub, False)
    if key == "bool":
        if "/" in sub:
            t, _, f = sub.partition("/")
            return _bool(t, f)
        return _bool(sub, "-")
    return None


def _si_offset(c):
    """The power of 1000 a prefix letter stands for: k is 1, m is -1, none is 0."""
    for i, prefix in enumerate(SI_PREFIXES):
        if _nfkd(prefix) == _nfkd(c):
            return i - SI_BASE
    return 0


def _nfkd(s):
    return unicodedata.normalize("NFKD", s)


def _formatter(unit):
    """The formatter for a unit id. Unknown ids fall back to short."""
    if not unit:
        unit = "none"
    unit = ALIASES.get(unit, unit)
    if unit in _FORMATTERS:
        return _FORMATTERS[unit]
    if unit in UNIT_FORMATS:
        fmt = _make(UNIT_FORMATS[unit][1])
    else:
        fmt = _custom(unit) or _make(UNIT_FORMATS["short"][1])
    _FORMATTERS[unit] = fmt
    return fmt


def format_value(value, unit=None, decimals=None):
    """Format a number as (text, suffix). Pass decimals=None to let Grafana choose."""
    if value is None or value != value:
        return "No data", ""
    if value == INF:
        return "∞", ""
    if value == -INF:
        return "-∞", ""
    return _formatter(unit)(float(value), decimals)


def unit_title(unit):
    """Grafana's display name for a unit id, like "bytes(IEC)". Custom ids come back as they are."""
    unit = ALIASES.get(unit or "none", unit or "none")
    if unit in UNIT_FORMATS:
        return UNIT_FORMATS[unit][0]
    return unit


# --- template variables ---------------------------------------------------------
#
# A dashboard's variables: their options (from a query, a list, intervals or the
# datasources), what's chosen, and how they go into queries and text in each of
# Grafana's formats.

ALL = "$__all"          # what Grafana stores for "All"
ALL_TEXT = "All"
AUTO_PREFIX = "$__auto_interval_"
PROM_TYPES = ("prometheus", "victoriametrics-metrics-datasource")
# The datasource references that aren't a datasource: told apart, never resolved.
SPECIAL_DS = {"-- mixed --": "mixed", "-- dashboard --": "dashboard", "-- grafana --": "grafana",
           "grafana": "grafana"}
# $var, [[var]], [[var:fmt]], ${var}, ${var.field}, ${var:fmt}: Grafana's own pattern.
VAR_RE = re.compile(r"\$(\w+)|\[\[(\w+?)(?::(\w+))?\]\]|\$\{(\w+)(?:\.([^:^\}]+))?(?::([^\}]+))?\}")
INTERVAL_UNITS = {"y": 31536000, "M": 2592000, "w": 604800, "d": 86400, "h": 3600,
                  "m": 60, "s": 1, "ms": 0.001}
# The "nice" steps a query interval rounds to: (upper bound in ms, step in ms)
NICE_STEPS = [(10, 1), (15, 10), (35, 20), (75, 50), (150, 100), (350, 200), (750, 500),
              (1500, 1000), (3500, 2000), (7500, 5000), (12500, 10000), (17500, 15000),
              (25000, 20000), (45000, 30000), (90000, 60000), (210000, 120000),
              (450000, 300000), (750000, 600000), (1050000, 900000), (1500000, 1200000),
              (2700000, 1800000), (5400000, 3600000), (9000000, 7200000),
              (16200000, 10800000), (32400000, 21600000), (86400000, 43200000),
              (604800000, 86400000), (1814400000, 604800000), (3628800000, 2592000000)]


# --- intervals ---------------------------------------------------------------

def interval_to_ms(text):
    """'30s', '1m', '500ms' or a bare number of seconds, in milliseconds."""
    text = str(text).strip()
    m = re.match(r"^(-?\d+(?:\.\d+)?)(ms|[Mwdhmsy])?$", text)
    if not m:
        raise ValueError("'%s' is not an interval like 30s or 5m" % text)
    return int(float(m.group(1)) * INTERVAL_UNITS[m.group(2) or "s"] * 1000)


def ms_text(ms):
    """Milliseconds as Grafana writes an interval: the biggest whole unit, like 5m."""
    seconds = ms / 1000.0
    for size, unit in ((31536000, "y"), (86400, "d"), (3600, "h"), (60, "m"), (1, "s")):
        n = int(seconds // size)
        if n:
            return "%d%s" % (n, unit)
    return "%dms" % int(ms) if int(ms) else "0s"


def round_interval(ms):
    """Round to the nearest step Grafana likes (1s, 5s, 15s, 1m, 5m, ...)."""
    for bound, step in NICE_STEPS:
        if ms < bound:
            return step
    return 31536000000


def calc_interval_ms(start, end, max_points, min_interval):
    """The step for a window of start..end (unix seconds) at about max_points points.
    min_interval is like "1m" or ">30s" (or empty): the step never goes below it."""
    floor_ms = 1
    low = str(min_interval or "").strip().lstrip(">").strip()  # dashboards say "1m", or 60
    if low:
        try:
            floor_ms = interval_to_ms(low)
        except ValueError:
            pass
    step = round_interval((end - start) * 1000.0 / max(1, max_points or 1))
    return int(max(step, floor_ms))


def builtin_vars(start, end, interval_ms, scrape_interval_s, dashboard_title):
    """Grafana's built-in variables for a window (unix seconds), as {name: value}."""
    scrape_ms = int(scrape_interval_s * 1000)
    rate_ms = max(int(interval_ms) + scrape_ms, 4 * scrape_ms)
    range_s = int(round(end - start))
    return {"__interval": ms_text(interval_ms), "__interval_ms": str(int(interval_ms)),
            "__rate_interval": "%dms" % rate_ms,
            "__range": "%ds" % range_s, "__range_s": str(range_s),
            "__range_ms": str(range_s * 1000),
            "__from": str(int(start * 1000)), "__to": str(int(end * 1000)),
            "__dashboard": dashboard_title or ""}


# --- escaping and formats ----------------------------------------------------

def prom_regular_escape(value):
    """A value in a plain (non-multi) Prometheus string: \\ and ' are escaped."""
    return value.replace("\\", "\\\\").replace("'", "\\\\'")


def prom_regex_escape(value):
    """A value going into a regex matcher: regex characters get a double backslash."""
    value = value.replace("\\", "\\\\\\\\")
    return re.sub(r"[$^*{}\[\]+?.()|]", lambda m: "\\\\" + m.group(0), value)


def prom_format(value, multi, include_all):
    """How a variable goes into a PromQL query: escaped, or as a regex for several."""
    if not multi and not include_all:
        return prom_regular_escape(value) if isinstance(value, str) else \
            prom_regular_escape(",".join(value))
    if isinstance(value, str):
        return prom_regex_escape(value)
    escaped = [prom_regex_escape(v) for v in value]
    return escaped[0] if len(escaped) == 1 else "(" + "|".join(escaped) + ")"


def enc(text):
    """encodeURIComponent, also encoding ! ' ( ) * as Grafana does."""
    return quote(str(text), safe="")


def lucene_escape(value):
    try:
        float(value)
        return value
    except ValueError:
        return re.sub(r'([!*+\-=<>\s&|()\[\]{}^~?:\\/"])', r"\\\1", value)


def iso_ms(ms):
    """A time in milliseconds as JavaScript's toISOString writes it (UTC)."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms / 1000.0))
    return "%s.%03dZ" % (stamp, int(ms) % 1000)


def as_list(value):
    return value if isinstance(value, list) else [value]


def format_var(value, fmt, args, name, texts):
    """One of Grafana's named formats. value is a string or a list of strings."""
    vals = [str(v) for v in as_list(value)]
    many = isinstance(value, list)
    one = ",".join(vals)
    if fmt == "csv":
        return one
    if fmt == "pipe":
        return "|".join(vals)
    if fmt == "html":
        return ", ".join(vals)
    if fmt == "raw":
        return one
    if fmt == "json":
        return json.dumps(value, separators=(",", ":")) if many else vals[0]
    if fmt == "text":
        return " + ".join(texts)
    if fmt == "lucene":
        if not many:
            return lucene_escape(vals[0])
        if not vals:
            return "__empty__"
        return "(" + " OR ".join('"' + lucene_escape(v) + '"' for v in vals) + ")"
    if fmt == "regex":
        esc = [re.sub(r"[\\^$*+?.()|\[\]{}/]", lambda m: "\\" + m.group(0), v) for v in vals]
        return "(" + "|".join(esc) + ")" if many and len(esc) > 1 else "".join(esc[:1])
    if fmt == "distributed":
        return ",".join(v if i == 0 else "%s=%s" % (name, v) for i, v in enumerate(vals))
    if fmt == "singlequote":
        return ",".join("'" + v.replace("'", "\\'") + "'" for v in vals)
    if fmt == "doublequote":
        return ",".join('"' + v.replace('"', '\\"') + '"' for v in vals)
    if fmt == "sqlstring":
        return ",".join("'" + v.replace("'", "''").replace('"', '\\"') + "'" for v in vals)
    if fmt == "percentencode":
        return enc("{" + one + "}") if many else enc(vals[0])
    if fmt == "queryparam":
        return "&".join("var-%s=%s" % (name, enc(v)) for v in vals)
    if fmt == "date":
        return format_date(vals[0], args[0] if args else "iso")
    return "{" + one + "}" if len(vals) > 1 else one   # glob, and anything unknown


def format_date(text, how):
    """${__from:date}, ${__from:date:seconds}: a time in milliseconds, written another way."""
    try:
        ms = int(text)
    except ValueError:
        return "NaN"
    if how == "ms":
        return str(ms)
    if how == "seconds":
        return str(int(math.floor(ms / 1000.0 + 0.5)))
    return iso_ms(ms)    # iso, and any custom format (not supported)


# --- reading a variable ------------------------------------------------------

class Variable:
    """One entry of templating.list, with its options and what's selected."""

    def __init__(self, raw):
        self.raw = raw if isinstance(raw, dict) else {}
        r = self.raw
        self.name = str(r.get("name") or "")
        self.type = r.get("type") or "custom"
        self.label = r.get("label") or self.name
        self.hide = int(r.get("hide") or 0)          # 0 shown, 1 no label, 2 hidden
        self.multi = bool(r.get("multi"))
        self.include_all = bool(r.get("includeAll"))
        self.all_value = r.get("allValue") or None
        self.refresh = int(r.get("refresh") or 0)    # 1 on load, 2 on time range change
        self.sort = int(r.get("sort") or 0)
        self.error = None
        self.options = saved_options(r)
        self.values = []
        self.deps = set()

    def texts(self):
        """What the selection is called: All, or the names of the chosen options."""
        by_value = dict((o["value"], o["text"]) for o in self.options)
        return [by_value.get(v, v) for v in self.values]

    def is_all(self):
        return ALL in self.values

    def all_values(self):
        """Every real option: what All stands for when there's no allValue."""
        return [o["value"] for o in self.options if o["value"] != ALL and not o.get("none")]

    def selected(self):
        """The current value the way an interpolation sees it: a string, or a list."""
        values = self.all_values() if self.is_all() else list(self.values)
        return values if self.multi or self.is_all() else (values[0] if values else "")


def saved_options(raw):
    """The options a dashboard saved, as {"text", "value"} (they may be out of date)."""
    out = []
    for o in raw.get("options") or []:
        if isinstance(o, dict) and ("value" in o or "text" in o):
            value = as_list(o.get("value", o.get("text")))[0]
            out.append({"text": str(as_list(o.get("text", value))[0]), "value": str(value)})
    return out


def saved_current(raw):
    """What the dashboard had selected: a list of values (or texts, from old dashboards)."""
    cur = raw.get("current")
    if not isinstance(cur, dict):
        return []
    wanted = cur.get("value", cur.get("text"))
    wanted = [] if wanted is None or wanted == "" else as_list(wanted)
    return [str(w) for w in wanted]


def pick_current(var, wanted):
    """Keep what's wanted if it is still an option; else the first option (All, with
    includeAll, since All comes first)."""
    by_value = dict((o["value"], o["value"]) for o in var.options)
    by_text = dict((o["text"], o["value"]) for o in var.options)
    chosen = [by_value.get(w, by_text.get(w)) for w in wanted]
    chosen = [c for c in chosen if c is not None]
    if ALL in chosen:
        chosen = [ALL]
    if not var.multi:
        chosen = chosen[:1]
    real = [o for o in var.options if not o.get("none")]
    return chosen or ([real[0]["value"]] if real else [])


def with_all(var, options):
    """Put All first when includeAll; say None when there's nothing at all."""
    if var.include_all and options:
        options = [{"text": ALL_TEXT, "value": ALL}] + options
    if not options:
        options = [{"text": "None", "value": "", "none": True}]
    return options


def custom_options(text):
    """A custom variable's comma list: 'a, b' or 'Name : value' pairs; \\, is a comma."""
    out = []
    for part in re.findall(r"(?:\\,|[^,])+", text or ""):
        part = part.replace("\\,", ",")
        m = re.match(r"^(.+)\s:\s(.+)$", part)
        key, value = (m.group(1).strip(), m.group(2).strip()) if m else (part.strip(),) * 2
        out.append({"text": key, "value": value})
    return out


def js_regex(text):
    """A Grafana regex field as a Python regex: '/.../flags', or a whole-name match."""
    if not text.startswith("/"):
        return re.compile("^" + text + "$"), False
    m = re.match(r"^/(.*?)/([gimsy]*)$", text, re.S)
    if not m:
        raise ValueError("'%s' is not a valid regular expression" % text)
    flags = 0
    for ch, flag in (("i", re.I), ("m", re.M), ("s", re.S)):
        if ch in m.group(2):
            flags |= flag
    body = re.sub(r"\(\?<([A-Za-z_]\w*)>", r"(?P<\1>", m.group(1))   # JS named groups
    return re.compile(body, flags), "g" in m.group(2)


def apply_regex(regex, items):
    """Filter and rewrite [(text, value)] with the variable's regex. A named group
    called text or value picks them; otherwise the first group; otherwise the item stays."""
    if not regex:
        return items
    rx, every = js_regex(regex)
    out = []
    for text, value in items:
        matches = list(rx.finditer(value)) if every else [m for m in [rx.search(value)] if m]
        if every:
            matches = trim_empty(matches)
        if not matches:
            continue
        named = [m.groupdict() for m in matches]
        vgroup = next((g["value"] for g in named if g.get("value")), None)
        tgroup = next((g["text"] for g in named if g.get("text")), None)
        grouped = [m for m in matches if m.re.groups]
        if vgroup or tgroup:
            out.append((tgroup or vgroup, vgroup or tgroup))
        elif len(matches) > 1 and grouped:
            out.extend((m.group(1), m.group(1)) for m in matches)
        elif grouped:
            out.append((grouped[0].group(1), grouped[0].group(1)))
        else:
            out.append((text, value))
    return out


def trim_empty(matches):
    """JS stops its global loop at the first empty match; so do we."""
    out = []
    for m in matches:
        out.append(m)
        if m.group(0) == "":
            break
    return out


def sort_options(options, mode):
    """Grafana's sort modes: 1/2 alphabetical, 3/4 numerical, 5/6 case-insensitive,
    7/8 natural; odd ascending, even descending. 0 keeps the order."""
    if not mode:
        return options
    kind = (mode + 1) // 2

    def number(o):
        m = re.match(r".*?(\d+).*", o["text"] or "", re.S)
        return int(m.group(1)) if m else -1

    def natural(o):
        return [(0, int(p), "") if p.isdigit() else (1, 0, p.lower())
                for p in re.split(r"(\d+)", o["text"] or "")]

    keys = {1: lambda o: o["text"], 2: number, 3: lambda o: o["text"].lower(), 4: natural}
    out = sorted(options, key=keys[kind]) if kind in keys else list(options)
    return out[::-1] if mode % 2 == 0 else out


def query_text(query):
    """The variable's query as a string: it may be one, or an object from the query editor."""
    if isinstance(query, dict):
        text = query.get("query") or query.get("classicQuery") or ""
        if text:
            return text
        kind, label, metric = query.get("qryType"), query.get("label"), query.get("metric")
        if kind == 1:
            return "label_names()"
        if kind == 2 and label:
            return "label_values(%s%s)" % (metric + ", " if metric else "", label)
        if kind == 3:
            return "metrics(%s)" % (metric or ".*")
        if kind == 4:
            return "query_result(%s)" % (query.get("varQuery") or "")
        return query.get("seriesQuery") or ""
    return query if isinstance(query, str) else ""


def prom_items(backend, ds, query, start, end):
    """Run a variable query against a Prometheus datasource: [(text, value)]."""
    query = query.strip()
    m = re.match(r"^label_names\(\)\s*$", query)
    if m:
        return pairs(backend.prom_label_names(ds, None, start, end))
    m = re.match(r"^label_names\((.+)\)\s*$", query)
    if m:
        selector = '{__name__=~".*%s.*"}' % m.group(1)
        return pairs([n for n in backend.prom_label_names(ds, selector, start, end)
                      if n != "__name__"])
    m = re.match(r"^label_values\((?:(.+),\s*)?(.+)\)\s*$", query)
    if m:
        match = m.group(1) if m.group(1) and m.group(1).replace(" ", "") != "{}" else None
        return pairs(backend.prom_label_values(ds, m.group(2).strip(), match, start, end))
    m = re.match(r"^metrics\((.+)\)\s*$", query)
    if m:
        selector = '{__name__=~"%s"}' % m.group(1)
        return pairs(backend.prom_label_values(ds, "__name__", selector, start, end))
    m = re.match(r"^query_result\((.+)\)\s*$", query)
    if m:
        return pairs(result_texts(backend.prom_instant(ds, m.group(1), end), end))
    if query in ("label_values()", "metrics()", "query_result()") or not query:
        return []
    return pairs(series_texts(backend.prom_series(ds, query, start, end)))


def pairs(texts):
    return [(str(t), str(t)) for t in texts]


def series_texts(series):
    """A series as name{a="b",...}, as Grafana lists them."""
    out = []
    for labels in series:
        rest = [(k, v) for k, v in labels.items() if k != "__name__"]
        out.append("%s{%s}" % (labels.get("__name__", ""),
                               ",".join('%s="%s"' % kv for kv in rest)))
    return out


def result_texts(result, t):
    """An instant query's result as query_result() lists it: name{labels} value time.
    A scalar (labels None) is only its value."""
    out = []
    for labels, value in result:
        text = "%.15g" % value
        if labels is not None:      # None is a scalar: just its value
            text = "%s %s %d" % (series_texts([labels])[0], text, int(t * 1000))
        out.append(text)
    return out


# --- the variables of a dashboard --------------------------------------------

class Variables:
    """A dashboard's template variables: reads them, fills in their options from a
    backend, and puts their values into text. resolve() and set() never raise: a
    variable that can't be filled in keeps what the dashboard saved, and says why in
    .error."""

    def __init__(self, items, backend=None):
        self.backend = backend
        self.vars = [Variable(i) for i in items or [] if isinstance(i, dict) and i.get("name")]
        self.by_name = dict((v.name, v) for v in self.vars)
        self.datasources = []
        self.start = self.end = 0.0
        for v in self.vars:
            v.deps = self.references(v)
            v.values = pick_current(v, saved_current(v.raw))
        self.order = self.sorted_vars()

    def get(self, name):
        return self.by_name.get(name)

    def references(self, var):
        """The other variables that a variable's query, regex or datasource use."""
        r = var.raw
        text = json.dumps([r.get("query"), r.get("regex"), r.get("datasource")])
        found = set(a or b or c for a, b, _, c, _, _ in VAR_RE.findall(text))
        return set(n for n in found if n in self.by_name and n != var.name)

    def sorted_vars(self):
        """Variables in the order they can be filled in; those in a loop go last, in error."""
        done, left = [], list(self.vars)
        while left:
            ready = [v for v in left if all(self.by_name[d] in done for d in v.deps)]
            if not ready:
                for v in left:
                    v.error = "variables refer to each other in a loop (%s)" % \
                        ", ".join(sorted(x.name for x in left))
                return done + left
            done.extend(ready)
            left = [v for v in left if v not in ready]
        return done

    def dependents(self, names):
        """The variables that depend, however indirectly, on any of these."""
        out = set()
        grew = True
        while grew:
            grew = False
            for v in self.vars:
                if v.name not in out and v.deps & (set(names) | out):
                    out.add(v.name)
                    grew = True
        return out

    # filling in

    def resolve(self, start, end, overrides=None):
        """Fill in every variable's options and pick its value. overrides is
        {name: [values]} (from var-name= in a URL); $__all means All."""
        self.start, self.end = start, end
        self.load_datasources()
        self.run([v.name for v in self.vars], overrides or {})

    def refresh_for_time(self, start, end):
        """The time range changed: redo the variables that ask for that (refresh 2)."""
        self.start, self.end = start, end
        names = [v.name for v in self.vars if v.refresh == 2 and v.type == "query"]
        self.run(sorted(set(names) | self.dependents(names)), {})
        for v in self.vars:
            if v.type == "interval":
                v.values = pick_current(v, v.values)

    def set(self, name, values, start, end):
        """Change what a variable has selected, and redo the ones that depend on it."""
        self.start, self.end = start, end
        var = self.by_name.get(name)
        if var is None:
            return
        var.values = self.accept(var, as_list(values))
        self.run(sorted(self.dependents([name])), {})

    def accept(self, var, wanted):
        """The values from a request, if they fit; a textbox takes anything."""
        wanted = [ALL if str(w) in (ALL, ALL_TEXT) and var.include_all else str(w)
                  for w in wanted]
        if var.type == "textbox":
            return wanted[:1]
        if var.type == "constant" or not var.options:
            return var.values
        return pick_current(var, wanted or var.values)

    def load_datasources(self):
        try:
            self.datasources = self.backend.datasources() if self.backend else []
        except Exception:
            self.datasources = []

    def run(self, names, overrides):
        """Fill in these variables, in dependency order."""
        want = set(names)
        for var in self.order:
            if var.name not in want or var.error and var.error.startswith("variables refer"):
                continue
            var.error = None
            try:
                self.fill(var, overrides.get(var.name))
            except Exception as e:      # a variable must never take the dashboard down
                var.error = str(e) or e.__class__.__name__
                if re.search(r"^re\.|nothing to repeat|unbalanced|bad escape", var.error):
                    var.error = "bad regex: " + var.error
                var.options = var.options or [{"text": v, "value": v} for v in var.values]
                var.values = pick_current(var, overrides.get(var.name) or var.values)

    def fill(self, var, override):
        """One variable: its options, then its value."""
        r = var.raw
        wanted = list(override) if override else (var.values or saved_current(r))
        wanted = [ALL if w == ALL_TEXT and var.include_all else w for w in wanted]
        kind = var.type
        if kind == "textbox":
            value = wanted[0] if wanted else str(r.get("query") or "")
            var.options, var.values = [{"text": value, "value": value}], [value]
            return
        if kind == "constant":
            value = self.interpolate(str(r.get("query") or ""), "", self.base_builtins())
            var.options, var.values = [{"text": value, "value": value}], [value]
            return
        if kind == "custom":
            query = self.interpolate(str(r.get("query") or ""), "", self.base_builtins(),
                                     default="csv")
            var.options = with_all(var, custom_options(query))
        elif kind == "interval":
            var.options = with_all(var, self.interval_options(var))
        elif kind == "datasource":
            var.options = with_all(var, self.datasource_options(var))
        elif kind == "query":
            var.options = with_all(var, self.query_options(var))
        elif kind == "adhoc":
            # Ad hoc filters add label filters to every query. Not supported: they are
            # accepted, selected nothing, and left out of the queries.
            var.options, var.values = [], []
            return
        else:
            var.error = "variable type %s is not supported" % kind
            return
        var.values = pick_current(var, wanted)

    def interval_options(self, var):
        r = var.raw
        items = [p.strip() for p in str(r.get("query") or "").split(",") if p.strip()]
        out = [{"text": i, "value": i} for i in items]
        if r.get("auto"):
            out.insert(0, {"text": "auto", "value": AUTO_PREFIX + var.name})
        return out

    def datasource_options(self, var):
        kind = str(var.raw.get("query") or "")
        regex = self.interpolate(str(var.raw.get("regex") or ""), "", self.base_builtins(),
                                 default="regex")
        rx = js_regex(regex)[0] if regex else None
        out = [{"text": d["name"], "value": d.get("uid") or d["name"]}
               for d in self.datasources
               if d.get("type") == kind and (not rx or rx.search(d["name"]))]
        return sort_options(out, var.sort)

    def query_options(self, var):
        """Run a query variable's query, then its regex and sort."""
        r = var.raw
        ds = datasource_ref(r.get("datasource"), self.datasources, self)
        if not ds or ds.get("special"):
            raise RuntimeError("the variable's datasource was not found")
        if ds.get("type") not in PROM_TYPES:
            raise RuntimeError("datasource type %s is not supported" % ds.get("type"))
        if self.backend is None:
            raise RuntimeError("no connection to run the query")
        builtin = self.base_builtins(ds)
        query = self.interpolate(query_text(r.get("query")), ds["type"], builtin)
        items = prom_items(self.backend, ds, query, self.start, self.end)
        regex = self.interpolate(str(r.get("regex") or ""), "", builtin, default="regex")
        seen, out = set(), []
        for text, value in apply_regex(regex, items):
            if value not in seen:
                seen.add(value)
                out.append({"text": text, "value": value})
        return sort_options(out, var.sort)

    def base_builtins(self, ds=None):
        """The built-ins a variable's own query can use."""
        scrape = 15
        try:
            scrape = interval_to_ms((ds or {}).get("jsonData", {}).get("timeInterval") or "15s") / 1000.0
        except ValueError:
            pass
        step = calc_interval_ms(self.start, self.end, 1000, "")
        return builtin_vars(self.start, self.end, int(max(scrape * 1000, step)), scrape, "")

    # putting values into text

    def interpolate(self, text, ds_type, builtins, scoped=None, default=None):
        """Replace $var, ${var}, ${var:format} and [[var]] in text. scoped is
        {name: value} from repeated panels or rows and wins over the variable's own
        value; names we don't know stay as they are. default is a format to use when
        the text doesn't name one (otherwise the datasource type decides)."""
        if not text or ("$" not in text and "[[" not in text):
            return text
        return VAR_RE.sub(lambda m: self.one(m, ds_type, builtins or {}, scoped or {}, default),
                          text)

    def one(self, m, ds_type, builtins, scoped, default):
        name = m.group(1) or m.group(2) or m.group(4)
        fmt = m.group(3) or m.group(6)
        var = self.by_name.get(name)
        if name in scoped:
            value, texts = scoped[name], [str(v) for v in as_list(scoped[name])]
            if var is not None and not var.multi and not isinstance(value, list):
                value = str(value)
        elif var is not None and var.type != "adhoc":
            if var.is_all() and var.all_value and fmt not in ("text", "queryparam"):
                return self.interpolate(var.all_value, ds_type, builtins, scoped)
            value, texts = var.selected(), (["All"] if var.is_all() else var.texts())
            value = self.interval_value(var, value)
        elif name in builtins:
            if not fmt:
                return str(builtins[name])
            value, texts = str(builtins[name]), [str(builtins[name])]
        elif name.startswith(AUTO_PREFIX[1:]):
            return self.auto_interval(name[len(AUTO_PREFIX) - 1:])
        else:
            return m.group(0)
        multi = bool(var and var.multi)
        include_all = bool(var and var.include_all)
        if not fmt:
            fmt = default
        if not fmt:
            if ds_type in PROM_TYPES:
                return prom_format(value, multi, include_all)
            fmt = "glob"
        args = fmt.split(":")
        return format_var(value, args[0], args[1:], name, texts)

    def interval_value(self, var, value):
        """An interval variable set to auto is the interval for the window."""
        if var.type != "interval":
            return value
        if isinstance(value, list):
            return [self.interval_value(var, v) for v in value]
        return self.auto_interval(var.name) if value == AUTO_PREFIX + var.name else value

    def auto_interval(self, name):
        var = self.by_name.get(name)
        count = int((var.raw.get("auto_count") if var else 0) or 30)
        low = str((var.raw.get("auto_min") if var else "") or "10s")
        return ms_text(calc_interval_ms(self.start, self.end, count, low))

    def datasource_ref(self, ref, datasources):
        return datasource_ref(ref, datasources, self)


# --- which datasource -------------------------------------------------------

def datasource_ref(ref, datasources, variables=None):
    """A panel's or target's datasource reference as one of the datasources (a dict), or
    None if there isn't one. The references that aren't a datasource (-- Mixed --,
    -- Dashboard --, -- Grafana --) come back as {"special": "mixed" | "dashboard" |
    "grafana", "name": ...}. A reference through a variable ($ds, ${ds}) takes the
    variable's value; pass variables (a Variables) for that."""
    if isinstance(ref, dict):
        key, kind = ref.get("uid") or ref.get("name"), ref.get("type")
    else:
        key, kind = ref, None
    if isinstance(key, str) and variables is not None and "$" in key:
        key = variables.interpolate(key, "", {}, default="raw")
    if not key or key == "default":
        pool = [d for d in datasources if not kind or d.get("type") == kind]
        return next((d for d in pool if d.get("isDefault")), pool[0] if pool else None)
    key = str(key)
    if key.lower() in SPECIAL_DS:
        return {"special": SPECIAL_DS[key.lower()], "name": key}
    for field in ("uid", "name"):
        for d in datasources:
            if d.get(field) == key:
                return d
    return None


# --- Prometheus over Grafana's REST API ---------------------------------------

RESOURCE_STYLES = {}    # datasource uid -> the path style that worked for it


def prom_paths(ds, api):
    """The paths a datasource's Prometheus API can be at: through Grafana's resources
    endpoint, or through its proxy."""
    uid = quote(ds["uid"], safe="")
    paths = ["/api/datasources/uid/%s/resources%s" % (uid, api),
             "/api/datasources/proxy/uid/%s%s" % (uid, api)]
    first = RESOURCE_STYLES.get(ds["uid"], 0)
    return [(first, paths[first]), (1 - first, paths[1 - first])]


def prom_get(request, ds, api, params):
    """GET a Prometheus API path for a datasource; returns its "data"."""
    query = urlencode([(k, v) for k, vs in params.items() for v in as_list(vs)
                       if v is not None], quote_via=quote)
    last = None
    for style, path in prom_paths(ds, api):
        try:
            doc = request("GET", path + ("?" + query if query else ""))
        except RuntimeError as e:
            last = e
            continue
        if not isinstance(doc, dict) or doc.get("status") != "success":
            last = RuntimeError(str((doc or {}).get("error") if isinstance(doc, dict)
                                    else "unexpected answer from the datasource"))
            continue
        RESOURCE_STYLES[ds["uid"]] = style
        return doc.get("data")
    raise last or RuntimeError("the datasource did not answer")


def prom_window(match, start, end):
    return {"match[]": [match] if match else None, "start": "%d" % start, "end": "%d" % math.ceil(end)}


def rest_prom_label_names(request, ds, match, start, end):
    return list(prom_get(request, ds, "/api/v1/labels", prom_window(match, start, end)) or [])


def rest_prom_label_values(request, ds, label, match, start, end):
    api = "/api/v1/label/%s/values" % quote(label, safe="")
    return list(prom_get(request, ds, api, prom_window(match, start, end)) or [])


def rest_prom_series(request, ds, match, start, end):
    return list(prom_get(request, ds, "/api/v1/series", prom_window(match, start, end)) or [])


def rest_prom_instant(request, ds, expr, t):
    data = prom_get(request, ds, "/api/v1/query", {"query": expr, "time": "%.3f" % t}) or {}
    result = data.get("result")
    if data.get("resultType") == "scalar" and result:
        return [(None, float(result[1]))]   # no labels at all: a scalar
    if data.get("resultType") != "vector":
        return []
    return [(r.get("metric") or {}, float(r["value"][1])) for r in result]


# --- time --------------------------------------------------------------------------------

UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "M": 2592000,
         "y": 31536000}


def parse_duration(s):
    """Seconds in "30s", "5m", "1h30m", ">1m" (Grafana's "at least"), or a bare number
    of seconds; None if it isn't one."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip().lstrip(">").strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return float(s)
    parts = re.findall(r"(\d+(?:\.\d+)?)(ms|[smhdwMy])", s)
    if not parts or "".join(a + b for a, b in parts) != s:
        return None
    return sum(float(n) * UNITS[u] for n, u in parts)


def round_down(t, unit):
    lt = time.localtime(t)
    if unit in ("s", "m", "h"):
        step = UNITS[unit]
        return t - (t - time.mktime(lt[:3] + (0, 0, 0) + lt[6:])) % step
    day = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    if unit == "d":
        return day
    if unit == "w":
        return day - lt.tm_wday * 86400
    if unit == "M":
        return time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1))
    if unit == "y":
        return time.mktime((lt.tm_year, 1, 1, 0, 0, 0, 0, 0, -1))
    return t


def round_up(t, unit):
    """The end of the unit t is in: now/d as the end of a range is the end of today. The
    start of the next one, by the calendar (months and years differ in length, and
    days can be 23 or 25 hours), less a millisecond."""
    start = round_down(t, unit)
    if unit in ("s", "m", "h"):
        return start + UNITS[unit] - 0.001
    lt = time.localtime(start)
    y, mo, d = lt.tm_year, lt.tm_mon, lt.tm_mday
    if unit == "d":
        d += 1
    elif unit == "w":
        d += 7
    elif unit == "M":
        y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
    elif unit == "y":
        y += 1
    else:
        return t
    return time.mktime((y, mo, d, 0, 0, 0, 0, 0, -1)) - 0.001


def parse_time(s, now, end=False):
    """Unix seconds for Grafana's time: "now", "now-6h", "now-1d/d", "now/w", epoch
    milliseconds, or an ISO date."""
    s = str(s).strip()
    if re.fullmatch(r"\d{10,}", s):
        return int(s) / 1000 if len(s) > 11 else float(s)
    m = re.fullmatch(r"now((?:[+-]\d+(?:ms|[smhdwMy]))*)(?:/([smhdwMy]))?", s)
    if m:
        t = now
        for sign, n, u in re.findall(r"([+-])(\d+)(ms|[smhdwMy])", m.group(1)):
            t += (1 if sign == "+" else -1) * int(n) * UNITS[u]
        if m.group(2):
            t = round_up(t, m.group(2)) if end else round_down(t, m.group(2))
        return t
    utc = s.endswith("Z") or s.endswith("+00:00")  # a Z means UTC, not local time
    bare = re.sub(r"(Z|\+00:00)$", "", s).split(".")[0]
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = time.strptime(bare, fmt)
        except ValueError:
            continue
        return calendar.timegm(parsed) if utc else time.mktime(parsed)
    raise ValueError(f"can't read the time {s!r}")


def human_range(frm, to):
    """"Last 6 hours" for now-6h..now; otherwise frm to to."""
    m = re.fullmatch(r"now-(\d+)([smhdwMy])", frm)
    if m and to == "now":
        n, u = int(m.group(1)), m.group(2)
        word = {"s": "second", "m": "minute", "h": "hour", "d": "day", "w": "week",
                "M": "month", "y": "year"}[u]
        return f"Last {n} {word}{'s' if n != 1 else ''}"
    if frm == "now/d" and to == "now/d":
        return "Today"
    return f"{frm} to {to}"


# --- the dashboard -----------------------------------------------------------------------

def read_link(text):
    """(uid, Grafana's base URL, {variable: [values]}, from, to, viewPanel) from a
    dashboard's link, or None if text isn't one."""
    if not re.match(r"https?://", text):
        return None
    parts = urlsplit(text)
    m = re.search(r"/d(?:-solo)?/([^/?#]+)", parts.path)
    if not m:
        return None
    base = f"{parts.scheme}://{parts.netloc}{parts.path[:m.start()]}"
    qs = parse_qs(parts.query, keep_blank_values=True)
    variables = {k[4:]: v for k, v in qs.items() if k.startswith("var-")}
    one = lambda k: (qs.get(k) or [None])[0]
    return m.group(1), base, variables, one("from"), one("to"), one("viewPanel")


def check_dashboard(doc):
    """doc, if it's a dashboard we can draw (Grafana's classic JSON), made a little more
    regular; Unsupported if not."""
    if not isinstance(doc, dict) or not doc:
        raise Unsupported("Grafana didn't send a dashboard")
    spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else None
    if ("elements" in doc or "layout" in doc or (spec and ("elements" in spec or "layout" in spec))
            or "v2" in str(doc.get("apiVersion", ""))):
        raise Unsupported(V2_MESSAGE)
    if spec and "panels" in spec:  # the same, in Grafana's Kubernetes-style wrapper
        doc = spec
    if "panels" not in doc and isinstance(doc.get("rows"), list):
        doc = dict(doc, panels=from_rows(doc["rows"]))
    if not isinstance(doc.get("panels", []), list):
        raise Unsupported("this dashboard's panels aren't in a form dashboard.py knows")
    return doc


def from_rows(rows):
    """Very old dashboards (before Grafana 5) keep panels in rows with spans, 12 across.
    Grafana moves those to its grid when it loads them, and so does this."""
    panels, y = [], 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        height = parse_duration(str(row.get("height", "250px")).replace("px", "")) or 250
        h = max(1, math.ceil(height / (GRID_ROW + GRID_GAP)))
        collapsed = bool(row.get("collapse"))
        holder = panels
        if row.get("showTitle") or len(rows) > 1 or collapsed:
            head = {"type": "row", "title": row.get("title", ""), "id": -len(panels) - 1,
                    "collapsed": collapsed, "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}}
            panels.append(head)
            y += 1
            if collapsed:  # a closed row keeps its panels inside it, as Grafana's do
                head["panels"] = holder = []
        top, x = y, 0
        for p in row.get("panels") or []:
            w = max(1, min(24, int(round((p.get("span") or 12) * 2))))
            if x + w > 24:
                x, y = 0, y + h
            holder.append(dict(p, gridPos={"x": x, "y": y, "w": w, "h": h}))
            x += w
        y = top if collapsed else y + h
    return panels


def grid(p):
    g = p.get("gridPos") or {}
    return (float(g.get("x") or 0), float(g.get("y") or 0), float(g.get("w") or 12),
            float(g.get("h") or 8))


class Item:
    """A panel where the layout puts it, in Grafana's grid units (24 across, rows of 30
    CSS pixels). scoped: the {variable: (text, value)} a repeated copy is for."""

    def __init__(self, panel, x, y, w, h, scoped=None, key=""):
        self.panel, self.x, self.y, self.w, self.h = panel, x, y, w, h
        self.scoped, self.key = scoped or {}, key


class Section:
    """A dashboard row and the panels under it. Panels above the first row are a section
    with no header (row None)."""

    def __init__(self, row, items, key, scoped=None):
        self.row, self.items, self.key = row, items, key
        self.scoped = scoped or {}
        self.collapsed = bool(row and row.get("collapsed"))


def sections_of(doc):
    """The dashboard's panels, sorted into sections, each panel's y counted from the top
    of its section."""
    panels = sorted((p for p in doc.get("panels") or [] if isinstance(p, dict)),
                    key=lambda p: (grid(p)[1], grid(p)[0]))
    out = [Section(None, [], "top")]
    base = 0
    seen = {}

    def key_of(p, x, y):
        """A key that tells a panel apart from every other: its id, or where it is when
        it has none (or shares one)."""
        key = f"p{p['id']}" if p.get("id") is not None else f"at{x:g},{y:g}"
        seen[key] = seen.get(key, 0) + 1
        return key if seen[key] == 1 else f"{key}#{seen[key]}"

    for p in panels:
        x, y, w, h = grid(p)
        if p.get("type") == "row":
            base = y + 1
            sec = Section(p, [], f"row:{p.get('id')}:{p.get('title')}:{y:g}")
            for q in p.get("panels") or []:  # a collapsed row keeps its panels inside
                qx, qy, qw, qh = grid(q)
                sec.items.append(Item(q, qx, max(0, qy - base), qw, qh, key=key_of(q, qx, qy)))
            out.append(sec)
        else:
            out[-1].items.append(Item(p, x, max(0, y - base), w, h, key=key_of(p, x, y)))
    return [s for s in out if s.row is not None or s.items]


def compact(items):
    """Grafana's grid gravity: each panel floats up as far as it can."""
    placed = []
    for it in sorted(items, key=lambda i: (i.y, i.x)):
        def free(y):
            return all(not (o.x < it.x + it.w - 1e-6 and it.x < o.x + o.w - 1e-6
                            and o.y < y + it.h - 1e-6 and y < o.y + o.h - 1e-6) for o in placed)
        y = it.y
        while y > 0 and free(y - 1):
            y -= 1
        while not free(y):
            y += 1
        it.y = y
        placed.append(it)
    return placed


def repeat_values(variables, name):
    """[(text, value)] for each value a variable has chosen (All: every option), which a
    row or panel repeated by it gets a copy for."""
    var = variables.get(name) if name else None
    if var is None:
        return []
    values = var.all_values() if var.is_all() else [v for v in var.values if v != ""]
    texts = dict((o["value"], o["text"]) for o in var.options)
    return [(texts.get(v, v), v) for v in values]


def scoped_values(scoped):
    """An item's {variable: (text, value)} as interpolate wants it: {variable: value}."""
    return {k: v[1] for k, v in (scoped or {}).items()}


def repeat_panels(items, variables):
    """A panel set to repeat by a variable becomes a copy for each value chosen."""
    out, shift = [], []
    for it in sorted(items, key=lambda i: (i.y, i.x)):
        it.y += sum(n for at, n in shift if it.y >= at)
        name = it.panel.get("repeat")
        values = repeat_values(variables, name)
        if not values:
            out.append(it)
            continue
        n = len(values)
        if it.panel.get("repeatDirection") == "v":
            for i, v in enumerate(values):
                out.append(Item(it.panel, it.x, it.y + i * it.h, it.w, it.h,
                                dict(it.scoped, **{name: v}), f"{it.key}/{v[1]}"))
            extra = (n - 1) * it.h
        else:
            per_row = max(1, min(n, int(it.panel.get("maxPerRow") or 4)))
            w = GRID_COLS / per_row
            for i, v in enumerate(values):
                out.append(Item(it.panel, (i % per_row) * w, it.y + (i // per_row) * it.h, w, it.h,
                                dict(it.scoped, **{name: v}), f"{it.key}/{v[1]}"))
            extra = ((n - 1) // per_row) * it.h
        if extra:
            shift.append((it.y + it.h, extra))
    return out


def layout(doc, variables, grafana, library):
    """The dashboard's sections, with repeated rows and panels copied out, library panels
    filled in, and everything floated up."""
    out = []
    for sec in sections_of(doc):
        for it in sec.items:  # a library panel is a reference until it's fetched
            ref = it.panel.get("libraryPanel")
            if isinstance(ref, dict) and ref.get("uid") and "targets" not in it.panel:
                model = library.get(ref["uid"])
                if model is None:
                    try:
                        model = grafana.library_panel(ref["uid"])
                    except RuntimeError as e:
                        model = {"type": "library-unavailable", "description": str(e)}
                    library[ref["uid"]] = model
                it.panel = dict(model, gridPos=it.panel.get("gridPos"), id=it.panel.get("id"),
                                title=it.panel.get("title") or model.get("title"))
        values = repeat_values(variables, (sec.row or {}).get("repeat"))
        name = (sec.row or {}).get("repeat")
        copies = [(sec, {})] if not values else [(sec, {name: v}) for v in values]
        for i, (s, scoped) in enumerate(copies):
            items = [Item(it.panel, it.x, it.y, it.w, it.h, dict(it.scoped, **scoped),
                          f"{it.key}@{i}") for it in s.items]
            ns = Section(s.row, compact(repeat_panels(items, variables)),
                         f"{s.key}@{i}", scoped)
            ns.collapsed = s.collapsed
            out.append(ns)
    return out


class Spot:
    """Where something lands, in the terminal's pixels: a panel (item) or a row header
    (section, which is one cell row high)."""

    def __init__(self, x, y, w, h, item=None, section=None):
        self.x, self.y, self.w, self.h, self.item, self.section = x, y, w, h, item, section


def place(sections, width, cw, ch):
    """Lay the sections out across width pixels -> ([Spot], total height in cell rows).
    Rows of the grid are Grafana's 30 CSS pixels, at graph's size for a CSS pixel; the 24
    columns share the width."""
    u = ch / CSS_PER_CELL
    gap, rowh = GRID_GAP * u, GRID_ROW * u
    colw = (width - 2 * gap - (GRID_COLS - 1) * gap) / GRID_COLS
    spots, top = [], 0  # top: in cell rows; each section starts on a fresh one
    for sec in sections:
        if sec.row is not None:
            spots.append(Spot(0, top * ch, width, ch, section=sec))
            top += 1
            if sec.collapsed:
                continue
        y0, bottom = top * ch + gap / 2, top * ch
        for it in sec.items:
            x = gap + it.x * (colw + gap)
            y = y0 + it.y * (rowh + gap)
            w = it.w * colw + (it.w - 1) * gap
            h = it.h * rowh + (it.h - 1) * gap
            spots.append(Spot(x, y, w, h, item=it))
            bottom = max(bottom, y + h)
        top = max(top, math.ceil((bottom + gap / 2) / ch - 1e-6))
    return spots, top


# --- queries -------------------------------------------------------------------------------

def deep_strings(obj, fn, skip=("refId", "datasource", "hide")):
    """obj with fn applied to every string in it, except under the keys in skip."""
    if isinstance(obj, str):
        return fn(obj)
    if isinstance(obj, list):
        return [deep_strings(v, fn, ()) for v in obj]
    if isinstance(obj, dict):
        return {k: (v if k in skip else deep_strings(v, fn, ())) for k, v in obj.items()}
    return obj


def panel_range(panel, start, end, now):
    """A panel's own time range: its relative time (timeFrom) and shift (timeShift)."""
    frm = parse_duration(str(panel.get("timeFrom") or "").replace("now-", "")) if panel.get("timeFrom") else None
    if frm:
        start = end - frm
    shift = parse_duration(str(panel.get("timeShift") or "")) if panel.get("timeShift") else None
    if shift:
        start, end = start - shift, end - shift
    return start, end


def resolve_ds(variables, ref, datasources):
    """The datasource a panel or query means: one of datasources, Grafana's own (for
    "-- Grafana --"), an expression, or a marker for "-- Mixed --" and "-- Dashboard --"."""
    if ref == "__expr__" or (isinstance(ref, dict) and "__expr__" in (ref.get("type"), ref.get("uid"))):
        return {"uid": "__expr__", "type": "__expr__", "name": "Expression"}
    ds = datasource_ref(ref, datasources, variables)
    if ds and ds.get("special") == "grafana":
        return {"uid": "grafana", "type": "datasource", "name": "-- Grafana --"}
    return ds


def build_queries(item, variables, datasources, start, end, width_css, title):
    """The queries to send for a panel -> (queries, {refId: hidden?}, note), where
    queries is Grafana's own query JSON with variables filled in. note: why there are
    none, if there are none."""
    panel = item.panel
    targets = [t for t in panel.get("targets") or [] if isinstance(t, dict)]
    if not targets:
        return [], {}, None
    pds = resolve_ds(variables, panel.get("datasource"), datasources)
    scoped = scoped_values(item.scoped)
    queries, hidden = [], {}
    max_points = int(panel.get("maxDataPoints") or max(100, width_css))
    for i, t in enumerate(targets):
        ref = t.get("refId") or chr(ord("A") + i)
        ds = pds
        if t.get("datasource") is not None or (pds or {}).get("special") == "mixed":
            ds = resolve_ds(variables, t.get("datasource"), datasources) or pds
        if not ds or ds.get("special") in ("mixed",):
            ds = next((d for d in datasources if d.get("isDefault")), None)
        if not ds:
            return [], {}, "no datasource for this panel"
        if ds.get("special") == "dashboard":
            return [], {}, "dashboard"
        min_iv = panel.get("interval") or (ds.get("jsonData") or {}).get("timeInterval")
        interval_ms = calc_interval_ms(start, end, max_points, min_iv)
        scrape = parse_duration((ds.get("jsonData") or {}).get("timeInterval")) or 15
        builtins = builtin_vars(start, end, interval_ms, scrape, title)
        q = deep_strings(t, lambda s: variables.interpolate(s, ds.get("type"), builtins, scoped))
        q.update(refId=ref, intervalMs=interval_ms, maxDataPoints=max_points,
                 datasource={"uid": ds.get("uid"), "type": ds.get("type")})
        if ds.get("type") in PROMETHEUS_TYPES and not q.get("range") and not q.get("instant"):
            q["range"] = True
        hidden[ref] = bool(t.get("hide"))
        queries.append(q)
    return queries, hidden, None


# --- drawing panels --------------------------------------------------------------------------

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
        while d.textlength(word, font=fnt) > width and len(word) > 1:
            cut = max(1, int(len(word) * width / d.textlength(word, font=fnt)) - 1)
            lines.append(word[:cut])
            word = word[cut:]
        cur = word
    return lines + ([cur] if cur else [])


def squeeze(d, text, fnt, width, middle=False):
    """text, cut with an ellipsis to fit in width: at the end, or in the middle, so a
    long name keeps the end that tells it apart."""
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
    return keep(lo) if lo else ""


class Draw:
    """What a panel's drawer gets: the canvas, the box inside the panel's title to draw
    in, the panel's settings and data, and helpers for Grafana's field config.

    Sizes are in CSS pixels times u (one CSS pixel on this canvas), so a panel looks the
    same at any cell size: font(12) is a 12-pixel font, 8 * u an 8-pixel gap.
    """

    def __init__(self, img, box, u, theme, panel, frames, start, end, now, title, interp=None):
        self.img, self.box, self.u, self.theme = img, box, u, theme
        self.d = ImageDraw.Draw(img, "RGBA")  # an RGBA brush on an RGB canvas blends
        self.panel, self.options = panel, panel.get("options") or {}
        self.frames, self.start, self.end, self.now, self.title = frames, start, end, now, title
        self._interp = interp

    def interpolate(self, text):
        """text with the dashboard's variables filled in ($job, ${instance}, ...), the
        way a text panel's content or a link wants them."""
        return self._interp(text) if self._interp and isinstance(text, str) else text

    def font(self, css_px, bold=False, mono=False):
        return font(self.theme, css_px * self.u, bold, mono)

    def fields(self, types=("number",)):
        """[(frame, field, settings, display name)] for every field of these types,
        leaving out ones the panel hides."""
        out = []
        for fr in self.frames:
            for f in fr.fields:
                if f.type not in types:
                    continue
                conf, name = field_config(self.panel, f, fr, self.frames)
                if ((conf.get("custom") or {}).get("hideFrom") or {}).get("viz"):
                    continue
                out.append((fr, f, conf, name))
        return out

    def series(self):
        """[(name, settings, color, [(t, v)])]: each number field next to its frame's time
        field, colored by its color mode, the palette counting up."""
        out = []
        for fr, f, conf, name in self.fields():
            tf = fr.time_field()
            if not tf:
                continue
            pts = [(t, v if isinstance(v, (int, float)) and not isinstance(v, bool) else None)
                   for t, v in zip(tf.values, f.values) if t is not None]
            nums = numbers(v for _, v in pts)
            last = nums[-1] if nums else None
            color = value_color(conf, last, self.theme, len(out),
                                min(nums) if nums else None, max(nums) if nums else None)
            out.append((name, conf, color, pts))
        return out

    def reduced(self):
        """The values a stat, gauge, bar gauge or pie chart shows, by its reduceOptions:
        each field's values reduced to one (by each calc: last, mean, max, ...), or with
        "all values", one per row. [{"name", "value", "conf", "field", "frame", "index",
        "times", "values"}], where times and values are the whole field, for a sparkline."""
        ro = self.options.get("reduceOptions") or {}
        calcs = [c for c in ro.get("calcs") or [] if isinstance(c, str)] or ["lastNotNull"]
        pattern = str(ro.get("fields") or "")
        rx = None
        if pattern:
            m = re.fullmatch(r"/(.*)/([a-z]*)", pattern, re.S)
            try:
                rx = re.compile(m.group(1), re.I if "i" in m.group(2) else 0) if m else None
            except re.error:
                rx = None
        out = []
        for fr in self.frames:
            tf = fr.time_field()
            names = next((f for f in fr.fields if f.type == "string"), None)
            for f in fr.fields:
                if f.type == "time":
                    continue
                conf, name = field_config(self.panel, f, fr, self.frames)
                if ((conf.get("custom") or {}).get("hideFrom") or {}).get("viz"):
                    continue
                if pattern:
                    if not ((rx and (rx.search(name) or rx.search(f.name)))
                            or (not rx and pattern in (name, f.name))):
                        continue
                elif f.type != "number":
                    continue
                times = tf.values if tf else []
                if ro.get("values"):  # every row, not one number per field
                    for i, v in enumerate(f.values[:int(ro.get("limit") or 5000)]):
                        row = names.values[i] if names and names is not f and i < len(names.values) else None
                        out.append({"name": str(row) if row is not None else name, "value": v,
                                    "conf": conf, "field": f, "frame": fr, "index": len(out),
                                    "times": times, "values": f.values})
                    continue
                for c in calcs:
                    v = reduce_values(f.values, c) if f.type == "number" else \
                        next((x for x in reversed(f.values) if x is not None), None)
                    out.append({"name": name if len(calcs) == 1 else f"{name} {CALC_NAMES.get(c, c)}",
                                "value": v, "conf": conf, "field": f, "frame": fr,
                                "index": len(out), "times": times, "values": f.values})
        return out

    def display(self, value, conf, index=0):
        """(text, unit text, color) for showing one value, with its mapping if one
        matches."""
        mapped = map_value(conf, value)
        if mapped and mapped.get("text") not in (None, ""):
            color = self.theme.color(mapped["color"]) if mapped.get("color") else \
                value_color(conf, value if isinstance(value, (int, float)) else None, self.theme, index)
            return str(mapped["text"]), "", color
        number = value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
        color = self.theme.color(mapped["color"]) if mapped and mapped.get("color") else \
            value_color(conf, number, self.theme, index)
        if isinstance(value, str):
            return value, "", color
        if isinstance(value, bool):
            return ("true" if value else "false"), "", color
        text, suffix = format_value(value, conf.get("unit"), conf.get("decimals"))
        if value is None and conf.get("noValue") is not None:
            text = str(conf["noValue"])
        return text, suffix, color

    def message(self, text, color=None):
        """A line or two of text in the middle of the box: "No data", say."""
        x0, y0, x1, y1 = self.box
        f = self.font(12)
        lines = wrap(self.d, text, f, x1 - x0)[:4]
        lh = f.size * 1.35
        y = (y0 + y1) / 2 - lh * len(lines) / 2
        for ln in lines:
            self.d.text(((x0 + x1) / 2, y), ln, font=f, fill=color or self.theme.muted, anchor="mt")
            y += lh

    def no_data(self):
        self.message(str((self.panel.get("fieldConfig") or {}).get("defaults", {}).get("noValue")
                         or "No data"))


PANELS = {}  # type -> drawer(Draw); a type that isn't here is drawn as an empty frame


def drawer(*types):
    def register(fn):
        for t in types:
            PANELS[t] = fn
        return fn
    return register


def finish(img, w, h, u, theme, alpha):
    """Round off a panel's corners, give it its border and opacity, and shrink it from
    the size it was drawn at to w x h."""
    W, H = img.size
    radius = theme.radius * u
    ImageDraw.Draw(img, "RGBA").rounded_rectangle(
        (0, 0, W - 1, H - 1), radius=radius, outline=theme.border, width=max(1, round(1.5 * u)))
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, W - 1, H - 1), radius=radius, fill=alpha)
    img.putalpha(mask)
    return img.resize((max(1, w), max(1, h)), Image.Resampling.LANCZOS)


class PanelData:
    """A panel's latest query results: frames, an error, when they came."""

    def __init__(self, frames=None, error=None, updated=None, note=None):
        self.frames, self.error, self.updated, self.note = frames or [], error, updated, note


def render_panel(item, data, w, h, ch, theme, start, end, now, title, interp=None):
    """An RGBA picture of a panel, w x h pixels, for a terminal whose cells are ch
    pixels high (at the same scale). interp fills in variables, for the drawers that
    show text from the dashboard."""
    S = SUPERSAMPLE
    W, H, u = max(2, round(w * S)), max(2, round(h * S)), ch * S / CSS_PER_CELL
    panel = item.panel
    kind = panel.get("type") or ""
    fn = PANELS.get(kind)
    img = Image.new("RGB", (W, H), theme.surface)
    d = ImageDraw.Draw(img, "RGBA")
    pad = 8 * u
    top = pad * 0.5
    if title:
        f = font(theme, 12.5 * u, bold=True)
        right = W - pad
        if data.error and fn:
            fs = font(theme, 11 * u)
            msg = squeeze(d, data.error, fs, (W - 2 * pad) * 0.45)
            right -= d.textlength(msg, font=fs) + pad
            d.text((W - pad, pad + f.size * 0.55), msg, font=fs, fill=theme.critical, anchor="rm")
        d.text((pad, pad), squeeze(d, title, f, right - pad), font=f, fill=theme.ink)
        top = pad + f.size * 1.6
    box = (pad, top, W - pad, H - pad)
    alpha = theme.panel_alpha
    g = Draw(img, box, u, theme, panel, data.frames, start, end, now, title, interp)
    if fn is None:
        alpha = theme.empty_alpha
        label = {"library-unavailable": "library panel (couldn't be read)"}.get(kind, f"{kind or 'unknown'} panel")
        g.message(label)
    elif data.note and not data.frames:
        g.message(data.note)
    elif data.error and not data.frames and panel.get("targets"):
        g.message(data.error, theme.ink_2)
    elif data.updated is None and panel.get("targets"):
        g.message("loading…")
    else:
        try:
            fn(g)
        except Exception as e:  # a drawer's bug shows in its own panel, not the whole screen
            traceback.print_exc(file=LOG)
            g.message(f"couldn't draw this panel: {e}", theme.ink_2)
    return finish(img, round(w), round(h), u, theme, alpha)


class Log:
    """Where tracebacks go: a file, with --log, or nowhere (the screen is ours)."""
    file = None

    def write(self, s):
        if self.file:
            self.file.write(s)
            self.file.flush()

    def flush(self):
        pass


LOG = Log()


# --- the panels -------------------------------------------------------------------------------

def nice_step(span, target):
    raw = span / max(target, 1)
    if raw <= 0 or not math.isfinite(raw):
        return 1.0
    mag = 10 ** math.floor(math.log10(raw))
    return next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)


def axis_range(values, conf):
    """(lo, hi, ticks) for a y axis over values: from zero for data that's all positive,
    with a little headroom, unless min, max or soft min/max say otherwise."""
    custom = conf.get("custom") or {}
    lo, hi = min(values), max(values)
    soft_lo, soft_hi = custom.get("axisSoftMin"), custom.get("axisSoftMax")
    if soft_lo is not None:
        lo = min(lo, soft_lo)
    if soft_hi is not None:
        hi = max(hi, soft_hi)
    fixed_lo, fixed_hi = conf.get("min"), conf.get("max")
    if fixed_lo is None and lo >= 0 and soft_lo is None:
        lo = 0.0
    lo = fixed_lo if fixed_lo is not None else lo
    hi = fixed_hi if fixed_hi is not None else hi
    if hi <= lo:
        hi = lo + (abs(lo) or 1)
    step = nice_step(hi - lo, 4)
    if fixed_hi is None:
        hi = math.ceil(hi / step - 1e-9) * step
    if fixed_lo is None and lo != 0:
        lo = math.floor(lo / step + 1e-9) * step
    ticks, t = [], math.ceil(lo / step - 1e-9) * step
    while t <= hi + step * 1e-6:
        ticks.append(t)
        t += step
    return lo, hi, ticks


def axis_labels(ticks, conf):
    """Tick labels in the field's unit, with as many decimals as the step between them
    needs (0.25 apart: 2; 50 apart: none), unless the field fixes its decimals."""
    decimals = conf.get("decimals")
    if decimals is None and len(ticks) > 1:
        step = abs(ticks[1] - ticks[0])
        scale = 1.0
        if conf.get("unit") == "percentunit":
            scale = 100.0
        decimals = 0
        while decimals < 6 and abs(round(step * scale, decimals) - step * scale) > step * scale * 1e-6:
            decimals += 1
        # Scaled units (K, Mi, ...) pick their own decimals.
        if conf.get("unit") not in (None, "", "none", "percent", "percentunit") and \
                max(abs(t) for t in ticks) >= 1000:
            decimals = None
    return ["".join(format_value(t, conf.get("unit"), decimals)) for t in ticks]


TIME_STEPS = [1, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200,
              86400, 172800, 604800, 1209600, 2592000]


def time_ticks(start, end, width_px, label_px):
    """Nice times between start and end for an axis width_px wide, and their format.
    label_px is how wide "00:00" is (or a function that measures a label): longer
    labels, like dates, get more room."""
    span = max(1, end - start)
    sample = time.localtime(end)
    for step in TIME_STEPS:
        fmt = "%H:%M:%S" if step < 60 else "%H:%M" if span <= 86400 else \
            "%m/%d %H:%M" if step < 86400 else "%m/%d"
        text = time.strftime(fmt, sample)
        lw = label_px(text) if callable(label_px) else label_px * len(text) / 5
        if span / step <= max(2, width_px / (lw * 1.6)):
            break
    t = math.ceil(start / step) * step
    if step >= 86400:  # whole days, in local time
        t = round_down(start, "d") + 86400
        while (t - round_down(t, "d")) or ((t - round_down(start, "d")) / 86400) % (step / 86400):
            t += 86400
            if t > end:
                break
    elif step >= 3600:  # on the hour, in local time, so 6h steps land on 00, 06, 12, 18
        t = round_down(start, "h") + 3600
        while ((t - round_down(t, "d")) / 3600) % (step / 3600) and t <= end:
            t += 3600
    out = []
    while t <= end:
        out.append(t)
        t += step
    return out, fmt


@drawer("timeseries", "graph", "trend")
def draw_timeseries(g):
    """Grafana's time series panel (and the old graph panel, read the same way): lines,
    bars or points over the time range, with the panel's unit, thresholds, colors and
    legend."""
    panel = g.panel
    if panel.get("type") == "graph":
        panel = g.panel = from_graph(panel)
        g.options = panel.get("options") or {}
        g.frames = g.frames
    series = g.series()
    if not series:
        return g.no_data()
    d, u, theme = g.d, g.u, g.theme
    x0, y0, x1, y1 = g.box
    f_tick, f_legend = g.font(10.5), g.font(11)

    # Values, transformed: negative-Y flips a series below the axis (network out, say).
    drawn = []
    for i, (name, conf, color, pts) in enumerate(series):
        custom = conf.get("custom") or {}
        sign = -1 if custom.get("transform") == "negative-Y" else 1
        pts = [(t, v * sign if v is not None else None) for t, v in pts]
        drawn.append({"name": name, "conf": conf, "custom": custom, "color": color, "pts": pts,
                      "raw": [v for _, v in pts]})

    # Stacking: each series sits on the ones before it in the same stacking group.
    groups = {}
    for s in drawn:
        st = s["custom"].get("stacking") or {}
        if st.get("mode") in ("normal", "percent"):
            groups.setdefault(st.get("group", "A"), []).append(s)
    for members in groups.values():
        percent = (members[0]["custom"].get("stacking") or {}).get("mode") == "percent"
        totals = {}
        if percent:
            for s in members:
                for t, v in s["pts"]:
                    if v is not None:
                        totals[t] = totals.get(t, 0) + abs(v)
        base = {}
        for s in members:
            new, floor = [], []
            for t, v in s["pts"]:
                b = base.get(t, 0.0)
                if v is None:
                    new.append((t, None))
                    floor.append(b)
                    continue
                if percent:
                    v = v / totals[t] if totals.get(t) else 0.0
                new.append((t, b + v))
                floor.append(b)
                base[t] = b + v
            s["pts"], s["floor"] = new, floor

    # The legend: below the plot (or to its right), a swatch, the name, and any calcs.
    legend = g.options.get("legend") or {}
    show_legend = legend.get("showLegend", True) and legend.get("displayMode") != "hidden" \
        and len(drawn) > 0 and not (len(drawn) == 1 and not legend.get("calcs") and not g.title)
    calcs = [c for c in legend.get("calcs") or [] if isinstance(c, str)]
    right_side = legend.get("placement") == "right"
    lines_h = f_legend.size * 1.45
    legend_items = []
    for s in drawn:
        if (s["custom"].get("hideFrom") or {}).get("legend"):
            continue
        parts = []
        for c in calcs:
            v = reduce_values(s["raw"], c)
            text, suffix, _ = g.display(v, s["conf"])
            parts.append(f"{CALC_NAMES.get(c, c)}: {text}{suffix}")
        legend_items.append((s["name"], s["color"], "  ".join(parts)))
    legend_box = None
    if show_legend and legend_items:
        sw = 14 * u
        if right_side:
            width = min((x1 - x0) * 0.4, max(d.textlength(f"{n}  {c}", font=f_legend)
                                             for n, _, c in legend_items) + sw + 12 * u)
            legend_box = (x1 - width, y0, x1, y1)
            x1 = x1 - width - 8 * u
        else:
            # Lay the items out in rows, as many as fit, at most a third of the panel.
            rows, row, rx = [], [], 0
            for n, c, extra in legend_items:
                iw = sw + 6 * u + d.textlength(n + ("  " + extra if extra else ""), font=f_legend) + 16 * u
                if row and rx + iw > x1 - x0:
                    rows.append(row)
                    row, rx = [], 0
                row.append((n, c, extra, iw))
                rx += iw
            if row:
                rows.append(row)
            most = max(1, int((y1 - y0) * 0.34 / lines_h))
            if len(rows) > most:
                rows = rows[:most]
            legend_box = (x0, y1 - len(rows) * lines_h, x1, y1)
            y1 = legend_box[1] - 4 * u
            ly = legend_box[1]
            for row in rows:
                lx = x0
                for n, c, extra, iw in row:
                    cy = ly + lines_h / 2
                    d.rounded_rectangle((lx, cy - 1.5 * u, lx + sw, cy + 1.5 * u), radius=u, fill=c)
                    tx = lx + sw + 6 * u
                    n2 = squeeze(d, n, f_legend, x1 - tx)
                    d.text((tx, cy), n2, font=f_legend, fill=theme.ink_2, anchor="lm")
                    if extra:
                        d.text((tx + d.textlength(n2 + "  ", font=f_legend), cy), extra,
                               font=f_legend, fill=theme.ink, anchor="lm")
                    lx += iw
                ly += lines_h
            legend_box = None  # drawn

    if legend_box:  # to the right: one item a line
        lx, ly = legend_box[0], legend_box[1]
        sw = 14 * u
        for n, c, extra in legend_items:
            if ly + lines_h > legend_box[3]:
                break
            cy = ly + lines_h / 2
            d.rounded_rectangle((lx, cy - 1.5 * u, lx + sw, cy + 1.5 * u), radius=u, fill=c)
            text = squeeze(d, n + ("  " + extra if extra else ""), f_legend, legend_box[2] - lx - sw - 6 * u)
            d.text((lx + sw + 6 * u, cy), text, font=f_legend, fill=theme.ink_2, anchor="lm")
            ly += lines_h

    # The y axis, from the first series' settings (its unit, min and max).
    axis_conf = drawn[0]["conf"]
    vals = [v for s in drawn for _, v in s["pts"] if v is not None and math.isfinite(v)]
    vals += [b for s in drawn for b in s.get("floor", []) if b is not None]
    # Thresholds are drawn where they fall; like Grafana, they don't stretch the axis.
    th_mode = ((axis_conf.get("custom") or {}).get("thresholdsStyle") or {}).get("mode", "off")
    if not vals:
        return g.no_data()
    stacked_pct = any((s["custom"].get("stacking") or {}).get("mode") == "percent" for s in drawn)
    conf_for_axis = dict(axis_conf, unit="percentunit") if stacked_pct else axis_conf
    lo, hi, ticks = axis_range(vals, conf_for_axis)
    tick_txt = axis_labels(ticks, conf_for_axis)
    axis_hidden = (axis_conf.get("custom") or {}).get("axisPlacement") == "hidden"
    left = x0 + (0 if axis_hidden else max(d.textlength(t, font=f_tick) for t in tick_txt) + 6 * u)
    bottom = y1 - f_tick.size * 1.5
    top = y0 + f_tick.size * 0.5
    right = x1
    if bottom - top < 10 * u:
        return g.message("too small to draw")

    def py(v):
        return bottom - (min(max(v, lo), hi) - lo) / (hi - lo) * (bottom - top)

    t0, t1 = g.start, g.end

    def px(t):
        return left + (t - t0) / max(1e-9, t1 - t0) * (right - left)

    lw1 = max(1, round(u))
    for t, txt in zip(ticks, tick_txt):
        yy = py(t)
        d.line((left, yy, right, yy), fill=theme.axis if t == lo else theme.grid, width=lw1)
        if not axis_hidden:
            d.text((left - 5 * u, yy), txt, font=f_tick, fill=theme.muted, anchor="rm")
    times, tfmt = time_ticks(t0, t1, right - left, lambda s: d.textlength(s, font=f_tick))
    for tt in times:
        xx = px(tt)
        txt = time.strftime(tfmt, time.localtime(tt))
        half = d.textlength(txt, font=f_tick) / 2
        if xx - half >= left - 4 * u and xx + half <= right + 4 * u:
            d.line((xx, top, xx, bottom), fill=theme.grid, width=lw1)
            d.text((xx, bottom + 3 * u), txt, font=f_tick, fill=theme.muted, anchor="mt")

    # Threshold lines and areas.
    if th_mode != "off":
        steps = (axis_conf.get("thresholds") or {}).get("steps") or []
        for k, st in enumerate(steps):
            c = theme.color(st.get("color"))
            v = st.get("value")
            nxt = steps[k + 1].get("value") if k + 1 < len(steps) else None
            if "area" in th_mode:
                ya = py(v) if isinstance(v, (int, float)) else bottom
                yb = py(nxt) if isinstance(nxt, (int, float)) else top
                d.rectangle((left, min(ya, yb), right, max(ya, yb)), fill=c + (28,))
            if ("line" in th_mode or "dashed" in th_mode) and isinstance(v, (int, float)):
                yy = py(v)
                if "dashed" in th_mode:
                    xx = left
                    while xx < right:
                        d.line((xx, yy, min(right, xx + 6 * u), yy), fill=c, width=lw1 * 2)
                        xx += 10 * u
                else:
                    d.line((left, yy, right, yy), fill=c, width=lw1 * 2)

    # The series: filled areas first, then bars, lines and points over them.
    clip = Image.new("L", g.img.size, 0)
    ImageDraw.Draw(clip).rectangle((left, top - 2 * u, right, bottom), fill=255)
    layer = Image.new("RGBA", g.img.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer, "RGBA")
    for s in drawn:
        custom, c, pts = s["custom"], s["color"], s["pts"]
        style = custom.get("drawStyle") or "line"
        width = max(1, round((custom.get("lineWidth") if custom.get("lineWidth") is not None else 1) * 1.5 * u))
        fill = custom.get("fillOpacity") or 0
        interp = custom.get("lineInterpolation") or "linear"
        span_nulls = custom.get("spanNulls")
        ts = [t for t, v in pts if v is not None]
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        typical = sorted(gaps)[len(gaps) // 2] if gaps else (t1 - t0)
        floor = s.get("floor")
        if style == "bars":
            bw = max(lw1, (px(t0 + typical) - px(t0)) * float(custom.get("barWidthFactor") or 0.6))
            for k, (t, v) in enumerate(pts):
                if v is None:
                    continue
                b = floor[k] if floor else max(lo, min(0, hi))
                xx = px(t)
                ld.rectangle((xx - bw / 2, min(py(v), py(b)), xx + bw / 2, max(py(v), py(b))),
                             fill=c + (max(60, int(255 * (fill or 80) / 100)),))
            continue
        # Runs of points, broken where values are missing (unless spanNulls) or there's
        # a gap of more than a few steps.
        runs, run = [], []
        for k, (t, v) in enumerate(pts):
            if v is None:
                if not span_nulls and run:
                    runs.append(run)
                    run = []
                continue
            if run and t - run[-1][0] > max(typical * 3, 1e-9) and span_nulls is not True:
                runs.append(run)
                run = []
            run.append((t, v, floor[k] if floor else None))
        if run:
            runs.append(run)
        for run in runs:
            xy = []
            for k, (t, v, b) in enumerate(run):
                if interp == "stepAfter" and k:
                    xy.append((px(t), py(run[k - 1][1])))
                elif interp == "stepBefore" and k:
                    xy.append((px(run[k - 1][0]), py(v)))
                xy.append((px(t), py(v)))
            if fill and len(xy) > 1:
                if floor:
                    base_pts = [(px(t), py(b if b is not None else 0)) for t, _, b in run][::-1]
                else:
                    zero = py(max(lo, min(0, hi)))
                    base_pts = [(xy[-1][0], zero), (xy[0][0], zero)]
                ld.polygon(xy + base_pts, fill=c + (int(255 * min(100, fill) / 100),))
            if style == "points" or custom.get("showPoints") == "always" or len(xy) == 1:
                r = max(width, (custom.get("pointSize") or 4) * u / 1.5)
                for x, y in xy:
                    ld.ellipse((x - r, y - r, x + r, y + r), fill=c)
            if style != "points" and len(xy) > 1:
                ld.line(xy, fill=c, width=width, joint="curve")
    g.img.paste(layer, (0, 0), Image.composite(layer, Image.new("RGBA", layer.size), clip))


def from_graph(p):
    """The old (Angular) graph panel's settings, read as a time series panel's."""
    yaxes = p.get("yaxes") or [{}]
    y = yaxes[0] if yaxes else {}
    custom = {"lineWidth": p.get("linewidth", 1),
              "fillOpacity": (p.get("fill") or 0) * 10 if p.get("lines", True) else 0,
              "drawStyle": "bars" if p.get("bars") and not p.get("lines", True) else
                           "points" if p.get("points") and not p.get("lines", True) else "line",
              "stacking": {"mode": "percent" if p.get("percentage") else "normal"} if p.get("stack") else {},
              "lineInterpolation": "stepAfter" if p.get("steppedLine") else "linear"}
    defaults = {"unit": y.get("format") or "short", "custom": custom}
    for key in ("min", "max"):
        try:
            if y.get(key) not in (None, ""):
                defaults[key] = float(y[key])
        except (TypeError, ValueError):
            pass
    if p.get("decimals") is not None:
        defaults["decimals"] = p["decimals"]
    defaults["color"] = {"mode": "palette-classic"}
    overrides = []
    for so in p.get("seriesOverrides") or []:
        alias = so.get("alias")
        if not alias:
            continue
        props = []
        if so.get("transform") == "negative-Y":
            props.append({"id": "custom.transform", "value": "negative-Y"})
        if so.get("color"):
            props.append({"id": "color", "value": {"mode": "fixed", "fixedColor": so["color"]}})
        if so.get("fill") is not None:
            props.append({"id": "custom.fillOpacity", "value": so["fill"] * 10})
        if so.get("linewidth") is not None:
            props.append({"id": "custom.lineWidth", "value": so["linewidth"]})
        if so.get("bars"):
            props.append({"id": "custom.drawStyle", "value": "bars"})
        if so.get("legend") is False:
            props.append({"id": "custom.hideFrom", "value": {"legend": True, "viz": False}})
        if props:
            m = re.fullmatch(r"/(.*)/", alias)
            matcher = {"id": "byRegexp", "options": m.group(1)} if m else {"id": "byName", "options": alias}
            overrides.append({"matcher": matcher, "properties": props})
    lg = p.get("legend") or {}
    calcs = [c for k, c in (("current", "lastNotNull"), ("min", "min"), ("max", "max"),
                            ("avg", "mean"), ("total", "sum")) if lg.get(k) and lg.get("values")]
    options = {"legend": {"showLegend": lg.get("show", True),
                          "displayMode": "table" if lg.get("alignAsTable") else "list",
                          "placement": "right" if lg.get("rightSide") else "bottom",
                          "calcs": calcs}}
    return dict(p, type="timeseries", fieldConfig={"defaults": defaults, "overrides": overrides},
                options=options)


@drawer("text")
def draw_text(g):
    """A text panel: its markdown (or HTML), as plain wrapped text with bigger headings."""
    opts = g.options
    content = opts.get("content") if "content" in opts else g.panel.get("content", "")
    mode = opts.get("mode") or g.panel.get("mode") or "markdown"
    text = g.interpolate(str(content or ""))
    if mode == "html":
        text = re.sub(r"(?is)<(script|style).*?</\1>", "", text)
        text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</h\d>|</li>", "\n", text)
        text = re.sub(r"(?i)<h(\d)[^>]*>", lambda m: "#" * int(m.group(1)) + " ", text)
        text = re.sub(r"(?i)<li[^>]*>", "- ", text)
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"&nbsp;", " ", text).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    d, theme = g.d, g.theme
    x0, y0, x1, y1 = g.box
    y = y0
    in_code = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        size, bold, mono, color = 12, False, in_code, theme.ink_2
        m = re.match(r"\s*(#{1,6})\s+(.*)", line)
        if m and not in_code:
            size, bold, color = max(12, 20 - 2 * len(m.group(1))), True, theme.ink
            line = m.group(2)
        elif not in_code:
            line = re.sub(r"^\s*[-*+]\s+", "• ", line)
            line = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", line)
            line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)
            line = re.sub(r"(\*\*|__|\*|_|`)(.+?)\1", r"\2", line)
        f = g.font(size, bold, mono)
        lh = f.size * 1.4
        if not line.strip():
            y += lh * 0.5
            continue
        for ln in (wrap(d, line, f, x1 - x0) if not in_code else [line]):
            if y + lh > y1:
                return
            d.text((x0, y), squeeze(d, ln, f, x1 - x0) if in_code else ln, font=f, fill=color)
            y += lh


# --- a legend, for the panels below ------------------------------------------------------------

def bar_legend(g, items, placement, box):
    """Draw a legend (a swatch, a name and maybe a value) for items [(name, color, extra)] at
    the bottom or right of box, and say what's left to plot in: (x0, y0, x1, y1). Pie,
    bar and histogram panels all use it."""
    d, u, theme = g.d, g.u, g.theme
    x0, y0, x1, y1 = box
    if not items:
        return box
    f = g.font(11)
    lh, sw = f.size * 1.45, 14 * u
    if placement == "right":
        need = max(d.textlength(n, font=f) + (d.textlength(e, font=f) + 12 * u if e else 0)
                   for n, _, e in items) + sw + 10 * u
        width = min((x1 - x0) * 0.4, need)
        lx, y = x1 - width, y0
        for n, c, e in items:
            if y + lh > y1:
                break
            cy = y + lh / 2
            d.rounded_rectangle((lx, cy - 1.5 * u, lx + sw, cy + 1.5 * u), radius=u, fill=c)
            tx = lx + sw + 6 * u
            right = x1
            if e:
                d.text((x1, cy), e, font=f, fill=theme.ink, anchor="rm")
                right = x1 - d.textlength(e, font=f) - 10 * u
            d.text((tx, cy), squeeze(d, n, f, right - tx), font=f, fill=theme.ink_2, anchor="lm")
            y += lh
        return (x0, y0, lx - 8 * u, y1)
    rows, row, rx = [], [], 0
    for n, c, e in items:
        iw = sw + 6 * u + d.textlength(n + ("  " + e if e else ""), font=f) + 16 * u
        if row and rx + iw > x1 - x0:
            rows.append(row)
            row, rx = [], 0
        row.append((n, c, e, iw))
        rx += iw
    rows.append(row)
    rows = rows[:max(1, int((y1 - y0) * 0.34 / lh))]
    top = y1 - len(rows) * lh
    y = top
    for row in rows:
        lx = x0
        for n, c, e, iw in row:
            cy = y + lh / 2
            d.rounded_rectangle((lx, cy - 1.5 * u, lx + sw, cy + 1.5 * u), radius=u, fill=c)
            tx = lx + sw + 6 * u
            n2 = squeeze(d, n, f, x1 - tx)
            d.text((tx, cy), n2, font=f, fill=theme.ink_2, anchor="lm")
            if e:
                d.text((tx + d.textlength(n2 + "  ", font=f), cy), e, font=f, fill=theme.ink, anchor="lm")
            lx += iw
        y += lh
    return (x0, y0, x1, top - 4 * u)


def bar_legend_on(legend, count, g):
    """Whether a panel's legend options (showLegend, displayMode) say to draw one."""
    return legend.get("showLegend", True) and legend.get("displayMode") != "hidden" and count > 0


def bar_percent(frac, decimals=0):
    return f"{frac * 100:.{decimals}f}%"


def bar_shown(g, v, conf):
    """A value as text, in its field's unit."""
    text, suffix, _ = g.display(v, conf)
    return text + suffix


# --- piechart --------------------------------------------------------------------------------

def pie_items(g):
    """The slices: [(name, value, conf, color)], the positive values reduceOptions gives,
    sorted as the panel's "sort" option says."""
    out = []
    for r in g.reduced():
        v = r["value"]
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v <= 0:
            continue
        out.append((r["name"], float(v), r["conf"], g.display(v, r["conf"], r["index"])[2]))
    order = g.options.get("sort")
    if order in ("desc", "asc"):
        out.sort(key=lambda s: s[1], reverse=order == "desc")
    return out


def pie_label(g, item, frac, kinds):
    """The lines of text to put on a slice: name, value and percent, as displayLabels say."""
    name, v, conf, _ = item
    lines = []
    if "name" in kinds:
        lines.append(name)
    if "value" in kinds:
        lines.append(bar_shown(g, v, conf))
    if "percent" in kinds:
        lines.append(bar_percent(frac, 0 if frac >= 0.1 else 1))
    return lines


@drawer("piechart")
def draw_piechart(g):
    """A pie or donut: one slice for each value the panel reduces to, labels on the
    slices that are wide enough, and a legend with values and percent if asked."""
    items = pie_items(g)
    total = sum(s[1] for s in items)
    if not items or total <= 0:
        return g.no_data()
    d, u, theme, opts = g.d, g.u, g.theme, g.options
    legend = opts.get("legend") or {}
    box = g.box
    if bar_legend_on(legend, len(items), g):
        shown = legend.get("values") or []
        entries = []
        for name, v, conf, color in items:
            extra = []
            if "value" in shown:
                extra.append(bar_shown(g, v, conf))
            if "percent" in shown:
                extra.append(bar_percent(v / total, conf.get("decimals") or 0))
            entries.append((name, color, "  ".join(extra)))
        box = bar_legend(g, entries, legend.get("placement") or "bottom", box)
    x0, y0, x1, y1 = box
    size = min(x1 - x0, y1 - y0)
    if size < 24 * u:
        return g.message("too small to draw")
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    outer = size / 2 * 0.96
    donut = opts.get("pieType") == "donut"
    inner = outer - max(outer / 3, 20 * u) if donut else 0
    inner = max(0, inner)
    kinds = [k for k in opts.get("displayLabels") or [] if k in ("name", "value", "percent")]
    angle = -90.0
    spans = []
    for item in items:
        sweep = item[1] / total * 360
        spans.append((angle, angle + sweep))
        angle += sweep
    bbox = (cx - outer, cy - outer, cx + outer, cy + outer)
    for item, (a, b) in zip(items, spans):
        if len(items) == 1:
            d.ellipse(bbox, fill=item[3])
        else:
            d.pieslice(bbox, a, b, fill=item[3])
    if len(items) > 1:  # a thin gap between slices
        for a, _ in spans:
            r = math.radians(a)
            d.line((cx, cy, cx + outer * math.cos(r), cy + outer * math.sin(r)),
                   fill=theme.surface, width=max(1, round(1.2 * u)))
    if inner > 0:
        d.ellipse((cx - inner, cy - inner, cx + inner, cy + inner), fill=theme.surface)
    if not kinds:
        return
    fs = min(max(outer / u / 150 * 14, 11), 20) if "name" in kinds else min(max(outer / u / 100 * 14, 11), 22)
    f = g.font(fs)
    for item, (a, b) in zip(items, spans):
        if (b - a) < 17.2 and len(items) > 1:  # Grafana's 0.3 radian minimum
            continue
        lines = pie_label(g, item, item[1] / total, kinds)
        mid = math.radians((a + b) / 2)
        rad = (outer + inner) / 2 if inner else outer * 0.62
        tx, ty = cx + rad * math.cos(mid), cy + rad * math.sin(mid)
        lh = f.size * 1.2
        # Light text on a dark slice, dark on a light one.
        c = item[3]
        ink = (20, 20, 20) if (c[0] * 0.299 + c[1] * 0.587 + c[2] * 0.114) > 150 else (245, 245, 245)
        y = ty - lh * len(lines) / 2 + lh / 2
        for ln in lines:
            if inner and d.textlength(ln, font=f) > (outer - inner) * 1.6 + 8 * u:
                ln = squeeze(d, ln, f, (outer - inner) * 1.6)
            d.text((tx, y), ln, font=f, fill=ink, anchor="mm")
            y += lh


# --- barchart --------------------------------------------------------------------------------

def bar_x_field(g):
    """(frame, the x field, [the number fields to draw]) for a bar chart: the xField option
    by name, else the first string field, else the first time field."""
    want = g.options.get("xField")
    for fr in g.frames:
        x = next((f for f in fr.fields if want and f.name == want), None) if want else None
        x = x or next((f for f in fr.fields if f.type == "string"), None) \
            or next((f for f in fr.fields if f.type == "time"), None)
        if x is None:
            continue
        ys = [(f, c, n) for (frame, f, c, n) in g.fields() if frame is fr and f is not x]
        if ys:
            return fr, x, ys
    return None, None, []


def bar_categories(x):
    """Text for each x value: strings as they are, times in a short local format."""
    if x.type == "time":
        vals = [v for v in x.values if isinstance(v, (int, float))]
        same_day = bool(vals) and time.strftime("%Y%m%d", time.localtime(min(vals))) == \
            time.strftime("%Y%m%d", time.localtime(max(vals)))
        fmt = "%H:%M:%S" if same_day and len(vals) > 1 and (max(vals) - min(vals)) < 600 else \
            "%H:%M" if same_day else "%m/%d %H:%M"
        return [time.strftime(fmt, time.localtime(v)) if isinstance(v, (int, float)) else "" for v in x.values]
    return ["" if v is None else str(v) for v in x.values]


def bar_stack(rows, mode):
    """Each bar's (from, to) for rows [[value or None per field] per category]: side by
    side when mode is none; stacked on each other (positives up, negatives down) for
    normal and percent."""
    out = []
    for vals in rows:
        if mode not in ("normal", "percent"):
            out.append([(0.0, v) if v is not None else None for v in vals])
            continue
        tot = sum(abs(v) for v in vals if v is not None) if mode == "percent" else 1.0
        pos = neg = 0.0
        segs = []
        for v in vals:
            if v is None:
                segs.append(None)
                continue
            v = v / tot if tot else 0.0
            if v >= 0:
                segs.append((pos, pos + v))
                pos += v
            else:
                segs.append((neg, neg + v))
                neg += v
        out.append(segs)
    return out


def bar_rotated(g, text, fnt, degrees, color):
    """A small picture of text turned by degrees, for a slanted axis label."""
    w = int(g.d.textlength(text, font=fnt)) + 4
    h = int(fnt.size * 1.4) + 2
    tile = Image.new("RGBA", (w, h), color + (0,))
    ImageDraw.Draw(tile).text((w - 2, h / 2), text, font=fnt, fill=color + (255,), anchor="rm")
    return tile.rotate(degrees, expand=True, resample=Image.Resampling.BICUBIC)


@drawer("barchart")
def draw_barchart(g):
    """A bar chart: a bar for each number field at each x value (a string or a time), side
    by side or stacked, vertical or horizontal, with values on the bars if asked."""
    fr, x, ys = bar_x_field(g)
    if not ys:
        return g.message("Bar charts need a string or time field") if g.frames and not x else g.no_data()
    d, u, theme, opts = g.d, g.u, g.theme, g.options
    n = min(len(x.values), 2000)
    if n == 0:
        return g.no_data()
    cats = bar_categories(x)[:n]
    num = lambda v: float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None  # noqa: E731
    cols = [[num(v) for v in (list(f.values) + [None] * n)[:n]] for f, _, _ in ys]
    rows = [[c[i] for c in cols] for i in range(n)]
    mode = opts.get("stacking") or "none"
    segs = bar_stack(rows, mode)
    ext = [0.0] + [v for r in segs for s in r if s for v in s]
    if len(ext) < 2:
        return g.no_data()
    f_tick = g.font(10.5)
    legend = opts.get("legend") or {}
    box = g.box
    if bar_legend_on(legend, len(ys), g):
        items = []
        for (f, conf, name), col in zip(ys, cols):
            nums = [v for v in col if v is not None]
            items.append((name, g.display(nums[-1] if nums else None, conf, len(items))[2], ""))
        box = bar_legend(g, items, legend.get("placement") or "bottom", box)
    x0, y0, x1, y1 = box
    conf0 = dict(ys[0][1], unit="percentunit") if mode == "percent" else ys[0][1]
    lo, hi, ticks = axis_range(ext, conf0)
    tick_txt = axis_labels(ticks, conf0)
    w_ticks = max(d.textlength(t, font=f_tick) for t in tick_txt)
    horizontal = opts.get("orientation") == "horizontal" or \
        (opts.get("orientation") in (None, "auto") and (x1 - x0) < (y1 - y0))
    rot = opts.get("xTickLabelRotation") or 0
    cat_w = max((d.textlength(c, font=f_tick) for c in cats), default=0)
    lw1 = max(1, round(u))
    # The plot area: the category labels take a strip on the left (horizontal) or below.
    if horizontal:
        label_w = min(cat_w + 12 * u, (x1 - x0) * 0.3)
        left, right = x0 + label_w, x1 - 8 * u
        top, bottom = y0 + f_tick.size * 0.5, y1 - f_tick.size * 1.5
    else:
        if rot:
            r = abs(math.radians(rot))
            strip = min((y1 - y0) * 0.35, cat_w * math.sin(r) + f_tick.size * 1.4 * math.cos(r) + 6 * u)
        else:
            strip = f_tick.size * 1.5
        left, right = x0 + w_ticks + 6 * u, x1
        top, bottom = y0 + f_tick.size * 0.5, y1 - strip
    if bottom - top < 10 * u or right - left < 10 * u:
        return g.message("too small to draw")

    def vpx(v):
        v = min(max(v, lo), hi)
        t = (v - lo) / (hi - lo)
        return left + t * (right - left) if horizontal else bottom - t * (bottom - top)

    c_lo, c_hi = (top, bottom) if horizontal else (left, right)
    slot = (c_hi - c_lo) / n
    # Gridlines and value ticks.
    for t, txt in zip(ticks, tick_txt):
        p = vpx(t)
        if horizontal:
            d.line((p, top, p, bottom), fill=theme.axis if t == lo else theme.grid, width=lw1)
            d.text((p, bottom + 3 * u), txt, font=f_tick, fill=theme.muted, anchor="mt")
        else:
            d.line((left, p, right, p), fill=theme.axis if t == lo else theme.grid, width=lw1)
            d.text((left - 5 * u, p), txt, font=f_tick, fill=theme.muted, anchor="rm")
    # Category labels, skipping some when they'd run into each other.
    skip = 1 if horizontal or rot else max(1, math.ceil((cat_w + 6 * u) / slot))
    skip_h = max(1, math.ceil(f_tick.size * 1.3 / slot)) if horizontal else skip
    for i in range(0, n, skip_h):
        mid = c_lo + (i + 0.5) * slot
        if horizontal:
            d.text((left - 5 * u, mid), squeeze(d, cats[i], f_tick, label_w - 6 * u), font=f_tick,
                   fill=theme.muted, anchor="rm")
        elif rot:
            tile = bar_rotated(g, cats[i], f_tick, -rot, theme.muted)
            g.img.paste(tile, (round(mid - tile.width / 2 - (0 if rot > 0 else 0)), round(bottom + 3 * u)), tile)
        else:
            d.text((mid, bottom + 3 * u), squeeze(d, cats[i], f_tick, slot * skip - 4 * u),
                   font=f_tick, fill=theme.muted, anchor="mt")
    # The bars.
    group_w = min(1.0, max(0.1, float(opts.get("groupWidth") or 0.7)))
    bar_w = min(1.0, max(0.1, float(opts.get("barWidth") or 0.97)))
    k = len(ys)
    stacked = mode in ("normal", "percent")
    show = opts.get("showValue") or "auto"
    f_val = g.font(10)
    base = vpx(max(lo, min(0.0, hi)))
    labels = []
    for i in range(n):
        gw = slot * group_w
        g0 = c_lo + i * slot + (slot - gw) / 2
        for j, (f, conf, name) in enumerate(ys):
            seg = segs[i][j]
            if seg is None:
                continue
            if stacked:
                a, b = g0, g0 + gw * bar_w
                a += gw * (1 - bar_w) / 2
                b = a + gw * bar_w
            else:
                sub = gw / k
                a = g0 + j * sub + sub * (1 - bar_w) / 2
                b = a + sub * bar_w
            v0, v1 = vpx(seg[0]), vpx(seg[1])
            raw = rows[i][j]
            conf_custom = conf.get("custom") or {}
            color = g.display(raw, conf, j)[2]
            alpha = int(255 * (conf_custom.get("fillOpacity") if conf_custom.get("fillOpacity") is not None else 80) / 100)
            width = conf_custom.get("lineWidth") if conf_custom.get("lineWidth") is not None else 1
            rect = (min(v0, v1), a, max(v0, v1), b) if horizontal else (a, min(v0, v1), b, max(v0, v1))
            if rect[2] - rect[0] < 1 or rect[3] - rect[1] < 1:
                rect = (rect[0], rect[1], max(rect[2], rect[0] + 1), max(rect[3], rect[1] + 1))
            d.rectangle(rect, fill=color + (alpha,),
                        outline=color + (255,) if width else None, width=max(1, round(width * u)))
            labels.append((rect, raw, conf, seg))
    if show != "never":
        for rect, raw, conf, seg in labels:
            txt = bar_shown(g, raw, conf)
            tw = d.textlength(txt, font=f_val)
            if horizontal:
                room = rect[3] - rect[1] >= f_val.size
                if show == "auto" and not room:
                    continue
                if stacked:
                    d.text(((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2), txt, font=f_val,
                           fill=theme.ink, anchor="mm")
                else:
                    pos_right = seg[1] >= 0
                    d.text((rect[2] + 3 * u if pos_right else rect[0] - 3 * u, (rect[1] + rect[3]) / 2),
                           txt, font=f_val, fill=theme.ink, anchor="lm" if pos_right else "rm")
            else:
                if show == "auto" and tw > (rect[2] - rect[0]) + slot * (1 - group_w) + 2 * u:
                    continue
                if stacked:
                    if rect[3] - rect[1] >= f_val.size:
                        d.text(((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2), txt, font=f_val,
                               fill=theme.ink, anchor="mm")
                else:
                    up = seg[1] >= 0
                    d.text(((rect[0] + rect[2]) / 2, rect[1] - 2 * u if up else rect[3] + 2 * u),
                           txt, font=f_val, fill=theme.ink, anchor="ms" if up else "mt")
    if lo < 0 < hi:  # the zero line, over the bars
        if horizontal:
            d.line((base, top, base, bottom), fill=theme.axis, width=lw1)
        else:
            d.line((left, base, right, base), fill=theme.axis, width=lw1)


# --- histogram -------------------------------------------------------------------------------

def hist_series(g):
    """[(name, conf, color, [numbers])]: each number field, or with combine, all of them as
    one."""
    out = []
    for fr, f, conf, name in g.fields():
        nums = numbers(f.values)
        if nums:
            out.append((name, conf, g.display(nums[-1], conf, len(out))[2], nums))
    if g.options.get("combine") and len(out) > 1:
        out = [("All values", out[0][1], out[0][2], [v for s in out for v in s[3]])]
    return out


def hist_buckets(lo, hi, opts):
    """(size, first bucket start) for values from lo to hi: bucketSize if the panel gives
    one, else a nice size for about bucketCount (30) buckets, then shifted by bucketOffset."""
    count = max(1, int(opts.get("bucketCount") or 30))
    size = float(opts.get("bucketSize") or 0)
    if size <= 0:
        size = nice_step(max(hi - lo, 1e-9), count) if hi > lo else 1.0
    off = float(opts.get("bucketOffset") or 0)
    start = math.floor((lo - off) / size + 1e-9) * size + off
    return size, start


@drawer("histogram")
def draw_histogram(g):
    """A histogram: every number field's values counted into buckets of one size shared
    between the series (bucketCount, bucketSize, bucketOffset), drawn as bars over each
    other, with the x axis in the field's unit."""
    series = hist_series(g)
    if not series:
        return g.no_data()
    d, u, theme, opts = g.d, g.u, g.theme, g.options
    lo = min(min(s[3]) for s in series)
    hi = max(max(s[3]) for s in series)
    size, start = hist_buckets(lo, hi, opts)
    nb = int(math.floor((hi - start) / size + 1e-9)) + 1
    if nb > 5000:
        size = (hi - lo) / 5000
        size, start = hist_buckets(lo, hi, dict(opts, bucketSize=size))
        nb = int(math.floor((hi - start) / size + 1e-9)) + 1
    counts = []
    for _, _, _, nums in series:
        c = np.zeros(nb, dtype=int)
        idx = np.clip(np.floor((np.array(nums) - start) / size + 1e-9).astype(int), 0, nb - 1)
        np.add.at(c, idx, 1)
        counts.append(c)
    f_tick = g.font(10.5)
    legend = opts.get("legend") or {}
    box = g.box
    if bar_legend_on(legend, len(series), g):
        box = bar_legend(g, [(s[0], s[2], "") for s in series], legend.get("placement") or "bottom", box)
    x0, y0, x1, y1 = box
    ymax = max(int(c.max()) for c in counts)
    ylo, yhi, yticks = axis_range([0.0, float(ymax)], {"decimals": 0})
    ytxt = axis_labels(yticks, {"decimals": 0})
    left = x0 + max(d.textlength(t, font=f_tick) for t in ytxt) + 6 * u
    top, bottom, right = y0 + f_tick.size * 0.5, y1 - f_tick.size * 1.5, x1 - 10 * u
    if bottom - top < 10 * u or right - left < 10 * u:
        return g.message("too small to draw")
    xlo, xhi = start, start + nb * size
    conf = series[0][1]

    def px(v):
        return left + (v - xlo) / (xhi - xlo) * (right - left)

    def py(v):
        return bottom - (v - ylo) / (yhi - ylo) * (bottom - top)

    lw1 = max(1, round(u))
    for t, txt in zip(yticks, ytxt):
        d.line((left, py(t), right, py(t)), fill=theme.axis if t == ylo else theme.grid, width=lw1)
        d.text((left - 5 * u, py(t)), txt, font=f_tick, fill=theme.muted, anchor="rm")
    # The x axis: nice ticks across the buckets.
    xstep = nice_step(xhi - xlo, max(2, int((right - left) / (60 * u))))
    xt, t = [], math.ceil(xlo / xstep - 1e-9) * xstep
    while t <= xhi + xstep * 1e-6:
        xt.append(t)
        t += xstep
    xtxt = axis_labels(xt, conf) if len(xt) > 1 else ["".join(format_value(v, conf.get("unit"))) for v in xt]
    for t, txt in zip(xt, xtxt):
        xx = px(t)
        half = d.textlength(txt, font=f_tick) / 2
        if xx - half >= left - 4 * u and xx + half <= x1 + 4 * u:
            d.line((xx, top, xx, bottom), fill=theme.grid, width=lw1)
            d.text((xx, bottom + 3 * u), txt, font=f_tick, fill=theme.muted, anchor="mt")
    for (name, sconf, color, _), c in zip(series, counts):
        custom = sconf.get("custom") or {}
        alpha = int(255 * (custom.get("fillOpacity") if custom.get("fillOpacity") is not None else 80) / 100)
        width = custom.get("lineWidth") if custom.get("lineWidth") is not None else 1
        for i, n in enumerate(c):
            if not n:
                continue
            a, b = px(start + i * size), px(start + (i + 1) * size)
            gap = lw1 if b - a > 3 * lw1 else 0
            d.rectangle((a + gap / 2, py(n), max(a + gap / 2 + 1, b - gap / 2), bottom), fill=color + (alpha,),
                        outline=color + (255,) if width else None, width=max(1, round(width * u)))


# --- logs ------------------------------------------------------------------------------------

LOG_LEVELS = [("critical", ("crit", "critical", "fatal", "emerg", "emergency", "alert", "panic")),
              ("error", ("err", "error", "eror", "severe")),
              ("warning", ("warn", "warning", "wrn")),
              ("info", ("info", "inf", "information", "notice")),
              ("debug", ("debug", "dbug", "dbg")),
              ("trace", ("trace", "trc"))]


def logs_level(text):
    """The canonical level (critical, error, warning, info, debug, trace) a level word
    means, or "unknown"."""
    w = str(text or "").strip().lower()
    for level, words in LOG_LEVELS:
        if w in words:
            return level
    return "unknown"


def logs_color(theme, level):
    return {"critical": theme.critical, "error": theme.critical, "warning": theme.warning,
            "info": theme.good, "debug": theme.series[0], "trace": theme.series[6]}.get(level, theme.muted)


def logs_lines(g):
    """[(time or None, level, text)] from every frame that has a string line field: the
    line is the field called body/line/message (else the first string field), and the
    level is a level field, the field's labels, or "level=..." in the line."""
    out = []
    for fr in g.frames:
        strs = [f for f in fr.fields if f.type == "string"]
        line = next((f for f in strs if f.name.lower() in ("body", "line", "message", "content", "msg")),
                    next((f for f in strs if f.name.lower() not in ("level", "severity", "detected_level", "id",
                                                                     "tsns", "labels")), None))
        if line is None:
            continue
        tf = fr.time_field()
        lvl = next((f for f in strs if f.name.lower() in ("level", "severity", "detected_level")), None)
        label_level = (line.labels or {}).get("detected_level") or (line.labels or {}).get("level") or \
            (line.labels or {}).get("severity")
        for i, text in enumerate(line.values):
            if text is None:
                continue
            text = str(text)
            level = logs_level(lvl.values[i]) if lvl and i < len(lvl.values) else "unknown"
            if level == "unknown" and label_level:
                level = logs_level(label_level)
            if level == "unknown":
                m = re.search(r"(?i)\b(?:level|lvl|severity)=\"?(\w+)", text) or \
                    re.search(r"(?i)^\W*(\w+)\W", text)
                if m:
                    level = logs_level(m.group(1))
            t = tf.values[i] if tf and i < len(tf.values) else None
            out.append((t if isinstance(t, (int, float)) else None, level, text))
    return out


@drawer("logs")
def draw_logs(g):
    """A logs panel: lines with a level stripe, a time, and the message in a monospace
    font, newest first unless sortOrder says Ascending, wrapped if wrapLogMessage."""
    lines = logs_lines(g)
    if not lines:
        return g.no_data()
    d, u, theme, opts = g.d, g.u, g.theme, g.options
    desc = opts.get("sortOrder", "Descending") != "Ascending"
    if any(t is not None for t, _, _ in lines):
        lines.sort(key=lambda l: l[0] if l[0] is not None else 0, reverse=desc)
    x0, y0, x1, y1 = g.box
    f = g.font(11, mono=True)
    lh = f.size * 1.35
    cw = d.textlength("M", font=f)
    show_time = opts.get("showTime", False)
    wrap_on = opts.get("wrapLogMessage", False)
    stripe = 3 * u
    tx = x0 + stripe + 6 * u
    time_w = 0
    if show_time:
        time_w = cw * 24
    y = y0
    for t, level, text in lines:
        if y + lh > y1:
            break
        color = logs_color(theme, level)
        text = text.replace("\t", "  ").replace("\r", "")
        stamp = ""
        if show_time and t is not None:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) + f".{int(t * 1000) % 1000:03d}"
        body_x = tx + (time_w + 6 * u if show_time else 0)
        per = max(8, int((x1 - body_x) / cw))
        # Wrap by character count (monospace), so a long unbroken line still folds.
        chunks = [text[i:i + per] for i in range(0, len(text), per)] or [""]
        if not wrap_on:
            chunks = [squeeze(d, text, f, x1 - body_x)]
        else:
            chunks = chunks[:max(1, int((y1 - y) / lh))]
        h = lh * len(chunks)
        d.rounded_rectangle((x0, y + 1 * u, x0 + stripe, y + h - 1 * u), radius=u, fill=color)
        if stamp:
            d.text((tx, y), stamp, font=f, fill=theme.muted)
        for k, ch_ in enumerate(chunks):
            d.text((body_x, y + k * lh), ch_, font=f, fill=theme.ink_2)
        y += h + (2 * u if wrap_on else 0)


# --- xychart (a scatter plot) -----------------------------------------------------------------

def xy_pairs(g):
    """[(name, color, [(x, y)])]: with the panel's series mapping (manual), each pairs an x
    field with a y field by name; otherwise (auto) the first number field of the frame is x
    and each of the others is a y."""
    fields = {}
    for fr, f, conf, name in g.fields():
        fields[f.name] = (fr, f, conf, name)
    out = []

    def add(xf, yf, conf, name, color=None):
        pts = [(a, b) for a, b in zip(xf.values, yf.values)
               if isinstance(a, (int, float)) and isinstance(b, (int, float))
               and math.isfinite(a) and math.isfinite(b)]
        if pts:
            nums = [b for _, b in pts]
            out.append((name, color or g.display(nums[-1], conf, len(out))[2], pts, conf))

    manual = g.options.get("mapping") == "manual" or (g.options.get("series") and g.options.get("mapping") != "auto")
    if manual:
        for s in g.options.get("series") or []:
            xn = (((s.get("x") or {}).get("matcher") or {}).get("options")) if isinstance(s.get("x"), dict) else s.get("x")
            yn = (((s.get("y") or {}).get("matcher") or {}).get("options")) if isinstance(s.get("y"), dict) else s.get("y")
            if xn in fields and yn in fields:
                col = (s.get("color") or {}).get("fixed") if isinstance(s.get("color"), dict) else None
                add(fields[xn][1], fields[yn][1], fields[yn][2], fields[yn][3],
                    g.theme.color(col) if col else None)
        if out:
            return out
    for fr in g.frames:
        nums = [(f, c, n) for (frame, f, c, n) in g.fields() if frame is fr]
        if len(nums) >= 2:
            for f, c, n in nums[1:]:
                add(nums[0][0], f, c, n)
    return out


def xy_axis(vals, conf):
    """(lo, hi, ticks) around vals with a bit of padding (a scatter's axes needn't start
    at zero)."""
    lo, hi = min(vals), max(vals)
    if hi == lo:
        lo, hi = lo - 1, hi + 1
    pad = (hi - lo) * 0.05
    lo, hi = lo - pad, hi + pad
    if conf.get("min") is not None:
        lo = conf["min"]
    if conf.get("max") is not None:
        hi = conf["max"]
    step = nice_step(hi - lo, 5)
    ticks, t = [], math.ceil(lo / step - 1e-9) * step
    while t <= hi + step * 1e-6:
        ticks.append(t)
        t += step
    return lo, hi, ticks


@drawer("xychart")
def draw_xychart(g):
    """A scatter plot: points for each y field against an x field, sized by pointSize,
    with axes in the fields' units."""
    pairs = xy_pairs(g)
    if not pairs:
        return g.no_data()
    d, u, theme = g.d, g.u, g.theme
    f_tick = g.font(10.5)
    legend = g.options.get("legend") or {}
    box = g.box
    if bar_legend_on(legend, len(pairs), g) and len(pairs) > 1:
        box = bar_legend(g, [(p[0], p[1], "") for p in pairs], legend.get("placement") or "bottom", box)
    x0, y0, x1, y1 = box
    xs = [a for p in pairs for a, _ in p[2]]
    ys = [b for p in pairs for _, b in p[2]]
    xl, xh, xt = xy_axis(xs, {})
    yl, yh, yt = xy_axis(ys, pairs[0][3])
    ytxt = axis_labels(yt, pairs[0][3])
    xtxt = axis_labels(xt, {})
    left = x0 + max(d.textlength(t, font=f_tick) for t in ytxt) + 6 * u
    top, bottom, right = y0 + f_tick.size * 0.5, y1 - f_tick.size * 1.5, x1 - 10 * u
    if bottom - top < 10 * u or right - left < 10 * u:
        return g.message("too small to draw")
    lw1 = max(1, round(u))
    px = lambda v: left + (v - xl) / (xh - xl) * (right - left)  # noqa: E731
    py = lambda v: bottom - (v - yl) / (yh - yl) * (bottom - top)  # noqa: E731
    for t, txt in zip(yt, ytxt):
        d.line((left, py(t), right, py(t)), fill=theme.grid, width=lw1)
        d.text((left - 5 * u, py(t)), txt, font=f_tick, fill=theme.muted, anchor="rm")
    for t, txt in zip(xt, xtxt):
        d.line((px(t), top, px(t), bottom), fill=theme.grid, width=lw1)
        d.text((px(t), bottom + 3 * u), txt, font=f_tick, fill=theme.muted, anchor="mt")
    d.line((left, bottom, right, bottom), fill=theme.axis, width=lw1)
    d.line((left, top, left, bottom), fill=theme.axis, width=lw1)
    for name, color, pts, conf in pairs:
        size = ((conf.get("custom") or {}).get("pointSize") or {})
        size = size.get("fixed", 5) if isinstance(size, dict) else size
        r = max(1.5, float(size or 5) * u / 2)
        for a, b in pts[:5000]:
            d.ellipse((px(a) - r, py(b) - r, px(a) + r, py(b) + r), fill=color + (190,), outline=color + (255,))


# --- shared: the time axis these three panels sit on --------------------------------------

def timeline_axis(g, left, right, top, bottom, grid=True):
    """The bottom time axis, the way draw_timeseries makes it: nice ticks, faint
    vertical lines, labels under the plot (left out where they'd spill)."""
    d, u, theme = g.d, g.u, g.theme
    f_tick = g.font(10.5)
    lw1 = max(1, round(u))
    span = max(1e-9, g.end - g.start)
    times, tfmt = time_ticks(g.start, g.end, right - left, lambda s: d.textlength(s, font=f_tick))
    for tt in times:
        xx = left + (tt - g.start) / span * (right - left)
        txt = time.strftime(tfmt, time.localtime(tt))
        half = d.textlength(txt, font=f_tick) / 2
        if xx - half >= left - 4 * u and xx + half <= right + 4 * u:
            if grid:
                d.line((xx, top, xx, bottom), fill=theme.grid, width=lw1)
            d.text((xx, bottom + 3 * u), txt, font=f_tick, fill=theme.muted, anchor="mt")


def timeline_contrast(g, rgb):
    """Text color that reads on rgb: the theme's surface on light fills, its ink on dark."""
    lum = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
    return g.theme.surface if lum > 140 else g.theme.ink


def timeline_legend_rows(g, items, width):
    """Legend items [(text, color)] laid out in rows that fit width -> [[(text, color, w)]]."""
    f, u = g.font(11), g.u
    rows, row, rx = [], [], 0
    for text, color in items:
        w = 10 * u + 5 * u + g.d.textlength(text, font=f) + 14 * u
        if row and rx + w > width:
            rows.append(row)
            row, rx = [], 0
        row.append((text, color, w))
        rx += w
    return rows + ([row] if row else [])


def timeline_draw_legend(g, rows, x0, y0, x1):
    """Draw the rows from timeline_legend_rows: a square swatch and the state's text."""
    f, u, d = g.font(11), g.u, g.d
    lh = f.size * 1.45
    for row in rows:
        lx = x0
        for text, color, w in row:
            cy = y0 + lh / 2
            d.rounded_rectangle((lx, cy - 5 * u, lx + 10 * u, cy + 5 * u), radius=2 * u, fill=color)
            d.text((lx + 15 * u, cy), squeeze(d, text, f, x1 - lx - 15 * u), font=f,
                   fill=g.theme.ink_2, anchor="lm")
            lx += w
        y0 += lh


def timeline_legend_wanted(g):
    legend = g.options.get("legend") or {}
    return legend.get("showLegend", True) and legend.get("displayMode") != "hidden"


# --- shared: the states of state-timeline and status-history ----------------------------

def timeline_step_label(conf, value):
    """Which threshold step a number is in, in words: "< 50", "50-80", ">= 80"."""
    steps = (conf.get("thresholds") or {}).get("steps") or []
    vals = [s.get("value") for s in steps]
    k = 0
    for i, v in enumerate(vals):
        if v is None or (isinstance(v, (int, float)) and value >= v):
            k = i
    lo = vals[k] if k < len(vals) else None
    hi = vals[k + 1] if k + 1 < len(vals) else None
    fmt = lambda x: "".join(format_value(x, conf.get("unit"), conf.get("decimals")))  # noqa: E731
    if lo is None and hi is None:
        return "all"
    if lo is None:
        return "< " + fmt(hi)
    return f">= {fmt(lo)}" if hi is None else f"{fmt(lo)}-{fmt(hi)}"


def timeline_state(g, conf, value, index, palette):
    """(text, color, legend label) for one value, or None where nothing is drawn (null
    without a mapping). Mappings and thresholds go through g.display; strings and
    booleans with no mapping get a palette color each, in the order they first show up."""
    if isinstance(value, float) and value != value:
        value = None
    if value is None:
        if not map_value(conf, None):
            return None
        text, suffix, color = g.display(None, conf, index)
        return text + suffix, color, text + suffix
    if map_value(conf, value):
        text, suffix, color = g.display(value, conf, index)
        return text + suffix, color, text + suffix
    if isinstance(value, (str, bool)):
        text = str(value).lower() if isinstance(value, bool) else value
        if text not in palette:
            palette[text] = g.theme.series[len(palette) % len(g.theme.series)]
        return text, palette[text], text
    if not isinstance(value, (int, float)):
        return None
    text, suffix, color = g.display(value, conf, index)
    mode = (conf.get("color") or {}).get("mode") or "thresholds"
    # Thresholds list as their steps; continuous colors have no discrete states to list.
    label = timeline_step_label(conf, value) if mode == "thresholds" else None
    return text + suffix, color, label


def timeline_lanes(g):
    """One lane per series: [{name, conf, pts: [(t, state)]}], and the legend's states
    [(label, color)] in the order they appear. State is timeline_state's answer."""
    lanes, legend, palette, cache = [], {}, {}, {}
    for fr, f, conf, name in g.fields(("number", "string", "boolean")):
        tf = fr.time_field()
        if not tf:
            continue
        idx = len(lanes)
        if str((conf.get("color") or {}).get("mode") or "").startswith("palette"):
            conf = dict(conf, color={"mode": "thresholds"})  # a state is a step, not a series
        pts = sorted(((t, v) for t, v in zip(tf.values, f.values) if t is not None),
                     key=lambda p: p[0])
        out = []
        for t, v in pts:
            key = (idx, v if not isinstance(v, float) or v == v else None)
            try:
                st = cache[key]
            except (KeyError, TypeError):
                st = cache[key] = timeline_state(g, conf, v, idx, palette)
            out.append((t, st))
            if st and st[2] is not None and st[2] not in legend and len(legend) < 40:
                legend[st[2]] = st[1]
        if out:
            lanes.append({"name": name, "conf": conf, "pts": out})
    return lanes, list(legend.items())


def timeline_layout(g, names, legend, label_axis=True):
    """The plot's box (left, top, right, bottom) with room for lane names on the left, the
    time axis under it and the legend below that; draws the legend too."""
    d, u = g.d, g.u
    x0, y0, x1, y1 = g.box
    f_tick = g.font(10.5)
    if legend and timeline_legend_wanted(g):
        rows = timeline_legend_rows(g, legend, x1 - x0)
        most = max(1, int((y1 - y0) * 0.3 / (g.font(11).size * 1.45)))
        rows = rows[:most]
        lh = len(rows) * g.font(11).size * 1.45
        timeline_draw_legend(g, rows, x0, y1 - lh, x1)
        y1 -= lh + 4 * u
    left = x0
    if names:
        left += min((x1 - x0) * 0.3, max(d.textlength(n, font=f_tick) for n in names)) + 8 * u
    bottom = y1 - f_tick.size * 1.5
    return left, y0 + f_tick.size * 0.3, x1, bottom


def timeline_names(g, names, left, top, bottom):
    """Each lane's name, right-aligned against the plot at its lane's middle."""
    u, d = g.u, g.d
    f = g.font(10.5)
    x0 = g.box[0]
    lane_h = (bottom - top) / max(1, len(names))
    for i, n in enumerate(names):
        d.text((left - 6 * u, top + (i + 0.5) * lane_h), squeeze(d, n, f, left - 6 * u - x0, middle=True),
               font=f, fill=g.theme.muted, anchor="rm")


def timeline_cell(g, box, color, alpha, text, show, align, line_width, font):
    """One filled cell with its state's text inside when it fits (or, with show == always,
    cut to fit). Blends over the surface so the text color can be chosen for it."""
    d, u = g.d, g.u
    x0, y0, x1, y1 = box
    if x1 - x0 < 0.5 or y1 - y0 < 0.5:
        return
    r = min(2 * u, (y1 - y0) / 4, max(0.0, (x1 - x0) / 4))
    d.rounded_rectangle((x0, y0, max(x1, x0 + 1), y1), radius=r, fill=color + (alpha,),
                        outline=color if line_width else None, width=max(1, round(line_width * u)))
    if show == "never" or not text:
        return
    room = x1 - x0 - 8 * u
    if room < 6 * u or font.size * 1.1 > y1 - y0:
        return
    need = d.textlength(text, font=font)
    if need > room:
        if show != "always":
            return
        text = squeeze(d, text, font, room)
        if not text:
            return
        need = d.textlength(text, font=font)
    bg = mix(g.theme.surface, color, alpha / 255)
    ink = timeline_contrast(g, bg)
    cy = (y0 + y1) / 2
    if align == "center":
        d.text(((x0 + x1) / 2, cy), text, font=font, fill=ink, anchor="mm")
    elif align == "right":
        d.text((x1 - 4 * u, cy), text, font=font, fill=ink, anchor="rm")
    else:
        d.text((x0 + 4 * u, cy), text, font=font, fill=ink, anchor="lm")


# --- state-timeline ---------------------------------------------------------------------------

def timeline_segments(g, pts, merge, span_nulls):
    """[(t0, t1, state)]: each value holds from its time until the next one (the last until
    the end of the range); equal neighbours merge into one; a null breaks the run unless
    spanNulls says to bridge it."""
    out = []
    last = None  # the segment a null might be bridged from
    for i, (t, st) in enumerate(pts):
        t_end = pts[i + 1][0] if i + 1 < len(pts) else max(g.end, t)
        if st is None:
            if span_nulls is True or (isinstance(span_nulls, (int, float)) and not isinstance(span_nulls, bool)
                                      and span_nulls and last and t_end - last[1] <= span_nulls / 1000):
                if last:
                    last[1] = t_end
            else:
                last = None
            continue
        if merge and last and last[2][:2] == st[:2] and last[1] >= t - 1e-9:
            last[1] = t_end
        else:
            last = [t, t_end, st]
            out.append(last)
    return out


@drawer("state-timeline")
def draw_state_timeline(g):
    """Grafana's state timeline: a lane for each series, with a bar for every stretch of
    time it held one state, colored by the value's mapping or threshold and labelled with
    the state when there's room."""
    lanes, legend = timeline_lanes(g)
    if not lanes:
        return g.no_data()
    u, o = g.u, g.options
    left, top, right, bottom = timeline_layout(g, [ln["name"] for ln in lanes], legend)
    if bottom - top < 10 * u or right - left < 20 * u:
        return g.message("too small to draw")
    span = max(1e-9, g.end - g.start)

    def px(t):
        return left + min(max((t - g.start) / span, 0.0), 1.0) * (right - left)

    timeline_axis(g, left, right, top, bottom)
    timeline_names(g, [ln["name"] for ln in lanes], left, top, bottom)
    lane_h = (bottom - top) / len(lanes)
    row_h = o.get("rowHeight")
    row_h = 0.9 if row_h is None else min(1.0, max(0.05, float(row_h)))
    font = g.font(11)
    merge = o.get("mergeValues", True)
    show, align = o.get("showValue") or "auto", o.get("alignValue") or "left"
    for i, ln in enumerate(lanes):
        custom = ln["conf"].get("custom") or {}
        fill = custom.get("fillOpacity")
        alpha = int(255 * (70 if fill is None else min(100, max(0, fill))) / 100)
        lw = custom.get("lineWidth") or 0
        cy = top + (i + 0.5) * lane_h
        half = lane_h * row_h / 2
        for t0, t1, st in timeline_segments(g, ln["pts"], merge, custom.get("spanNulls")):
            if t1 <= g.start or t0 >= g.end:
                continue
            a, b = px(t0), px(t1)
            gap = 0.5 * u if not merge else 0
            timeline_cell(g, (a + gap, cy - half, b - gap, cy + half), st[1], alpha, st[0],
                          show, align, lw, font)


# --- status-history --------------------------------------------------------------------------

def history_cells(g, pts, left, right):
    """[(x0, x1, state)] in pixels for one lane: a cell for each point, reaching halfway to
    its neighbours. Points too close together to see are folded into 3-pixel columns
    (the latest state in each wins), so a long query still draws."""
    span = max(1e-9, g.end - g.start)
    width = right - left
    xs = [left + (t - g.start) / span * width for t, _ in pts]
    if not xs:
        return []
    bounds = [xs[0] - (xs[1] - xs[0]) / 2 if len(xs) > 1 else xs[0] - width / 40]
    bounds += [(a + b) / 2 for a, b in zip(xs, xs[1:])]
    bounds.append(xs[-1] + (xs[-1] - xs[-2]) / 2 if len(xs) > 1 else xs[-1] + width / 40)
    step = 3 * g.u
    typical = (bounds[-1] - bounds[0]) / max(1, len(xs))
    if typical < step:
        binned = {}
        for x, (_, st) in zip(xs, pts):
            k = int((x - left) // step)
            if st is not None or k not in binned:
                binned[k] = st
        return [(left + k * step, left + (k + 1) * step, st) for k, st in sorted(binned.items())
                if st is not None]
    return [(a, b, st) for a, b, (_, st) in zip(bounds, bounds[1:], pts) if st is not None]


@drawer("status-history")
def draw_status_history(g):
    """Grafana's status history: a row for each series and a cell for each point in time,
    colored by the value's mapping or threshold."""
    lanes, legend = timeline_lanes(g)
    if not lanes:
        return g.no_data()
    u, o = g.u, g.options
    left, top, right, bottom = timeline_layout(g, [ln["name"] for ln in lanes], legend)
    if bottom - top < 10 * u or right - left < 20 * u:
        return g.message("too small to draw")
    timeline_axis(g, left, right, top, bottom, grid=False)
    timeline_names(g, [ln["name"] for ln in lanes], left, top, bottom)
    lane_h = (bottom - top) / len(lanes)
    num = lambda k: min(1.0, max(0.05, float(o[k]))) if o.get(k) is not None else 0.9  # noqa: E731
    col_w, row_h = num("colWidth"), num("rowHeight")
    font = g.font(10)
    show, align = o.get("showValue") or "auto", o.get("alignValue") or "center"
    for i, ln in enumerate(lanes):
        custom = ln["conf"].get("custom") or {}
        fill = custom.get("fillOpacity")
        alpha = int(255 * (70 if fill is None else min(100, max(0, fill))) / 100)
        cy = top + (i + 0.5) * lane_h
        half = lane_h * row_h / 2
        for a, b, st in history_cells(g, ln["pts"], left, right):
            if b <= left or a >= right:
                continue
            a, b = max(a, left), min(b, right)
            inset = (b - a) * (1 - col_w) / 2
            timeline_cell(g, (a + inset, cy - half, b - inset, cy + half), st[1], alpha, st[0],
                          show, align, custom.get("lineWidth") or 0, font)


# --- heatmap --------------------------------------------------------------------------------

HEATMAP_SCHEMES = {  # d3's color schemes as a few stops each; the second item is Grafana's
    # "invert" (a dark theme runs 'dark' and 'always' schemes backwards, so low is dim)
    "BrBG": ("543005 8c510a bf812d dfc27d f6e8c3 f5f5f5 c7eae5 80cdc1 35978f 01665e 003c30", "always"),
    "PiYG": ("8e0152 c51b7d de77ae f1b6da fde0ef f7f7f7 e6f5d0 b8e186 7fbc41 4d9221 276419", "always"),
    "PRGn": ("40004b 762a83 9970ab c2a5cf e7d4e8 f7f7f7 d9f0d3 a6dba0 5aae61 1b7837 00441b", "always"),
    "PuOr": ("7f3b08 b35806 e08214 fdb863 fee0b6 f7f7f7 d8daeb b2abd2 8073ac 542788 2d004b", "always"),
    "RdBu": ("67001f b2182b d6604d f4a582 fddbc7 f7f7f7 d1e5f0 92c5de 4393c3 2166ac 053061", "always"),
    "RdGy": ("67001f b2182b d6604d f4a582 fddbc7 ffffff e0e0e0 bababa 878787 4d4d4d 1a1a1a", "always"),
    "RdYlBu": ("a50026 d73027 f46d43 fdae61 fee090 ffffbf e0f3f8 abd9e9 74add1 4575b4 313695", "always"),
    "RdYlGn": ("a50026 d73027 f46d43 fdae61 fee08b ffffbf d9ef8b a6d96a 66bd63 1a9850 006837", "always"),
    "Spectral": ("9e0142 d53e4f f46d43 fdae61 fee08b ffffbf e6f598 abdda4 66c2a5 3288bd 5e4fa2", "always"),
    "Blues": ("f7fbff deebf7 c6dbef 9ecae1 6baed6 4292c6 2171b5 08519c 08306b", "dark"),
    "Greens": ("f7fcf5 e5f5e0 c7e9c0 a1d99b 74c476 41ab5d 238b45 006d2c 00441b", "dark"),
    "Greys": ("ffffff f0f0f0 d9d9d9 bdbdbd 969696 737373 525252 252525 000000", "dark"),
    "Oranges": ("fff5eb fee6ce fdd0a2 fdae6b fd8d3c f16913 d94801 a63603 7f2704", "dark"),
    "Purples": ("fcfbfd efedf5 dadaeb bcbddc 9e9ac8 807dba 6a51a3 54278f 3f007d", "dark"),
    "Reds": ("fff5f0 fee0d2 fcbba1 fc9272 fb6a4a ef3b2c cb181d a50f15 67000d", "dark"),
    "Turbo": ("30123b 4662d7 36aaf9 1ae4b6 72fe5e c7ef34 faba39 f66b19 ca2a04 7a0403", "light"),
    "Cividis": ("00204d 00336f 39486b 575c6e 707173 8a8779 a69d75 c4b56c e4cf5b ffea46", "light"),
    "Viridis": ("440154 482878 3e4989 31688e 26828e 1f9e89 35b779 6ece58 b5de2b fde725", "light"),
    "Magma": ("000004 180f3e 451077 721f81 9f2f7f cd4071 f1605d fd9567 feca8d fcfdbf", "light"),
    "Inferno": ("000004 160b39 420a68 6a176e 932667 bc3754 dd513a f37819 fca50a f6d746 fcffa4", "light"),
    "Plasma": ("0d0887 47039f 7301a8 9c179e bd3786 d8576b ed7953 fa9e3b fdc926 f0f921", "light"),
    "Warm": ("6e40aa be3caf fe4b83 ff7747 e3a42d aff05b", "light"),
    "Cool": ("6e40aa 4776de 1ac7c2 1ddfa3 aff05b", "light"),
    "Cubehelix": ("000000 19122f 1c4a4a 4a7a3a b38a5a e0a6c8 ffffff", "light"),
    "BuGn": ("f7fcfd e5f5f9 ccece6 99d8c9 66c2a4 41ae76 238b45 006d2c 00441b", "dark"),
    "BuPu": ("f7fcfd e0ecf4 bfd3e6 9ebcda 8c96c6 8c6bb1 88419d 810f7c 4d004b", "dark"),
    "GnBu": ("f7fcf0 e0f3db ccebc5 a8ddb5 7bccc4 4eb3d3 2b8cbe 0868ac 084081", "dark"),
    "OrRd": ("fff7ec fee8c8 fdd49e fdbb84 fc8d59 ef6548 d7301f b30000 7f0000", "dark"),
    "PuBuGn": ("fff7fb ece2f0 d0d1e6 a6bddb 67a9cf 3690c0 02818a 016c59 014636", "dark"),
    "PuBu": ("fff7fb ece7f2 d0d1e6 a6bddb 74a9cf 3690c0 0570b0 045a8d 023858", "dark"),
    "PuRd": ("f7f4f9 e7e1ef d4b9da c994c7 df65b0 e7298a ce1256 980043 67001f", "dark"),
    "RdPu": ("fff7f3 fde0dd fcc5c0 fa9fb5 f768a1 dd3497 ae017e 7a0177 49006a", "dark"),
    "YlGnBu": ("ffffd9 edf8b1 c7e9b4 7fcdbb 41b6c4 1d91c0 225ea8 253494 081d58", "dark"),
    "YlGn": ("ffffe5 f7fcb9 d9f0a3 addd8e 78c679 41ab5d 238443 006837 004529", "dark"),
    "YlOrBr": ("ffffe5 fff7bc fee391 fec44f fe9929 ec7014 cc4c02 993404 662506", "dark"),
    "YlOrRd": ("ffffcc ffeda0 fed976 feb24c fd8d3c fc4e2a e31a1c bd0026 800026", "dark"),
    "Rainbow": ("6e40aa be3caf fe4b83 ff7747 e2b933 aff05b 52f667 1ddfa3 23abd8 4c6edb 6e40aa", "always"),
    "Sinebow": ("ff4040 e2b933 aff05b 52f667 1ddfa3 23abd8 4c6edb 8f5bd6 ff4040", "always"),
}


def heatmap_palette(g, color):
    """The heatmap's colors, lowest to highest, as a (steps, 4) uint8 array: a d3 scheme
    (the right way up for a dark theme, and flipped again by reverse), or one color that
    gets more opaque."""
    steps = max(2, min(128, int(color.get("steps") or 64)))
    ramp = np.linspace(0, 1, steps)
    if color.get("mode") == "opacity":
        rgb = g.theme.color(color.get("fill") or "dark-orange")
        out = np.zeros((steps, 4), np.uint8)
        out[:, :3] = rgb
        out[:, 3] = np.round(ramp * 255)
        return out
    stops, invert = HEATMAP_SCHEMES.get(color.get("scheme"), HEATMAP_SCHEMES["Spectral"])
    pts = np.array([hex_rgb(s) for s in stops.split()], float)
    where = np.linspace(0, 1, len(pts))
    rgb = np.stack([np.interp(ramp, where, pts[:, k]) for k in range(3)], 1)
    if invert in ("always", "dark"):
        rgb = rgb[::-1]
    out = np.full((steps, 4), 255, np.uint8)
    out[:, :3] = np.round(rgb)
    return out[::-1] if color.get("reverse") else out


def heatmap_colorize(z, palette, lo, hi, color, floor):
    """(ny, nx, 4) colors for the cell values z: scaled between lo and hi (linear, or
    by exponent), into the palette; cells with no value, or at or under floor, are clear."""
    with np.errstate(invalid="ignore"):
        shown = np.isfinite(z) & (z > floor)
        t = np.clip((np.where(shown, z, lo) - lo) / (hi - lo), 0, 1) if hi > lo else np.ones(z.shape)
    if color.get("scale") == "exponential":
        t = t ** float(color.get("exponent") or 0.5)
    idx = np.minimum(len(palette) - 1, (t * len(palette)).astype(int))
    out = palette[idx].copy()
    out[..., 3] = np.where(shown, out[..., 3], 0)
    return out


def heatmap_cells_grid(xlo, xhi, ylo, yhi, v):
    """Loose cells (arrays) -> a grid: sorted column starts and ends, row starts and ends,
    and the values as (rows, columns)."""
    ux, ix = np.unique(xlo, return_inverse=True)
    uy, iy = np.unique(ylo, return_inverse=True)
    ex, ey = np.full(len(ux), -np.inf), np.full(len(uy), -np.inf)
    np.maximum.at(ex, ix, xhi)
    np.maximum.at(ey, iy, yhi)
    z = np.full((len(uy), len(ux)), np.nan)
    z[iy, ix] = v
    return {"xlo": ux, "xhi": ex, "ylo": uy, "yhi": ey, "z": z}


def heatmap_size(sorted_values, fallback):
    """The usual gap between sorted values, or fallback if there is just one."""
    gaps = np.diff(np.unique(sorted_values))
    gaps = gaps[gaps > 0]
    return float(np.median(gaps)) if len(gaps) else fallback


def heatmap_num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def heatmap_from_rows(g):
    """Pre-bucketed series (Prometheus's heatmap format: one per `le`, or fields named for
    their bucket) -> a grid with ordinal rows; None if the frames aren't like that.
    Prometheus buckets count everything below, so each is made into its own count."""
    cols, cumulative = {}, False
    layout = ((g.options.get("rowsFrame") or {}).get("layout")) or "auto"
    for fr in g.frames:
        tf = fr.time_field()
        if not tf:
            continue
        for f in fr.fields:
            if f is tf or f.type != "number":
                continue
            le = (f.labels or {}).get("le")
            key = heatmap_num(le if le is not None else f.name)
            if key is None:
                continue
            cumulative = cumulative or le is not None
            cols.setdefault(key, {}).update({t: v for t, v in zip(tf.values, f.values) if t is not None})
    if not cols:
        return None
    keys = sorted(cols)
    times = sorted({t for c in cols.values() for t in c})
    z = np.full((len(keys), len(times)), np.nan)
    at = {t: i for i, t in enumerate(times)}
    for r, k in enumerate(keys):
        for t, v in cols[k].items():
            if isinstance(v, (int, float)) and v == v:
                z[r, at[t]] = v
    if layout == "le" or (layout == "auto" and cumulative):
        seen = np.isfinite(z)
        z = np.diff(np.nan_to_num(z), axis=0, prepend=0.0)
        z = np.where(seen, np.maximum(z, 0), np.nan)
    elif layout == "ge":
        seen = np.isfinite(z)
        z = np.nan_to_num(z) - np.vstack([np.nan_to_num(z)[1:], np.zeros((1, z.shape[1]))])
        z = np.where(seen, np.maximum(z, 0), np.nan)
    ts = np.array(times, float)
    dx = heatmap_size(ts, max(1.0, (g.end - g.start) / 40))
    n = len(keys)
    unit = (g.options.get("yAxis") or {}).get("unit")
    labels = ["+Inf" if k == math.inf else "".join(format_value(k, unit or heatmap_unit(g),
                                                               (g.options.get("yAxis") or {}).get("decimals")))
              for k in keys]
    return {"xlo": ts - dx / 2, "xhi": ts + dx / 2, "ylo": np.arange(n, dtype=float),
            "yhi": np.arange(n, dtype=float) + 1, "z": z, "labels": labels}


def heatmap_unit(g):
    """The unit of the first number field, for axis labels when yAxis doesn't set one."""
    fields = g.fields()
    return fields[0][2].get("unit") if fields else None


def heatmap_from_cells(g):
    """Grafana's own heatmap-cells frame (xMin, xMax, yMin, yMax, count) -> a grid, or
    None if no frame looks like one."""
    for fr in g.frames:
        by = {f.name.lower(): f for f in fr.fields}
        count = by.get("count") or by.get("value")
        ylo_f = by.get("ymin") or by.get("y")
        xlo_f = by.get("xmin") or by.get("x") or fr.time_field()
        if not (count and ylo_f and xlo_f):
            continue
        if fr.meta.get("type") not in ("heatmap-cells", None) and "ymin" not in by:
            continue
        n = min(len(count.values), len(ylo_f.values), len(xlo_f.values))
        tof = lambda f: np.array([np.nan if v is None else v for v in f.values[:n]], float)  # noqa: E731
        xlo, ylo, v = tof(xlo_f), tof(ylo_f), tof(count)
        scale = 1 / 1000 if xlo_f.type != "time" and np.nanmax(xlo, initial=0) > 1e11 else 1
        xlo = xlo * scale
        xhi = tof(by["xmax"]) * scale if "xmax" in by else None
        yhi = tof(by["ymax"]) if "ymax" in by else None
        ok = np.isfinite(xlo) & np.isfinite(ylo)
        if not ok.any():
            continue
        xlo, ylo, v = xlo[ok], ylo[ok], v[ok]
        xhi = xhi[ok] if xhi is not None else xlo + heatmap_size(xlo, 60)
        yhi = yhi[ok] if yhi is not None else ylo + heatmap_size(ylo, 1)
        return heatmap_cells_grid(xlo, np.where(np.isfinite(xhi), xhi, xlo), ylo,
                                  np.where(np.isfinite(yhi), yhi, ylo), v)
    return None


def heatmap_interval(text):
    """A bucket size like "30s" or "5m" in seconds, or None."""
    try:
        return interval_to_ms(str(text)) / 1000 or None
    except Exception:
        return None


def heatmap_from_series(g, w_css, h_css):
    """Raw series, counted into buckets by options.calculation: by size or count across,
    and linear or log buckets up the side. Defaults aim for cells about 10 CSS pixels."""
    t, v = [], []
    for _, _, _, pts in g.series():
        for a, b in pts:
            if b is not None and math.isfinite(b) and g.start <= a <= g.end:
                t.append(a)
                v.append(b)
    if not t:
        return None
    t, v = np.array(t), np.array(v)
    calc = g.options.get("calculation") or {}
    xb, yb = calc.get("xBuckets") or {}, calc.get("yBuckets") or {}
    span = g.end - g.start
    xval = heatmap_num(xb.get("value"))
    if xb.get("mode") == "size" and heatmap_interval(xb.get("value")):
        dx = heatmap_interval(xb.get("value"))
    else:
        dx = span / max(1, min(500, int(xval) if xval and xb.get("mode") == "count" else max(5, w_css / 10)))
    dx = max(dx, span / 800)
    x_start = math.floor(g.start / dx) * dx
    nx = int(math.ceil((g.end - x_start) / dx))
    scale = yb.get("scale") or {}
    kind, base = scale.get("type") or "linear", int(scale.get("log") or 2)
    yval = heatmap_num(yb.get("value"))
    if kind == "log":
        keep = v > 0
        t, v = t[keep], np.log(v[keep]) / math.log(base)
        if not len(v):
            return None
        lo, hi = math.floor(v.min()), math.ceil(v.max())
        dy = 1.0
        hi = max(hi, lo + 1)
    else:
        lo_v, hi_v = float(v.min()), float(v.max())
        if hi_v == lo_v:
            hi_v = lo_v + 1
        if yb.get("mode") == "size" and yval:
            dy = yval
        else:
            dy = nice_step(hi_v - lo_v, int(yval) if yval and yb.get("mode") == "count"
                           else max(4, h_css / 10))
        lo = math.floor(lo_v / dy + 1e-9) * dy
        hi = max(math.ceil(hi_v / dy - 1e-9) * dy, lo + dy)
    ny = max(1, int(round((hi - lo) / dy)))
    counts, _, _ = np.histogram2d(v, t, bins=[ny, nx],
                                  range=[(lo, lo + ny * dy), (x_start, x_start + nx * dx)])
    ye = lo + np.arange(ny + 1) * dy
    grid = {"xlo": x_start + np.arange(nx) * dx, "xhi": x_start + (np.arange(nx) + 1) * dx,
            "ylo": ye[:-1], "yhi": ye[1:], "z": np.where(counts > 0, counts, np.nan)}
    if kind == "log":
        grid["log"] = base
    return grid


def heatmap_grid(g, w_css, h_css):
    """The heatmap's cells as a grid, from whichever way the data came."""
    if not g.options.get("calculate"):
        grid = heatmap_from_cells(g) or heatmap_from_rows(g)
        if grid:
            return grid
    return heatmap_from_series(g, w_css, h_css)


def heatmap_yticks(g, grid, plo, phi, height):
    """[(position, label)] for the y axis: every bucket's name for ordinal rows (thinned to
    fit), powers of the base for log buckets, nice numbers otherwise."""
    ax = g.options.get("yAxis") or {}
    conf = {"unit": ax.get("unit") or heatmap_unit(g), "decimals": ax.get("decimals")}
    want = max(2, int(height / (24 * g.u)))
    if "labels" in grid:
        n = len(grid["labels"])
        stride = max(1, int(math.ceil(n / want)))
        return [(i + 0.5, grid["labels"][i]) for i in range(0, n, stride)]
    if grid.get("log"):
        stride = max(1, int(math.ceil((phi - plo) / want)))
        ks = range(int(math.ceil(plo - 1e-9)), int(math.floor(phi + 1e-9)) + 1, stride)
        return [(k, "".join(format_value(float(grid["log"]) ** k, conf["unit"], conf["decimals"])))
                for k in ks]
    step = nice_step(phi - plo, want)
    ticks, t = [], math.ceil(plo / step - 1e-9) * step
    while t <= phi + step * 1e-6:
        ticks.append(t)
        t += step
    return list(zip(ticks, axis_labels(ticks, conf)))


def heatmap_paint(g, grid, colors, box, y_view, reverse, gap):
    """Paint the colored cells into box, one image for the whole grid: every pixel looks up
    its column and row, so it costs the size of the plot, not the number of cells. Gap
    pixels at each cell's edges are left clear."""
    left, top, right, bottom = [int(round(v)) for v in box]
    W, H = right - left, bottom - top
    if W < 1 or H < 1:
        return
    plo, phi = y_view
    span = max(1e-9, g.end - g.start)
    c = (np.arange(W) + 0.5) / W
    t = g.start + c * span
    ci = np.clip(np.searchsorted(grid["xlo"], t, "right") - 1, 0, len(grid["xlo"]) - 1)
    lo_px, hi_px = (grid["xlo"][ci] - g.start) / span * W, (grid["xhi"][ci] - g.start) / span * W
    half = np.minimum(gap / 2, (hi_px - lo_px) / 4)
    col_ok = (t >= grid["xlo"][ci]) & (t < grid["xhi"][ci]) & (c * W >= lo_px + half) & (c * W <= hi_px - half)
    r = (np.arange(H) + 0.5) / H
    pos = plo + r * (phi - plo) if reverse else phi - r * (phi - plo)
    ri = np.clip(np.searchsorted(grid["ylo"], pos, "right") - 1, 0, len(grid["ylo"]) - 1)
    a, b = grid["ylo"][ri], grid["yhi"][ri]
    pix = (phi - plo) / H
    rlo, rhi = (pos - a) / pix, (b - pos) / pix
    half = np.minimum(gap / 2, (rlo + rhi) / 4)
    row_ok = (pos >= a) & (pos < b) & (rlo >= half) & (rhi >= half)
    out = colors[ri[:, None], ci[None, :]].copy()
    out[..., 3] = np.where(row_ok[:, None] & col_ok[None, :], out[..., 3], 0)
    g.img.paste(Image.fromarray(out, "RGBA"), (left, top), Image.fromarray(out, "RGBA"))


def heatmap_legend(g, palette, lo, hi, x, y, width):
    """A small color bar with the lowest and highest cell value under it."""
    d, u = g.d, g.u
    f = g.font(10)
    bar = Image.fromarray(np.repeat(palette[None, :, :], 1, 0), "RGBA").resize(
        (max(1, int(width)), max(1, int(6 * u))), Image.Resampling.NEAREST)
    g.img.paste(bar, (int(x), int(y)), bar)
    conf = {"unit": (g.options.get("cellValues") or {}).get("unit"),
            "decimals": (g.options.get("cellValues") or {}).get("decimals")}
    show = lambda v: "".join(format_value(v, conf["unit"], conf["decimals"]))  # noqa: E731
    d.text((x, y + 7 * u), show(lo), font=f, fill=g.theme.muted, anchor="lt")
    d.text((x + width, y + 7 * u), show(hi), font=f, fill=g.theme.muted, anchor="rt")


@drawer("heatmap")
def draw_heatmap(g):
    """Grafana's heatmap: time across, buckets up, each cell colored by its count. The data
    is pre-bucketed (Prometheus's heatmap format, or Grafana's cells frame) or raw series
    counted here, per options.calculate."""
    d, u, theme, o = g.d, g.u, g.theme, g.options
    x0, y0, x1, y1 = g.box
    f_tick = g.font(10.5)
    grid = heatmap_grid(g, (x1 - x0) / u, (y1 - y0) / u)
    if not grid or not np.isfinite(grid["z"]).any():
        return g.no_data()
    color = dict({"scheme": "Oranges", "fill": "dark-orange", "steps": 64, "exponent": 0.5},
                 **(o.get("color") or {}))
    color.setdefault("mode", "scheme")
    ax = o.get("yAxis") or {}
    floor = ((o.get("filterValues") or {}).get("le"))
    floor = 1e-9 if floor is None else floor
    z = grid["z"]
    shown = z[np.isfinite(z) & (z > floor)]
    if not len(shown):
        return g.no_data()
    lo = color["min"] if color.get("min") is not None else float(shown.min())
    hi = color["max"] if color.get("max") is not None else float(shown.max())
    palette = heatmap_palette(g, color)
    colors = heatmap_colorize(z, palette, lo, hi, color, floor)

    plo, phi = float(grid["ylo"].min()), float(grid["yhi"].max())
    if "labels" not in grid:
        plo = ax["min"] if ax.get("min") is not None and not grid.get("log") else plo
        phi = ax["max"] if ax.get("max") is not None and not grid.get("log") else phi
        if phi <= plo:
            phi = plo + 1
    reverse = bool(ax.get("reverse"))
    legend_h = 0
    if (o.get("legend") or {}).get("show", True) and (y1 - y0) > 110 * u:
        legend_h = 20 * u
    ticks = heatmap_yticks(g, grid, plo, phi, (y1 - y0) - legend_h)
    hidden = ax.get("axisPlacement") == "hidden"
    on_right = ax.get("axisPlacement") == "right"
    label_w = 0 if hidden else max([d.textlength(t, font=f_tick) for _, t in ticks] or [0]) + 6 * u
    left, right = x0 + (0 if on_right else label_w), x1 - (label_w if on_right else 0)
    top, bottom = y0 + f_tick.size * 0.5, y1 - f_tick.size * 1.5 - legend_h
    if bottom - top < 10 * u or right - left < 20 * u:
        return g.message("too small to draw")

    def py(p):
        frac = (p - plo) / (phi - plo)
        return top + frac * (bottom - top) if reverse else bottom - frac * (bottom - top)

    lw1 = max(1, round(u))
    for p, text in ticks:
        if not plo - 1e-9 <= p <= phi + 1e-9:
            continue
        yy = py(p)
        d.line((left, yy, right, yy), fill=theme.grid, width=lw1)
        if not hidden:
            if on_right:
                d.text((right + 5 * u, yy), text, font=f_tick, fill=theme.muted, anchor="lm")
            else:
                d.text((left - 5 * u, yy), text, font=f_tick, fill=theme.muted, anchor="rm")
    timeline_axis(g, left, right, top, bottom)
    gap = (1 if o.get("cellGap") is None else float(o["cellGap"])) * u
    heatmap_paint(g, grid, colors, (left, top, right, bottom), (plo, phi), reverse, gap)
    if legend_h:
        width = min(right - left, 200 * u)
        heatmap_legend(g, palette, lo, hi, left, bottom + f_tick.size * 1.5 + 3 * u, width)


# --- transformations -------------------------------------------------------------------
#
# Grafana runs a panel's transformations in the browser, on the frames the queries
# returned: merge, organize, reduce, join and so on. These do the same, on Frame/Field
# objects, each taking (frames, options) and giving frames back.

def tf_reducer_names():
    """Grafana's reducer names, as they head a reduced column."""
    return dict(CALC_NAMES, stdDev="Std Dev", diffperc="Difference percent",
                distinctCount="Distinct count", changeCount="Change count", step="Step",
                lastNotNull="Last *", firstNotNull="First *", sum="Total")


def tf_name(field, frame, frames):
    """A field's display name, as the transformations (and the options they match
    against) see it."""
    return field.config.get("displayName") or field_name(field, frame, frames)


def tf_copy(field, **kw):
    """A field to change without touching the one the data came in."""
    return Field(kw.get("name", field.name), kw.get("type", field.type),
                 kw.get("values", field.values), kw.get("labels", dict(field.labels)),
                 kw.get("config", dict(field.config)))


def tf_rename(field, name):
    """Give a copied field a new display name that later transformations, overrides and
    the table's header all agree on."""
    field.name = name
    field.config.pop("displayName", None)
    field.config["displayNameFromDS"] = name


def tf_frame(frame, fields, name=None, ref_id=None):
    return Frame(fields, frame.name if name is None else name,
                 frame.ref_id if ref_id is None else ref_id, frame.meta)


def tf_rows(frame, idxs):
    """frame, with only the rows at idxs, in that order."""
    out = []
    for f in frame.fields:
        n = len(f.values)
        out.append(tf_copy(f, values=[f.values[i] if i < n else None for i in idxs]))
    return tf_frame(frame, out)


def tf_frame_name(frame, frames):
    """What Grafana calls a frame: its name, else its one value field, else its refId."""
    if frame.name:
        return frame.name
    vals = [f for f in frame.fields if f.type != "time"]
    if len(vals) == 1:
        return tf_name(vals[0], frame, frames)
    return frame.ref_id or ""


def tf_regex(text):
    """A transformation's regex option as Python's: /.../flags, or a whole-name match."""
    return js_regex(str(text))


def tf_expand(match, pattern):
    """JavaScript's replacement patterns: $1, ${1}, $&, $$."""
    def sub(m):
        key = m.group(1)
        if key == "$":
            return "$"
        if key == "&":
            return match.group(0)
        key = key.strip("{}")
        try:
            return match.group(int(key)) or ""
        except (ValueError, IndexError):
            return m.group(0)
    return re.sub(r"\$(\$|&|\d+|\{\d+\})", sub, pattern)


def tf_matcher(cfg):
    """A field matcher config ({id, options}) -> fn(field, frame, frames) -> bool.
    No config matches everything."""
    if not isinstance(cfg, dict) or not cfg.get("id"):
        return lambda f, fr, frames: True
    return lambda f, fr, frames: matches(cfg, tf_name(f, fr, frames), f, fr)


def tf_guess_type(values):
    """A field's type from its first value, for reduced columns that don't know."""
    for v in values:
        if isinstance(v, bool):
            return "boolean"
        if isinstance(v, (int, float)):
            if isinstance(v, float) and v != v:
                continue
            return "number"
        if isinstance(v, str):
            return "string"
        if v is not None:
            return "other"
    return "number"


def tf_reduce(field, calc):
    """One value from a field by a reducer; numbers by Grafana's reducers, anything else
    by the few that make sense (count, first, last)."""
    vals = field.values
    if field.type == "number":
        return reduce_values(vals, calc)
    have = [v for v in vals if v is not None]
    if calc == "count":
        return float(len(vals))
    if calc in ("first", "firstNotNull", "last", "lastNotNull", "current"):
        pool = vals if calc in ("first", "last") else have
        if not pool:
            return None
        return pool[0] if calc in ("first", "firstNotNull") else pool[-1]
    if calc == "distinctCount":
        return float(len({str(v) for v in have}))
    return None


def tf_calc_list(options):
    return [c for c in (options or []) if isinstance(c, str)]


# merge: many frames into one table, rows that agree on their shared columns joined up.

def tf_mergeable(existing, value):
    """Whether two partial rows can be one: every column they both have a value in
    agrees."""
    for k, v in value.items():
        if k not in existing or existing[k] is None:
            continue
        if existing[k] != v:
            return False
    return True


def tf_merge(frames, opts):
    """Merge series/tables: one frame whose columns are the union of every frame's, with
    rows that match on the columns all frames share joined into one."""
    if len(frames) <= 1:
        return frames
    data = [f for f in frames if f.fields]
    if not data:
        return frames[:1]
    names, where, protos = [], {}, {}
    for fi, fr in enumerate(data):
        for xi, f in enumerate(fr.fields):
            if f.name not in where:
                names.append(f.name)
                where[f.name] = {}
                protos[f.name] = f
            where[f.name][fi] = xi
    keys = [f.name for f in data[-1].fields if len(where[f.name]) == len(data)]
    if not keys:
        return frames
    by_key, order = {}, []
    for fi, fr in enumerate(data):
        key_cols = [fr.fields[where[k][fi]] for k in keys]
        cols = [(n, fr.fields[where[n][fi]]) for n in names if fi in where[n]]
        for i in range(fr.length()):
            key = tuple(repr(f.values[i]) if i < len(f.values) else "" for f in key_cols)
            row = {n: (f.values[i] if i < len(f.values) else None) for n, f in cols}
            slot = by_key.get(key)
            if slot is None:
                by_key[key] = [row]
                order.append((key, 0))
                continue
            for j, existing in enumerate(slot):
                if tf_mergeable(existing, row):
                    slot[j] = {**existing, **row}
                    break
            else:
                slot.append(row)
                order.append((key, len(slot) - 1))
    rows = [by_key[k][j] for k, j in order]
    fields = []
    for n in names:
        p = protos[n]
        conf = {k: v for k, v in p.config.items() if k not in ("displayName", "displayNameFromDS")}
        fields.append(Field(n, p.type, [r.get(n) for r in rows], {}, conf))
    return [Frame(fields, "", "merge-" + "-".join(f.ref_id for f in data if f.ref_id))]


# organize: hide, reorder and rename columns.

def tf_organize(frames, opts):
    """Organize fields: drop the ones excludeByName says, order the rest by indexByName,
    then rename by renameByName. All by display name."""
    exclude = opts.get("excludeByName") or {}
    index = opts.get("indexByName") or {}
    rename = opts.get("renameByName") or {}
    out = []
    for fr in frames:
        names = [tf_name(f, fr, frames) for f in fr.fields]
        keep = [i for i, n in enumerate(names) if not exclude.get(n)]
        keep.sort(key=lambda i: (0, index[names[i]], i) if isinstance(index.get(names[i]), (int, float))
                  else (1, 0, i))
        fields = []
        for i in keep:
            f = tf_copy(fr.fields[i])
            new = rename.get(names[i])
            if isinstance(new, str) and new:
                tf_rename(f, new)
            elif names[i] != f.name or f.labels:
                tf_rename(f, names[i])
            fields.append(f)
        if fields:
            out.append(tf_frame(fr, fields))
    return out


# reduce: a row per series, or a row per frame.

def tf_reduce_rows(frames, matcher, calcs, labels_to_fields):
    """Reduce, series to rows: for every frame's matching fields, a row of the field's
    name and each reducer's result; the rows of all frames end up in one table."""
    names = tf_reducer_names()
    keys = []
    if labels_to_fields:
        for fr in frames:
            for f in fr.fields:
                keys += [k for k in f.labels if k not in keys]
    cols = [[] for _ in range(1 + len(keys) + len(calcs))]
    for fr in frames:
        for f in fr.fields:
            if not matcher(f, fr, frames):
                continue
            cols[0].append(f.name if labels_to_fields else tf_name(f, fr, frames))
            for j, k in enumerate(keys):
                cols[1 + j].append(f.labels.get(k))
            for j, c in enumerate(calcs):
                v = tf_reduce(f, c)
                cols[1 + len(keys) + j].append(math.nan if v is None else v)
    if not cols[0]:
        return []
    fields = [Field("Field", "string", cols[0])]
    fields += [Field(k, "string", cols[1 + j]) for j, k in enumerate(keys)]
    for j, c in enumerate(calcs):
        vals = cols[1 + len(keys) + j]
        fields.append(Field(names.get(c, c), tf_guess_type(vals), vals))
    return [Frame(fields, "", "reduce-" + "-".join(f.ref_id for f in frames if f.ref_id))]


def tf_reduce_fields(frames, matcher, calcs):
    """Reduce, reduce fields: each frame keeps its columns but shrinks to one row."""
    names = tf_reducer_names()
    out = []
    for fr in frames:
        fields = []
        for f in fr.fields:
            if not matcher(f, fr, frames):
                continue
            for c in calcs:
                v = tf_reduce(f, c)
                same = c in ("first", "firstNotNull", "last", "lastNotNull")
                copy = tf_copy(f, values=[v], type=f.type if same else "number")
                if len(calcs) > 1:
                    copy.labels["reducer"] = names.get(c, c)
                fields.append(copy)
        if fields:
            out.append(tf_frame(fr, fields))
    return out


def tf_reduce_step(frames, opts):
    """Reduce: every series (or column) boiled down to the reducers the options list."""
    calcs = tf_calc_list(opts.get("reducers"))
    if not calcs:
        return frames
    by_fields = opts.get("mode") == "reduceFields"
    if opts.get("fields"):
        matcher = tf_matcher(opts["fields"])
    elif opts.get("includeTimeField") and by_fields:
        matcher = tf_matcher(None)
    else:
        matcher = lambda f, fr, frames: f.type != "time"  # noqa: E731
    if by_fields:
        return tf_reduce_fields(frames, matcher, calcs)
    return tf_reduce_rows(frames, matcher, calcs, bool(opts.get("labelsToFields")))


# labelsToFields: series' labels become columns, or rows.

def tf_labels_to_rows(frames, keep):
    out = []
    for fr in frames:
        for f in fr.fields:
            if not f.labels:
                continue
            pairs = [(k, f.labels.get(k)) for k in keep] if keep else list(f.labels.items())
            if pairs:
                out.append(tf_frame(fr, [Field("label", "string", [k for k, _ in pairs]),
                                         Field("value", "string", [v for _, v in pairs])],
                                    name=tf_name(f, fr, frames)))
    return out


def tf_labels_to_fields(frames, opts):
    """Labels to fields: each label becomes a column of its value (or, in rows mode, a
    label/value table per series)."""
    keep = [k for k in opts.get("keepLabels") or [] if isinstance(k, str)]
    if opts.get("mode") == "rows":
        return tf_labels_to_rows(frames, keep)
    out = []
    for fr in frames:
        fields, unique = [], {}
        for f in fr.fields:
            if not f.labels:
                fields.append(f)
                continue
            conf = {k: v for k, v in f.config.items() if k not in ("displayName", "displayNameFromDS")}
            bare = tf_copy(f, labels={}, config=conf)
            fields.append(bare)
            for k, v in f.labels.items():
                if keep and k not in keep:
                    continue
                if opts.get("valueLabel") == k:
                    bare.name = v
                    continue
                unique.setdefault(k, {}).setdefault(v, None)
        n = fr.length()
        for k, vals in unique.items():
            for v in vals:
                fields.append(Field(k, "string", [v] * n))
        out.append(tf_frame(fr, fields))
    return out


# seriesToRows: the series stacked, newest first, with a Metric column.

def tf_series_to_rows(frames, opts):
    """Series to rows: every series' (time, value) points stacked into one Time / Metric /
    Value table, newest first."""
    frames = [f for f in frames if f.length() > 0]
    if not frames or not all(f.time_field() and len(f.fields) > 1 for f in frames):
        return frames
    time0 = value0 = None
    rows = []
    for fr in frames:
        tf = fr.time_field()
        vf = next(f for f in fr.fields if f is not tf)
        time0, value0 = time0 or tf, value0 or vf
        metric = tf_frame_name(fr, frames)
        for i in range(len(tf.values)):
            rows.append((tf.values[i], metric, vf.values[i] if i < len(vf.values) else None))
    rows.sort(key=lambda r: (r[0] is None, -(r[0] or 0)))

    def bare(f):
        return {k: v for k, v in f.config.items() if k not in ("displayName", "displayNameFromDS")}
    fields = [Field("Time", time0.type, [r[0] for r in rows], {}, bare(time0)),
              Field("Metric", "string", [r[1] for r in rows]),
              Field("Value", value0.type, [r[2] for r in rows], {}, bare(value0))]
    return [Frame(fields, "", "seriesToRows-" + "-".join(f.ref_id for f in frames if f.ref_id))]


# joinByField: frames side by side, matched on a time or other field.

def tf_key_field(frame, frames, by):
    """The field a frame is joined on: the one named by, else its first time field."""
    if by:
        return next((f for f in frame.fields if f.name == by or tf_name(f, frame, frames) == by), None)
    return frame.time_field()


def tf_join_rows(maps, keys, tabular):
    """[(key, [row index or None per frame])]: one per key, or when frames repeat a key,
    one per combination (a tabular join) or per position."""
    out = []
    for k in keys:
        lists = [m.get(k) or [None] for m in maps]
        if tabular:
            for combo in itertools.product(*lists):
                out.append((k, list(combo)))
        else:
            for j in range(max(len(x) for x in lists)):
                out.append((k, [x[j] if j < len(x) else None for x in lists]))
    return out


def tf_join(frames, opts):
    """Join by field: an outer (or inner) join of the frames on a field, time by
    default, so each frame's other columns line up on the same rows."""
    by, mode = opts.get("byField"), opts.get("mode") or "outer"
    parts = [(fr, kf) for fr in frames for kf in [tf_key_field(fr, frames, by)] if kf is not None]
    if len(parts) < 2:
        return frames
    maps = []
    for fr, kf in parts:
        m = {}
        for i, k in enumerate(kf.values):
            if k is not None and not (isinstance(k, float) and k != k):
                m.setdefault(k, []).append(i)
        maps.append(m)
    keys = list(dict.fromkeys(k for m in maps for k in m))
    if mode == "inner":
        keys = [k for k in keys if all(k in m for m in maps)]
    if parts[0][1].type in ("time", "number"):
        try:
            keys.sort()
        except TypeError:
            pass
    rows = tf_join_rows(maps, keys, mode == "outerTabular")
    first = parts[0][1]
    fields = [tf_copy(first, values=[k for k, _ in rows], labels={})]
    names = [[tf_name(f, fr, frames) for f in fr.fields if f is not kf] for fr, kf in parts]
    seen = {}
    for ns in names:
        for n in ns:
            seen[n] = seen.get(n, 0) + 1
    for p, (fr, kf) in enumerate(parts):
        others = [f for f in fr.fields if f is not kf]
        for f, n in zip(others, names[p]):
            vals = [f.values[r[p]] if r[p] is not None and r[p] < len(f.values) else None for _, r in rows]
            col = tf_copy(f, values=vals)
            if seen[n] > 1 and fr.ref_id:
                n = f"{n} #{fr.ref_id}"
            tf_rename(col, n)
            fields.append(col)
    return [Frame(fields, "", "join-" + "-".join(fr.ref_id for fr, _ in parts if fr.ref_id))]


# filtering and renaming fields and frames

def tf_names_match(spec, name, field):
    """Whether a filterFieldsByName half ({names, pattern}) matches a field."""
    if not isinstance(spec, dict):
        return False
    if name in (spec.get("names") or []) or field.name in (spec.get("names") or []):
        return True
    pattern = spec.get("pattern")
    if pattern:
        try:
            return bool(tf_regex(pattern)[0].search(name))
        except (ValueError, re.error):
            return False
    return False


def tf_filter_fields(frames, opts):
    """Filter fields by name: keep the ones include matches (all, if it names none),
    minus the ones exclude matches."""
    inc, exc = opts.get("include") or {}, opts.get("exclude") or {}
    has_inc = bool(inc.get("names") or inc.get("pattern"))
    out = []
    for fr in frames:
        fields = []
        for f in fr.fields:
            n = tf_name(f, fr, frames)
            if has_inc and not tf_names_match(inc, n, f):
                continue
            if tf_names_match(exc, n, f):
                continue
            fields.append(f)
        if fields:
            out.append(tf_frame(fr, fields))
    return out


def tf_ref_match(pattern, ref):
    """Grafana's refId matcher: /regex/, a|b|c, or one refId."""
    pattern = str(pattern or "")
    if pattern.startswith("/"):
        try:
            return bool(tf_regex(pattern)[0].search(ref))
        except (ValueError, re.error):
            return False
    return ref in pattern.split("|") if "|" in pattern else ref == pattern


def tf_filter_ref(frames, opts):
    """Filter data by query refId: only the frames from queries include names (and none
    from exclude)."""
    inc, exc = opts.get("include"), opts.get("exclude")
    return [fr for fr in frames
            if (not inc or tf_ref_match(inc, fr.ref_id)) and not (exc and tf_ref_match(exc, fr.ref_id))]


def tf_rename_regex(frames, opts):
    """Rename fields by regex: a name the regex matches becomes the renamePattern, with
    $1 and friends filled in."""
    try:
        rx, every = tf_regex(opts.get("regex") or "(.*)")
    except (ValueError, re.error):
        return frames
    pattern = opts.get("renamePattern", "$1")
    out = []
    for fr in frames:
        fields = []
        for f in fr.fields:
            name = tf_name(f, fr, frames)
            if rx.search(name):
                f = tf_copy(f)
                tf_rename(f, rx.sub(lambda m: tf_expand(m, pattern), name, count=0 if every else 1))
            fields.append(f)
        out.append(tf_frame(fr, fields))
    return out


# sorting and trimming rows

def tf_sorted(order, values, desc):
    """order, stably sorted by values[i]: numbers, then text, then blanks last."""
    def key(i):
        v = values[i] if i < len(values) else None
        if isinstance(v, bool):
            return (1, 0, str(v))
        if isinstance(v, (int, float)):
            return (0, v, "")
        return (1, 0, str(v))

    def blank(i):
        v = values[i] if i < len(values) else None
        return v is None or (isinstance(v, float) and v != v)
    full = sorted((i for i in order if not blank(i)), key=key, reverse=bool(desc))
    return full + [i for i in order if blank(i)]


def tf_find(frame, frames, name):
    return next((f for f in frame.fields if f.name == name or tf_name(f, frame, frames) == name), None)


def tf_sort_by(frames, opts):
    """Sort by: each frame's rows ordered by the fields listed, first one first."""
    keys = [s for s in opts.get("sort") or [] if isinstance(s, dict) and s.get("field")]
    out = []
    for fr in frames:
        order = list(range(fr.length()))
        for s in reversed(keys):
            f = tf_find(fr, frames, s["field"])
            if f is not None:
                order = tf_sorted(order, f.values, s.get("desc"))
        out.append(tf_rows(fr, order))
    return out


def tf_limit(frames, opts):
    """Limit: only the first n rows of each frame (10 unless limitField says)."""
    try:
        n = int(float(opts.get("limitField", 10)))
    except (TypeError, ValueError):
        n = 10
    if n < 0:
        return frames
    return [tf_rows(fr, list(range(min(n, fr.length())))) if fr.length() > n else fr for fr in frames]


# calculateField: a new column from the others.

def tf_operand(side, frame, frames):
    """One side of a binary calculation -> ("field", Field) or ("number", float) or None."""
    if isinstance(side, dict):
        if side.get("matcher"):
            side = (side["matcher"] or {}).get("options")
        elif "fixed" in side:
            side = side["fixed"]
        else:
            return None
    f = tf_find(frame, frames, str(side)) if isinstance(side, str) else None
    if f is not None:
        return "field", f
    try:
        return "number", float(side)
    except (TypeError, ValueError):
        return None


def tf_binary(op, a, b):
    """a op b for two numbers, None where it isn't a number (a divide by zero, say)."""
    if not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (a, b)):
        return None
    try:
        r = {"+": a + b, "-": a - b, "*": a * b, "/": a / b, "%": a % b, "^": a ** b}[op]
    except (KeyError, ZeroDivisionError, OverflowError, ValueError):
        return None
    return r if isinstance(r, (int, float)) and math.isfinite(r) else None


def tf_new_column(frame, frames, opts, n):
    """-> (name, values) of the column calculateField adds to this frame, or None."""
    mode = opts.get("mode") or "reduceRow"
    if mode == "index":
        pct = (opts.get("index") or {}).get("asPercentile")
        return opts.get("alias") or "Row", [(i / (n - 1) if n > 1 else 0.0) if pct else float(i)
                                              for i in range(n)]
    if mode == "binary":
        b = opts.get("binary") or {}
        left, right = tf_operand(b.get("left"), frame, frames), tf_operand(b.get("right"), frame, frames)
        if not left or not right:
            return None
        op = b.get("operator") or "+"

        def at(side, i):
            kind, x = side
            if kind == "number":
                return x
            return x.values[i] if i < len(x.values) else None
        lt, rt = (left[1].name if left[0] == "field" else left[1]), (right[1].name if right[0] == "field" else right[1])
        lt = int(lt) if isinstance(lt, float) and lt.is_integer() else lt
        rt = int(rt) if isinstance(rt, float) and rt.is_integer() else rt
        return opts.get("alias") or f"{lt} {op} {rt}", [tf_binary(op, at(left, i), at(right, i)) for i in range(n)]
    if mode == "reduceRow":
        red = opts.get("reduce") or {}
        calc = red.get("reducer") or "sum"
        include = red.get("include") or []
        cols = [f for f in frame.fields if f.type == "number"
                and (not include or tf_name(f, frame, frames) in include or f.name in include)]
        vals = []
        for i in range(n):
            row = [c.values[i] if i < len(c.values) else None for c in cols]
            vals.append(reduce_values(row, calc))
        return opts.get("alias") or tf_reducer_names().get(calc, calc), vals
    return None


def tf_calculate(frames, opts):
    """Add field from calculation: a column of each row's reduction (reduceRow), of
    left op right (binary) or of the row number (index)."""
    out = []
    for fr in frames:
        made = tf_new_column(fr, frames, opts, fr.length())
        if made is None:
            raise ValueError("unsupported calculation")
        name, vals = made
        col = Field(name, "number", vals)
        if opts.get("replaceFields"):
            keep = [f for f in fr.fields if f.type == "time"][:1]
            out.append(tf_frame(fr, keep + [col]))
        else:
            out.append(tf_frame(fr, list(fr.fields) + [col]))
    return out


# convertFieldType

def tf_to_number(v, kind):
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return v * 1000 if kind == "time" else v
    try:
        return float(str(v).strip().replace(",", ""))
    except ValueError:
        return None


def tf_to_time(v):
    """Seconds since the epoch from a number (milliseconds) or an ISO-ish date."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v / 1000
    text = str(v).strip().replace("Z", "+00:00")
    for parse in (lambda s: datetime.datetime.fromisoformat(s),
                  lambda s: datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")):
        try:
            d = parse(text)
            return (d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)).timestamp()
        except ValueError:
            pass
    try:
        return float(text) / 1000
    except ValueError:
        return None


def tf_to_string(v, kind):
    if v is None:
        return None
    if kind == "time" and isinstance(v, (int, float)):
        try:
            return datetime.datetime.fromtimestamp(v).strftime("%Y-%m-%d %H:%M:%S")
        except (OverflowError, OSError, ValueError):
            return str(v)
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def tf_convert_value(v, kind, dest):
    if v is None:
        return None
    if dest == "number":
        return tf_to_number(v, kind)
    if dest == "string":
        return tf_to_string(v, kind)
    if dest == "time":
        return tf_to_time(v)
    if dest == "boolean":
        return v if isinstance(v, bool) else (str(v).strip().lower() not in ("", "0", "false", "no", "null")
                                              if not isinstance(v, (int, float)) else v != 0)
    return v


def tf_convert(frames, opts):
    """Convert field type: the named fields to number, string, time or boolean."""
    todo = [c for c in opts.get("conversions") or [] if isinstance(c, dict) and c.get("targetField")]
    out = []
    for fr in frames:
        fields = []
        for f in fr.fields:
            name = tf_name(f, fr, frames)
            c = next((c for c in todo if c["targetField"] in (name, f.name)), None)
            dest = (c or {}).get("destinationType")
            if dest in ("number", "string", "time", "boolean"):
                f = tf_copy(f, type=dest, values=[tf_convert_value(v, f.type, dest) for v in f.values])
            fields.append(f)
        out.append(tf_frame(fr, fields))
    return out


# groupBy

def tf_group_by(frames, opts):
    """Group by: rows with the same values in the group-by fields become one row, the
    aggregated fields reduced across them ("cpu (mean)")."""
    spec = opts.get("fields") or {}
    out = []
    for fr in frames:
        names = [tf_name(f, fr, frames) for f in fr.fields]
        ops = [(spec.get(n) or {}) for n in names]
        by = [i for i, o in enumerate(ops) if o.get("operation") == "groupby"]
        agg = [i for i, o in enumerate(ops) if o.get("operation") == "aggregate" and o.get("aggregations")]
        if not by and not agg:
            out.append(fr)
            continue
        groups = {}
        for r in range(fr.length()):
            key = tuple(repr(fr.fields[i].values[r]) if r < len(fr.fields[i].values) else "" for i in by)
            groups.setdefault(key, []).append(r)
        firsts = [rows[0] for rows in groups.values()]
        fields = []
        for i, f in enumerate(fr.fields):
            if i in by:
                fields.append(tf_copy(f, values=[f.values[r] if r < len(f.values) else None for r in firsts]))
            elif i in agg:
                for calc in tf_calc_list(ops[i]["aggregations"]):
                    vals = []
                    for rows in groups.values():
                        sub = tf_copy(f, values=[f.values[r] if r < len(f.values) else None for r in rows])
                        vals.append(tf_reduce(sub, calc))
                    same = calc in ("first", "firstNotNull", "last", "lastNotNull")
                    col = tf_copy(f, values=vals, type=f.type if same else "number")
                    tf_rename(col, f"{names[i]} ({calc})")
                    fields.append(col)
        out.append(tf_frame(fr, fields))
    return out


# filterByValue

def tf_number(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def tf_value_test(cfg, v):
    """Whether one value passes one filterByValue matcher ({id, options})."""
    kind, o = (cfg or {}).get("id"), (cfg or {}).get("options") or {}
    if kind == "isNull":
        return v is None
    if kind == "isNotNull":
        return v is not None
    if kind == "regex":
        try:
            return bool(re.search(str(o.get("value", "")), "" if v is None else str(v)))
        except re.error:
            return False
    if kind == "range":
        lo, hi, x = tf_number(o.get("from")), tf_number(o.get("to")), tf_number(v)
        return x is not None and lo is not None and hi is not None and lo <= x <= hi
    want, x = o.get("value"), tf_number(v)
    if kind in ("equal", "notEqual"):
        w = tf_number(want)
        same = (x == w) if (x is not None and w is not None) else str(v) == str(want)
        return same if kind == "equal" else not same
    w = tf_number(want)
    if x is None or w is None:
        return False
    return {"greater": x > w, "greaterOrEqual": x >= w, "lower": x < w, "lowerOrEqual": x <= w}.get(kind, False)


def tf_filter_values(frames, opts):
    """Filter data by values: keep (or drop) the rows that match any (or all) of the
    filters."""
    include = (opts.get("type") or "include") == "include"
    every = opts.get("match") == "all"
    out = []
    for fr in frames:
        tests = []
        for flt in opts.get("filters") or []:
            f = tf_find(fr, frames, str((flt or {}).get("fieldName")))
            if f is not None and (flt or {}).get("config"):
                tests.append((f, flt["config"]))
        if not tests:
            out.append(fr)
            continue
        keep = []
        for r in range(fr.length()):
            hits = [tf_value_test(c, f.values[r] if r < len(f.values) else None) for f, c in tests]
            hit = all(hits) if every else any(hits)
            if hit == include:
                keep.append(r)
        out.append(tf_rows(fr, keep))
    return out


def tf_steps():
    """Transformation id -> what does it."""
    return {"merge": tf_merge, "organize": tf_organize, "reduce": tf_reduce_step,
            "labelsToFields": tf_labels_to_fields, "seriesToRows": tf_series_to_rows,
            "joinByField": tf_join, "seriesToColumns": tf_join,
            "filterFieldsByName": tf_filter_fields, "filterByRefId": tf_filter_ref,
            "renameByRegex": tf_rename_regex, "sortBy": tf_sort_by, "limit": tf_limit,
            "calculateField": tf_calculate, "convertFieldType": tf_convert,
            "groupBy": tf_group_by, "filterByValue": tf_filter_values}


def tf_run(step, frames, opts, only):
    """One transformation; with a filter on it, only the frames the filter picks go
    through, and the rest follow after."""
    if isinstance(only, dict) and only.get("id") == "byRefId":
        hit = [f for f in frames if tf_ref_match(only.get("options"), f.ref_id)]
        rest = [f for f in frames if f not in hit]
        return step(hit, opts) + rest
    return step(frames, opts)


def apply_transformations(frames, transformations, panel):
    """A panel's transformations (merge, organize, reduce, ...), which Grafana runs in
    the browser after the queries -> (frames, [ids of ones it skipped])."""
    notes = []
    for t in transformations or []:
        tid = t.get("id") if isinstance(t, dict) else None
        step = tf_steps().get(tid)
        if step is None:
            notes.append(tid)
            continue
        try:
            frames = tf_run(step, frames, t.get("options") or {}, t.get("filter"))
        except Exception:  # a transformation that can't cope leaves the data as it was
            traceback.print_exc(file=LOG)
            notes.append(tid)
    return frames, notes


# --- the table ----------------------------------------------------------------------------

def table_status_words():
    """Words that color a cell in a status-like column, worst first: (bad, warn, good)."""
    return ({"error", "failed", "failure", "crashloopbackoff", "oomkilled", "evicted",
             "imagepullbackoff", "errimagepull", "backoff", "unhealthy", "notready", "down",
             "dead", "lost", "critical", "firing", "alerting"},
            {"pending", "containercreating", "podinitializing", "init", "terminating", "waiting",
             "starting", "restarting", "unknown", "degraded", "paused", "exited", "progressing",
             "suspended", "warning"},
            {"running", "ready", "up", "healthy", "ok", "succeeded", "completed", "complete",
             "active", "bound", "available", "passing", "true", "normal", "resolved"})


def table_status_color(theme, text):
    """The color for a cell in a status-like column, by its worst word: red, yellow or
    green. None when it says nothing."""
    bad, warn, good = table_status_words()
    words = set(re.findall(r"[a-z]+", str(text).lower()))
    if words & bad:
        return mix(theme.critical, theme.ink, 0.2)
    if words & warn:
        return theme.warning
    return mix(theme.good, theme.ink, 0.35) if words & good else None


def table_cap_widths(widths, avail, floor):
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


def table_cell_options(custom):
    """The cell type of a column: {type, mode, valueDisplayMode}, from cellOptions, or
    from the older displayMode setting."""
    opts = dict(custom.get("cellOptions") or {})
    if not opts.get("type"):
        old = {"color-text": ("color-text", None), "color-background": ("color-background", "gradient"),
               "color-background-solid": ("color-background", "basic"),
               "basic": ("gauge", "basic"), "gradient-gauge": ("gauge", "gradient"),
               "lcd-gauge": ("gauge", "lcd"), "json-view": ("json-view", None), "image": ("image", None)
               }.get(custom.get("displayMode"), ("auto", None))
        opts["type"] = old[0]
        if old[1]:
            opts.setdefault("mode", old[1])
    return opts


def table_columns(g, frame):
    """The columns to show: each visible field with its settings, name and cell type."""
    cols = []
    for f in frame.fields:
        conf, name = field_config(g.panel, f, frame, g.frames)
        custom = conf.get("custom") or {}
        if custom.get("hidden") or (custom.get("hideFrom") or {}).get("viz"):
            continue
        nums = numbers(f.values) if f.type == "number" else []
        align = custom.get("align") or "auto"
        cols.append({"field": f, "conf": conf, "name": name, "cell": table_cell_options(custom),
                     "right": align == "right" or (align == "auto" and f.type == "number"),
                     "center": align == "center", "width": custom.get("width"),
                     "lo": conf["min"] if isinstance(conf.get("min"), (int, float)) else (min(nums) if nums else 0),
                     "hi": conf["max"] if isinstance(conf.get("max"), (int, float)) else (max(nums) if nums else 1),
                     "status": (f.type == "string" and not conf.get("mappings") and conf.get("thresholds") is None
                               and bool(re.search(r"status|state|phase|health|ready|reason|condition",
                                                  name, re.I)))})
    return cols


def table_time_text(g, col, v):
    """A time cell: the unit's date format if the panel set one, else YYYY-MM-DD HH:MM:SS."""
    if col["conf"].get("unit"):
        text, suffix = format_value(v * 1000, col["conf"]["unit"], col["conf"].get("decimals"))
        return text + suffix
    try:
        return datetime.datetime.fromtimestamp(v).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return str(v)


def table_cell(g, col, v, i):
    """(text, color or None) for one cell: the value as the field's unit, decimals and
    mappings say, and the color it wants (a mapping's, or its thresholds' for a number)."""
    conf, f = col["conf"], col["field"]
    mapped = map_value(conf, v)
    has_color = bool(mapped and mapped.get("color"))
    if isinstance(v, bool) and not mapped:
        return ("true" if v else "false"), None
    if v is None and not mapped:
        return ("" if conf.get("noValue") is None else str(conf["noValue"])), None
    if isinstance(v, (list, dict)):
        return json.dumps(v, separators=(",", ":"), default=str), None
    if isinstance(v, float) and v != v and not mapped:
        return "NaN", None
    if f.type == "time" and isinstance(v, (int, float)) and not mapped:
        return table_time_text(g, col, v), None
    if f.type != "number" and not isinstance(v, (int, float)) and not mapped:
        return str(v), None
    text, suffix, color = g.display(v, conf, i)
    use = has_color or (isinstance(v, (int, float)) and not isinstance(v, bool))
    return text + suffix, (color if use else None)


def table_ink_on(g, bg):
    """Text color that reads on a background: the lighter or darker of the theme's."""
    def lum(c):
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    a, b = g.theme.ink, g.theme.surface
    return a if abs(lum(a) - lum(bg)) >= abs(lum(b) - lum(bg)) else b


def table_gradient(g, box, color, a0, a1):
    """A rectangle of color fading from alpha a0 at the left to a1 at the right."""
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    w, h = x1 - x0, y1 - y0
    if w < 1 or h < 1:
        return
    layer = np.empty((1, w, 4), np.uint8)
    layer[0, :, :3] = color
    layer[0, :, 3] = np.linspace(a0, a1, w)
    layer = Image.fromarray(layer, "RGBA").resize((w, h), Image.Resampling.NEAREST)
    g.img.paste(layer.convert("RGB"), (x0, y0), layer.getchannel("A"))


def table_gauge(g, col, v, band, x0, w):
    """A bar cell: a track, and the value's share of min..max filled in (basic: flat,
    gradient: fading in, lcd: in little blocks)."""
    d, u = g.d, g.u
    conf, lo, hi = col["conf"], col["lo"], col["hi"]
    if not isinstance(v, (int, float)) or isinstance(v, bool) or v != v:
        return
    frac = 1.0 if hi == lo else min(1.0, max(0.0, (v - lo) / (hi - lo)))
    color = value_color(conf, v, g.theme, 0, lo, hi)
    y0, y1 = band
    d.rectangle((x0, y0, x0 + w, y1), fill=g.theme.ink + (16,))
    mode = col["cell"].get("mode") or "gradient"
    if mode == "lcd":
        n = max(2, int(w / (5 * u)))
        for k in range(n):
            sx = x0 + w * k / n
            lit = (k + 0.5) / n <= frac
            c = value_color(conf, lo + (hi - lo) * (k + 0.5) / n, g.theme, 0, lo, hi) if lit else g.theme.ink
            d.rectangle((sx, y0, sx + w / n - 1.5 * u, y1), fill=c + ((200 if lit else 24),))
    elif mode == "basic":
        d.rectangle((x0, y0, x0 + w * frac, y1), fill=color + (170,))
    else:
        table_gradient(g, (x0, y0, x0 + w * frac, y1), color, 40, 190)


def table_draw_cell(g, col, text, v, color, x, w, y, rh, fnt, first, right_edge):
    """One cell: its background or bar if its type has one, then its text, aligned."""
    d, u = g.d, g.u
    kind, mode = col["cell"].get("type") or "auto", col["cell"].get("mode")
    band = (y + 1.5 * u, y + rh - 1.5 * u)
    gap = right_edge
    ink = g.theme.ink if first else g.theme.ink_2
    if kind == "gauge":
        table_gauge(g, col, v, band, x, w)
        if col["cell"].get("valueDisplayMode") == "hidden":
            text = ""
    elif kind == "color-background" and color:
        box = (max(g.box[0], x - gap / 2), band[0], min(g.box[2], x + w + gap / 2), band[1])
        if mode == "basic":
            d.rectangle(box, fill=color)
        else:
            table_gradient(g, box, color, 120, 255)
        ink = table_ink_on(g, color)
    elif kind == "color-text" and color:
        ink = color
    elif color and (map_value(col["conf"], v) or {}).get("color"):
        ink = color
    elif col["status"]:
        ink = table_status_color(g.theme, text) or ink
    tw = d.textlength(text, font=fnt)
    tx = x + w - tw if col["right"] else x + (w - tw) / 2 if col["center"] else x
    d.text((tx, y + rh / 2), text, font=fnt, fill=ink, anchor="lm")


def table_footer_values(g, cols, frame, order):
    """The footer's cells: each number column (or the ones footer.fields names) reduced
    by the footer's reducer, formatted like its column; the first cell can hold a row
    count."""
    foot = g.options.get("footer") or {}
    calcs = tf_calc_list(foot.get("reducer"))
    wanted = [str(x) for x in foot.get("fields") or []]
    out = []
    for j, col in enumerate(cols):
        f = col["field"]
        text = ""
        if calcs and f.type == "number" and (not wanted or col["name"] in wanted or f.name in wanted):
            v = reduce_values(f.values, calcs[0])
            text = table_cell(g, col, v, 0)[0]
        out.append(text)
    if foot.get("countRows") and out:
        out[0] = f"Count: {frame.length()}"
    return out if any(out) else None


def table_sorted(g, frame):
    """The row order: the panel's sortBy (by column name), else as the data has it."""
    order = list(range(frame.length()))
    sort = [s for s in g.options.get("sortBy") or [] if isinstance(s, dict) and s.get("displayName")]
    for s in reversed(sort):
        f = next((f for f in frame.fields
                  if tf_name(f, frame, g.frames) == s["displayName"] or f.name == s["displayName"]), None)
        if f is not None:
            order = tf_sorted(order, f.values, s.get("desc"))
    return order


def table_texts(g, cols, rows):
    """[[ (text, color, raw value) per column ] per row] for just these rows."""
    out = []
    for r in rows:
        line = []
        for col in cols:
            vals = col["field"].values
            v = vals[r] if r < len(vals) else None
            text, color = table_cell(g, col, v, r)
            line.append((text, color, v))
        out.append(line)
    return out


def table_layout(g, cols, cells, size, avail_w, header_font_scale=0.86):
    """(size, gap, shown column count, column widths): text as big as fits, then
    smaller, then the widest columns narrowed, then columns dropped from the right."""
    d, u = g.d, g.u

    def measure(sz):
        fb, fh = font(g.theme, sz), font(g.theme, sz * header_font_scale, bold=True)
        gap = 1.1 * sz
        out = []
        for j, col in enumerate(cols):
            w = max([d.textlength(col["name"], font=fh)] + [d.textlength(row[j][0], font=fb) for row in cells])
            if col["cell"].get("type") == "gauge":
                w = max(w, 8 * sz)
            if isinstance(col["width"], (int, float)) and col["width"] > 0:
                w = max(4 * u, col["width"] * u - gap)
            out.append(w)
        return out

    widths = measure(size)
    need = sum(widths) + 1.1 * size * (len(widths) - 1)
    if need > avail_w:
        size = max(10 * u, size * avail_w / need)
        widths = measure(size)
    gap = 1.1 * size
    shown, caps = len(cols), []
    while shown:
        caps = table_cap_widths(widths[:shown], avail_w - gap * (shown - 1), 3.5 * size)
        if caps is not None:
            break
        shown -= 1
    return size, gap, shown, caps or []


@drawer("table", "table-old")
def draw_table(g):
    """A table panel: a header, then the rows of one frame (the first, or options.frameIndex)
    with each column's unit, mappings and cell type, clipped to what fits."""
    frames = [f for f in g.frames if f.fields]
    if not frames:
        return g.no_data()
    pick = g.options.get("frameIndex")
    k = pick if isinstance(pick, int) and 0 <= pick < len(frames) else 0
    frame = frames[k]
    cols = table_columns(g, frame)
    nrows = frame.length()
    if not cols or not nrows:
        return g.no_data()
    d, u, theme = g.d, g.u, g.theme
    x0, y0, x1, y1 = g.box
    order = table_sorted(g, frame)
    show_head = g.options.get("showHeader", True) is not False
    foot = table_footer_values(g, cols, frame, order)
    f_small = g.font(10.5)
    line_h = f_small.size * 1.45
    base = 12 * u
    row_k = {"md": 1.2, "lg": 1.5}.get(g.options.get("cellHeight"), 1.0)

    def rows_fit(size, notes):
        rh = size * 1.6 * row_k
        free = y1 - y0 - rh * ((1 if show_head else 0) + (1 if foot else 0)) - notes * line_h
        return max(1, int(free / rh))

    # Only the rows that could be showing count toward the columns' widths.
    most = rows_fit(10 * u, 1)
    cells = table_texts(g, cols, order[:most])
    extra = [[(t, None, None) for t in foot]] if foot else []
    size, gap, shown, caps = table_layout(g, cols, cells + extra, base, x1 - x0)
    note_lines = 1 if (len(frames) > 1 or shown < len(cols) or nrows > rows_fit(size, 0)) else 0
    n = rows_fit(size, note_lines)
    cells = cells[:n]
    rh = size * 1.6 * row_k
    f_body, f_head = font(theme, size), font(theme, size * 0.86, bold=True)
    lw = max(1, round(1.5 * u))
    xs = [x0 + sum(caps[:i]) + gap * i for i in range(shown)]
    right = xs[-1] + caps[-1] if shown else x1
    y = y0
    if show_head:
        for i in range(shown):
            col, name = cols[i], squeeze(d, cols[i]["name"], f_head, caps[i])
            tw = d.textlength(name, font=f_head)
            tx = xs[i] + caps[i] - tw if col["right"] else xs[i] + (caps[i] - tw) / 2 if col["center"] else xs[i]
            d.text((tx, y + rh / 2), name, font=f_head, fill=theme.muted, anchor="lm")
        d.line((x0, y + rh * 0.94, right, y + rh * 0.94), fill=theme.axis, width=lw)
        y += rh
    for r, row in enumerate(cells):
        if r % 2:
            d.rectangle((x0 - 2 * u, y, x1 + 2 * u, y + rh), fill=theme.ink + (7,))
        for i in range(shown):
            text, color, v = row[i]
            col = cols[i]
            text = squeeze(d, text, f_body, caps[i])
            table_draw_cell(g, col, text, v, color, xs[i], caps[i], y, rh, f_body, i == 0, gap)
        y += rh
    if foot:
        d.line((x0, y + rh * 0.04, right, y + rh * 0.04), fill=theme.axis, width=lw)
        fb = font(theme, size, bold=True)
        for i in range(shown):
            text = squeeze(d, foot[i], fb, caps[i])
            tw = d.textlength(text, font=fb)
            d.text((xs[i] + caps[i] - tw if cols[i]["right"] else xs[i], y + rh / 2), text, font=fb,
                   fill=theme.ink, anchor="lm")
        y += rh
    more = []
    if len(frames) > 1:
        more.append(f"frame {k + 1} of {len(frames)}")
    if nrows > len(cells):
        more.append(f"+{nrows - len(cells)} more row{'' if nrows - len(cells) == 1 else 's'}")
    if shown < len(cols):
        more.append(f"+{len(cols) - shown} more column{'' if len(cols) - shown == 1 else 's'}")
    if more:
        d.text((x0, y + line_h * 0.15), "  ·  ".join(more), font=f_small, fill=theme.muted)


# --- shared: laying values out, fitting text --------------------------------------------------
#
# Stat, gauge and bar gauge all show one tile per value, laid out the way Grafana's "viz
# repeater" does it. Layout maths below is in CSS pixels, then drawn at g.u times that.

STAT_UNIT_SCALE = 0.6      # the unit next to a big value, relative to the value


def stat_child_size(pw, ph, n):
    """The side of the biggest square that n of them fit in a pw x ph box with, Grafana's
    way (the first half of its grid maths)."""
    if pw <= 0 or ph <= 0:
        return max(pw, ph, 1)
    parts = max(1, math.ceil(math.sqrt(n * pw / ph)))
    if math.floor(parts * ph / pw) * parts < n:
        return ph / math.ceil(parts * ph / pw)
    return pw / parts


def stat_grid(pw, ph, sp, n):
    """Grafana's auto layout: as square a grid of tiles as will fit.
    -> [(x, y, w, h)] for n tiles."""
    square = max(stat_child_size(pw, ph, n), stat_child_size(ph, pw, n), 1)
    xc = max(1, math.floor(pw / square))
    yc = math.ceil(n / xc)
    xc = math.ceil(n / yc)  # now even the rows out
    last = max(1, xc - (xc * yc - n))
    w, h = pw / xc - sp + sp / xc, ph / yc - sp + sp / yc
    w_last = pw / last - sp + sp / last
    out = []
    for i in range(n):
        yg, xg = divmod(i, xc)
        iw = w_last if yg == yc - 1 else w
        out.append((xg * iw + sp * xg, yg * h + sp * yg, iw, h))
    return out


def stat_repeat(w, h, n, orientation, spacing, auto_grid=True, min_w=0, min_h=0, max_h=None):
    """Where n tiles go in a w x h box: -> ([(x, y, w, h)], the orientation it came to).
    Auto orientation with auto_grid is a grid; otherwise a row or a column of them."""
    orientation = orientation if orientation in ("horizontal", "vertical") else "auto"
    if auto_grid and orientation == "auto":
        return stat_grid(w, h, spacing, n), "auto"
    if orientation == "auto":
        orientation = "vertical" if w > h else "horizontal"
    out = []
    if orientation == "horizontal":  # tiles stacked in a column
        vh = (h + spacing) / n - spacing
        vh = max(vh, min_h or 0)
        if max_h:
            vh = min(vh, max_h)
        out = [(0, i * (vh + spacing), w, vh) for i in range(n)]
    else:  # tiles side by side
        vw = max(w / n - spacing + spacing / n, min_w or 0)
        out = [(i * (vw + spacing), 0, vw, h) for i in range(n)]
    return out, orientation


def stat_width(g, text, size, bold=False):
    """The width of text at size, in CSS pixels."""
    try:
        return g.d.textlength(text, font=g.font(size, bold)) / g.u
    except (OSError, ZeroDivisionError, ValueError):  # PIL balks at some tiny sizes
        return len(text) * size * 0.55


def stat_fit(g, text, w, h, line_height, max_size=None, bold=False, suffix=""):
    """The biggest font size (CSS px) that has text (and its smaller unit) fit in w x h.
    Measured for real at the end, since fonts round."""
    def width(size):
        return stat_width(g, text, size, bold) + (
            stat_width(g, suffix, size * STAT_UNIT_SCALE, bold) if suffix else 0)

    w, h = max(w, 1), max(h, 1)
    size = min(h / line_height, w / (width(14) + 2) * 14)
    if max_size:
        size = min(size, max_size)
    size = max(size, 1)
    for _ in range(4):
        have = width(size)
        if have <= w or have <= 0:
            break
        size *= w / have * 0.99
    return max(size, 1)


def stat_put(g, ox, oy, x, y, text, size, fill, bold=False, anchor="l", lh=1.2):
    """Draw text on a line box whose top is y (CSS px, relative to the origin ox, oy in
    pixels), x being its left, middle or right by anchor. The line is centered on y plus
    half its height, which sits digits where they look right."""
    if not text or size * g.u < 6:
        return 0
    f = g.font(size, bold)
    base = oy + (y + size * lh / 2 + size * 0.36) * g.u
    g.d.text((ox + x * g.u, base), text, font=f, fill=fill, anchor=anchor + "s")
    return stat_width(g, text, size, bold)


def stat_run(g, ox, oy, x, y, text, suffix, size, fill, bold=False, anchor="l", lh=1.2):
    """A value and its (smaller) unit on one baseline. Returns the width used."""
    if size * g.u < 6:  # too small to read, and too small for FreeType to draw
        return 0
    f, fs = g.font(size, bold), g.font(size * STAT_UNIT_SCALE, bold)
    tw = stat_width(g, text, size, bold)
    sw = stat_width(g, suffix, size * STAT_UNIT_SCALE, bold) if suffix else 0
    gap = size * 0.04 if suffix else 0
    total = tw + sw + gap
    x0 = x - total / 2 if anchor == "m" else x - total if anchor == "r" else x
    base = oy + (y + size * lh / 2 + size * 0.36) * g.u
    g.d.text((ox + x0 * g.u, base), text, font=f, fill=fill, anchor="ls")
    if suffix:
        g.d.text((ox + (x0 + tw + gap) * g.u, base), suffix, font=fs, fill=fill, anchor="ls")
    return total


def stat_contrast(g, color):
    """Text color for sitting on color: the theme's light ink or its dark surface."""
    lum = (0.299 * color[0] + 0.587 * color[1] + 0.114 * color[2]) / 255
    return g.theme.surface if lum > 0.62 else g.theme.ink


def stat_items(g):
    """g.reduced(), or, when there's nothing to reduce but the panel says what to show for
    a missing value (a null -> "N/A" mapping, or a noValue), one empty value to show that."""
    items = g.reduced()
    if items:
        return items
    f = Field("", "number", [])
    fr = Frame([f])
    conf, name = field_config(g.panel, f, fr, [fr])
    if map_value(conf, None) is None and conf.get("noValue") is None:
        return []
    return [{"name": name, "value": None, "conf": conf, "field": f, "frame": fr, "index": 0,
             "times": [], "values": []}]


def stat_show(g, it, i):
    """(text, suffix, color) for a reduced value, "No data" for nothing."""
    text, suffix, color = g.display(it["value"], it["conf"], i)
    if text in ("", None) and it["value"] is None:
        text = "No data"
    return text, suffix, color


# --- stat -------------------------------------------------------------------------------------

def stat_gradient(w, h, c1, c2):
    """A w x h image going c1 -> c2 along CSS's 120deg, built small and stretched."""
    sw, sh = max(2, w // 4), max(2, h // 4)
    ys, xs = np.mgrid[0:sh, 0:sw]
    dx, dy = math.sin(math.radians(120)), -math.cos(math.radians(120))
    span = abs(sw * dx) + abs(sh * dy)
    t = (((xs + 0.5) - sw / 2) * dx + ((ys + 0.5) - sh / 2) * dy) / span + 0.5
    t = np.clip(t, 0, 1)[..., None]
    arr = np.array(c1, float) * (1 - t) + np.array(c2, float) * t
    return Image.fromarray(arr.astype("uint8"), "RGB").resize((w, h), Image.Resampling.BILINEAR)


def stat_shift(color, dh, dl):
    """color with its hue turned by dh (0-1) and lightness moved by dl (0-1): the two ends
    of Grafana's background gradient."""
    h, l, s = colorsys.rgb_to_hls(*[c / 255 for c in color])
    r, gg, b = colorsys.hls_to_rgb((h + dh) % 1.0, min(1, max(0, l + dl)), s)
    return (round(r * 255), round(gg * 255), round(b * 255))


def stat_background(g, rect, color, solid):
    """Fill the tile with the value's color: flat, or Grafana's soft diagonal gradient."""
    x0, y0, x1, y1 = [round(v) for v in rect]
    if x1 - x0 < 2 or y1 - y0 < 2:
        return
    if solid:
        fill = color
        g.d.rounded_rectangle((x0, y0, x1, y1), radius=4 * g.u, fill=fill)
        return
    img = stat_gradient(x1 - x0, y1 - y0, stat_shift(color, 8 / 360, -0.15),
                        stat_shift(color, -8 / 360, -0.05))
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, img.size[0] - 1, img.size[1] - 1),
                                           radius=4 * g.u, fill=255)
    g.img.paste(img, (x0, y0), mask)


def stat_sparkline(g, rect, it, line, fill, conf):
    """A sparkline of the field's whole series in rect (pixels): the line and a wash under
    it, over the dashboard's time range."""
    x0, y0, x1, y1 = rect
    times, vals = it["times"], it["values"]
    pts = [(t, v) for t, v in zip(times, vals)
           if isinstance(t, (int, float)) and isinstance(v, (int, float))
           and not isinstance(v, bool) and math.isfinite(v)]
    if len(pts) < 2 or x1 - x0 < 4 or y1 - y0 < 4:
        return
    lo, hi = conf.get("min"), conf.get("max")
    ys = [v for _, v in pts]
    lo = lo if isinstance(lo, (int, float)) else min(ys)
    hi = hi if isinstance(hi, (int, float)) else max(ys)
    if hi <= lo:
        lo, hi = lo - 1, hi + 1
    t0 = g.start if g.start is not None else pts[0][0]
    t1 = g.end if g.end is not None else pts[-1][0]
    if t1 <= t0:
        t0, t1 = pts[0][0], pts[-1][0] + 1
    step = max(1, len(pts) // max(2, int((x1 - x0) // 1.5)))
    pts = pts[::step] + ([pts[-1]] if (len(pts) - 1) % step else [])
    pad = g.u
    xy = [(x0 + (t - t0) / (t1 - t0) * (x1 - x0),
           y1 - pad - (min(max(v, lo), hi) - lo) / (hi - lo) * (y1 - y0 - 2 * pad)) for t, v in pts]
    xy = [(min(max(x, x0), x1), y) for x, y in xy]
    g.d.polygon([(xy[0][0], y1)] + xy + [(xy[-1][0], y1)], fill=fill)
    g.d.line(xy, fill=line, width=max(1, round(1.5 * g.u)), joint="curve")


def stat_text_parts(mode, count, it, name, text, suffix):
    """(title, value text, unit, alignment-title?) per textMode, the way a stat panel picks."""
    if mode == "auto":
        mode = "value" if count == 1 else "value_and_name"
    if mode == "name":
        return None, name, "", mode
    if mode == "value":
        return None, text, suffix, mode
    if mode == "none":
        return None, "", "", mode
    return name, text, suffix, mode


def stat_tile(g, rect, it, i, count, align, opts):
    """One value in rect (CSS px, in the box): its background, text and sparkline."""
    x, y, w, h = rect
    ox, oy = g.box[0] + x * g.u, g.box[1] + y * g.u
    text, suffix, color = stat_show(g, it, i)
    pre, post = opts.get("affix", ("", ""))
    text, suffix = (pre + text if pre else text), (suffix + post if post else suffix)
    color_mode = opts["color_mode"]
    bg = color_mode in ("background", "background_solid")
    title, vtext, vsuffix, mode = stat_text_parts(opts["text_mode"], count, it, it["name"],
                                                  text, suffix)
    if mode == "name":
        a_text, a_suffix, a_title = align["title"], "", None
    elif mode == "value":
        a_text, a_suffix, a_title = align["text"], align["suffix"], None
    else:
        a_text, a_suffix, a_title = align["text"], align["suffix"], align["title"]
    if count == 1:
        a_text, a_suffix, a_title = vtext, vsuffix, title

    if bg:
        stat_background(g, (ox, oy, ox + w * g.u, oy + h * g.u), color, color_mode == "background_solid")
    ink = g.theme.ink
    vcolor = color if color_mode == "value" else stat_contrast(g, color) if bg else ink
    tcolor = stat_contrast(g, color) if bg else g.theme.ink_2

    pad = 2 if mode == "none" else 12 if h > 100 else 8
    mw, mh = w - 2 * pad, h - 2 * pad
    sparkline = (opts["graph_mode"] == "area" and mode is not None and len(it["times"]) > 1
                 and not opts["all_values"])
    spark = bool(sparkline and it["field"].type == "number")
    center = opts["justify"] == "center" or not title
    wide = opts["wide"] and w / max(h, 1) > 2.5
    man = opts["text"]
    LH = 1.2
    tfs = vfs = 0
    chart_h = 0

    def calc(txt, cw, ch, maxs=None, suf=""):
        return stat_fit(g, txt, cw, ch, LH, maxs, False, suf) if txt else 0

    # the four layouts, each choosing its font sizes and where the chart goes
    if wide and h > 50 and spark:
        kind = "wide_chart"
        chart_h = h * 0.5
        tfs = calc(a_title, mw * 0.6, mh * 0.4, 30) if a_title else 0
        vfs = calc(a_text, mw * 0.4, mh * 0.5, None, a_suffix)
    elif wide:
        kind = "wide"
        vfs = calc(a_text, mw * (0.3 if a_title else 1.0), mh, None, a_suffix)
        if a_title:
            tfs = min(vfs * 0.7, calc(a_title, mw * 0.6, mh, 30))
    elif h > 100 and spark:
        kind = "stack_chart"
        chart_h = h * 0.25
        th = 0
        if a_title:
            tfs = calc(a_title, mw, h * 0.15, 30)
            th = tfs * LH
        vfs = calc(a_text, mw, mh - chart_h - th, None, a_suffix)
        if a_title:
            tfs = min(vfs * 0.7, tfs)
        chart_h = h - tfs * LH - vfs * LH
    else:
        kind = "stack"
        th = 0
        if a_title:
            tfs = calc(a_title, mw, h * 0.15, 30)
            th = tfs * LH
        vfs = calc(a_text, mw, mh - th, None, a_suffix)
        if a_title:
            tfs = min(vfs * 0.7, tfs)
    if man.get("valueSize"):
        vfs = float(man["valueSize"])
    if man.get("titleSize"):
        tfs = float(man["titleSize"])

    # the sparkline
    if kind in ("wide_chart", "stack_chart") and chart_h > 4:
        if bg:
            line, wash = mix(color, (255, 255, 255), 0.4), (255, 255, 255, 100)
        else:
            line, wash = color, color + (51,)
        stat_sparkline(g, (ox, oy + (h - chart_h) * g.u, ox + w * g.u, oy + h * g.u),
                       it, line, wash, it["conf"])

    if mode == "none":
        return
    vtext_w = stat_width(g, vtext, vfs) + (stat_width(g, vsuffix, vfs * STAT_UNIT_SCALE) if vsuffix else 0)
    ttext = squeeze(g.d, title, g.font(tfs), (mw if kind.startswith("stack") else mw * 0.6) * g.u) if title else ""
    tw = stat_width(g, ttext, tfs) if ttext else 0

    def value_at(vx, vy, anchor):
        stat_run(g, ox, oy, vx, vy, vtext, vsuffix, vfs, vcolor, False, anchor)

    if kind.startswith("stack"):
        block = (tfs * LH if ttext else 0) + vfs * LH
        top = pad if kind == "stack_chart" else (h - block) / 2
        cx = w / 2
        if ttext:
            stat_put(g, ox, oy, cx if center else pad, top, ttext, tfs, tcolor, False,
                     "m" if center else "l")
        value_at(cx if center else pad, top + (tfs * LH if ttext else 0), "m" if center else "l")
    else:
        row_h = max(vfs, tfs) * LH
        top = pad if kind == "wide_chart" else (h - row_h) / 2
        vy = top + (row_h - vfs * LH) / 2
        ty = top + (row_h - tfs * LH) / 2
        if center:
            gap = 0.75 * vfs * 0.5
            total = tw + (gap if ttext else 0) + vtext_w
            start = (w - total) / 2
            if ttext:
                stat_put(g, ox, oy, start, ty, ttext, tfs, tcolor)
            value_at(start + tw + (gap if ttext else 0), vy, "l")
        else:
            if ttext:
                stat_put(g, ox, oy, pad, ty, ttext, tfs, tcolor)
            value_at(w - pad, vy, "r")


def stat_options(g, panel_type="stat"):
    """The panel's options with Grafana's defaults filled in."""
    o = g.options
    text_mode = o.get("textMode") or "auto"
    if text_mode == "auto" and ((g.panel.get("fieldConfig") or {}).get("defaults", {}).get("displayName")
                                or not g.title):
        text_mode = "value_and_name"
    return {"color_mode": o.get("colorMode") or "value", "graph_mode": o.get("graphMode") or "area",
            "justify": o.get("justifyMode") or "auto", "text_mode": text_mode,
            "wide": o.get("wideLayout", True) is not False, "text": o.get("text") or {},
            "all_values": bool((o.get("reduceOptions") or {}).get("values"))}


def stat_draw(g, affix=("", "")):
    """Draw a stat panel's values (a singlestat comes here too, with its prefix and postfix)."""
    items = stat_items(g)
    if not items:
        return g.no_data()
    opts = stat_options(g)
    opts["affix"] = affix
    x0, y0, x1, y1 = g.box
    w, h = (x1 - x0) / g.u, (y1 - y0) / g.u
    rects, _ = stat_repeat(w, h, len(items), stat_orientation(g), 3, True)
    shown = [stat_show(g, it, i) for i, it in enumerate(items)]
    align = {"text": max((s[0] for s in shown), key=len, default=""),
             "suffix": max((s[1] for s in shown), key=len, default=""),
             "title": max((it["name"] for it in items), key=len, default="")}
    for i, (it, rect) in enumerate(zip(items, rects)):
        if rect[1] + rect[3] > h + 1 and i:
            break
        stat_tile(g, rect, it, i, len(items), align, opts)


def stat_orientation(g):
    """The panel's orientation option."""
    return g.options.get("orientation") or "auto"


@drawer("stat")
def draw_stat(g):
    """A stat panel: a big value per field, with its color, name and sparkline."""
    stat_draw(g)


# --- singlestat, the old Angular panel ----------------------------------------------------------

STAT_OLD_CALCS = {"avg": "mean", "current": "lastNotNull", "total": "sum", "name": "mean",
                  "first": "firstNotNull", "last": "last", "last_time": "lastNotNull"}


def stat_old_mappings(p, steps):
    """Grafana's convertOldAngularValueMappings: valueMaps / rangeMaps -> new mappings, each
    colored the way the thresholds would have colored its text."""
    kind = p.get("mappingType")
    if not kind:
        kind = 1 if p.get("valueMaps") else 2 if p.get("rangeMaps") else None

    def color_of(text):
        try:
            num = float(text)
        except (TypeError, ValueError):
            return None
        out = None
        for st in steps or []:
            if st.get("value") is None or num >= st["value"]:
                out = st.get("color")
        return out

    values, out = {}, []
    for m in (p.get("valueMaps") if kind == 1 else p.get("rangeMaps") if kind == 2 else None) or []:
        if not isinstance(m, dict):
            continue
        result = {"text": m.get("text")}
        c = color_of(m.get("text"))
        if c:
            result["color"] = c
        if kind == 1:
            if m.get("value") is None:
                continue
            if m["value"] == "null":
                out.append({"type": "special", "options": {"match": "null", "result": result}})
            else:
                values[str(m["value"])] = result
        elif m.get("from") == "null" or m.get("to") == "null":
            out.append({"type": "special", "options": {"match": "null", "result": result}})
        else:
            try:
                out.append({"type": "range", "options": {"from": float(m.get("from")),
                                                         "to": float(m.get("to")), "result": result}})
            except (TypeError, ValueError):
                pass
    return ([{"type": "value", "options": values}] if values else []) + out


def stat_from_singlestat(p):
    """A singlestat panel's JSON -> the stat (or gauge) panel Grafana's own migration makes of
    it. A new dict: the original is left alone."""
    steps = None
    d = {}
    if p.get("format"):
        d["unit"] = p["format"]
    if p.get("nullText"):
        d["noValue"] = p["nullText"]
    if isinstance(p.get("decimals"), (int, float)) and not isinstance(p.get("decimals"), bool):
        d["decimals"] = p["decimals"]
    colors = p.get("colors")
    if p.get("thresholds") not in (None, "") and isinstance(colors, list) and colors:
        levels = []
        for s in str(p["thresholds"]).split(","):
            try:
                levels.append(float(s.strip()))
            except ValueError:
                levels.append(None)
        steps = [{"value": levels[i - 1] if 0 < i <= len(levels) else None, "color": c}
                 for i, c in enumerate(colors)]
        steps = [s for s in steps if s["value"] is not None or s is steps[0]]
        d["thresholds"] = {"mode": "absolute", "steps": steps}
    maps = stat_old_mappings(p, steps)
    if maps:
        d["mappings"] = maps
    calc = STAT_OLD_CALCS.get(p.get("valueName") or "avg", p.get("valueName") or "mean")
    ro = {"calcs": [calc if calc in CALC_NAMES else "mean"], "values": False}
    if p.get("tableColumn"):
        ro["fields"] = "/^" + re.escape(str(p["tableColumn"])) + "$/"
    gauge = p.get("gauge") or {}
    spark = p.get("sparkline") or {}
    fc = p.get("fieldConfig") or {}
    opts = {"reduceOptions": ro, "orientation": "horizontal"}
    kind = "stat"
    if gauge.get("show"):
        kind = "gauge"
        d["min"], d["max"] = gauge.get("minValue"), gauge.get("maxValue")
        opts["showThresholdMarkers"] = bool(gauge.get("thresholdMarkers", True))
        opts["showThresholdLabels"] = bool(gauge.get("thresholdLabels", False))
    else:
        opts["graphMode"] = "area" if spark.get("show") else "none"
        opts["colorMode"] = ("background" if p.get("colorBackground")
                             else "value" if p.get("colorValue") else "none")
        if opts["colorMode"] == "none" and spark.get("lineColor") and opts["graphMode"] == "area":
            d["color"] = {"mode": "fixed", "fixedColor": spark["lineColor"]}
        opts["textMode"] = "name" if p.get("valueName") == "name" else "auto"
    if not d.get("color"):
        d["color"] = {"mode": "thresholds"}
    return {"id": p.get("id"), "title": p.get("title"), "type": kind, "options": opts,
            "fieldConfig": {"defaults": d, "overrides": fc.get("overrides") or []},
            "targets": p.get("targets")}, (p.get("prefix") or "", p.get("postfix") or "")


def stat_null_as_zero(g):
    """singlestat's "null as zero": the frames with their gaps filled with 0."""
    frames = []
    for fr in g.frames:
        fields = [Field(f.name, f.type, [0 if v is None else v for v in f.values], f.labels, f.config)
                  if f.type == "number" else f for f in fr.fields]
        frames.append(Frame(fields, fr.name, fr.ref_id, fr.meta))
    g.frames = frames


@drawer("singlestat", "grafana-singlestat-panel")
def draw_singlestat(g):
    """A singlestat panel: its options turned into a stat's (or a gauge's, when it has a
    gauge), the way Grafana's migration does, and drawn as that."""
    new, affix = stat_from_singlestat(g.panel)
    g.panel, g.options = new, new["options"]
    if "null as zero" in str(((new.get("fieldConfig") or {}).get("defaults") or {}).get("nullValueMode", "")):
        stat_null_as_zero(g)
    if new["type"] == "gauge":
        return gauge_draw(g)
    stat_draw(g, affix)


# --- gauge ------------------------------------------------------------------------------------

def gauge_range(conf):
    """(min, max) the way the gauge panel picks them: the field's, or 0-100 (0-1 for a
    fraction), bumped apart if they came out equal."""
    unit = conf.get("unit")
    lo = conf.get("min") if isinstance(conf.get("min"), (int, float)) else 0
    hi = conf.get("max") if isinstance(conf.get("max"), (int, float)) else (1 if unit == "percentunit" else 100)
    if lo == hi:
        lo, hi = lo - lo * 0.1, hi + hi * 0.1
        if lo == hi:
            lo, hi = lo - 1, hi + 1
    return lo, hi


def gauge_dimensions(w, h, end_angle, rounded, bar_factor, bar_index, thr_bar, labels):
    """Grafana's calculateDimensions: the centre, radius and bar widths of a gauge in w x h."""
    y_max = 180 if end_angle > 180 else end_angle
    max_w = w / 2
    max_h = h / (1 + math.sin(math.radians(y_max - 90)))
    max_r = max(1, min(max_w, max_h))
    limited_by_h = max_h == max_r
    outer = max_r
    bar = max(bar_factor * (max_r / 3), 2)
    endcap = bar * 0.5 if y_max < 180 and rounded else 0
    outer -= endcap * 0.5
    max_h -= endcap * 0.5
    max_w -= endcap * 0.5
    label_fs = label_space = label_r = 0
    if labels:
        label_r = outer
        label_fs = max(0.12 * outer ** 0.92, 10)
        label_space = label_fs / 3
        size = label_fs * 1.2 + label_space
        outer -= size
        max_w -= size
        max_h -= size
        if limited_by_h and end_angle < 180:
            amount = outer * 0.07
            label_r -= amount
            max_h -= amount
            max_w -= amount
            outer -= amount
    thr_w = thr_space = thr_r = 0
    if thr_bar:
        thr_w = min(max(0.2 * bar ** 0.92, 4), 12)
        thr_space = min(max(thr_w / 2, 2), 12)
        thr_r = outer - thr_w / 2
        max_w -= thr_w + thr_space
        max_h -= thr_w + thr_space
        outer = min(max_w, max_h)
    radius = outer - bar / 2
    below = max_r * math.sin(math.radians(y_max - 90))
    rest = h - below - max_r
    cy = max_r + rest / 2 - (endcap - endcap * 0.5)
    if bar_index > 0:
        radius -= (bar + 4) * bar_index
    return {"cx": w / 2, "cy": cy, "radius": radius, "bar": bar, "thr_w": thr_w, "thr_r": thr_r,
            "label_fs": label_fs, "label_r": label_r}


def gauge_arc(g, ox, oy, dim, radius, width, start, sweep, color, caps=False):
    """A ring segment from start degrees (clockwise from 12 o'clock), sweep degrees long."""
    if sweep <= 0.01 or width <= 0 or radius <= 0:
        return
    sweep = min(sweep, 360)
    n = max(2, int(sweep / 1.5) + 1)
    cx, cy = ox + dim["cx"] * g.u, oy + dim["cy"] * g.u
    ro, ri = (radius + width / 2) * g.u, max(0.0, (radius - width / 2) * g.u)
    angles = [math.radians(start + sweep * k / n - 90) for k in range(n + 1)]
    outer = [(cx + ro * math.cos(a), cy + ro * math.sin(a)) for a in angles]
    inner = [(cx + ri * math.cos(a), cy + ri * math.sin(a)) for a in reversed(angles)]
    g.d.polygon(outer + inner, fill=color)
    if caps:
        rr = width / 2 * g.u
        for a in (angles[0], angles[-1]):
            px, py = cx + radius * g.u * math.cos(a), cy + radius * g.u * math.sin(a)
            g.d.ellipse((px - rr, py - rr, px + rr, py + rr), fill=color)


def gauge_step_color(steps, value):
    """The step a value falls in: the last whose value it has reached."""
    found = steps[0]
    for st in steps:
        if st.get("value") is None or (isinstance(st["value"], (int, float)) and value >= st["value"]):
            found = st
    return found


def gauge_threshold_stops(g, conf, lo, hi):
    """Grafana's getFormattedThresholds: [(value, color)] marking where each band ends,
    from min to max, each colored by the step below it."""
    th = conf.get("thresholds") or {}
    steps = th.get("steps") or [{"value": None, "color": "green"}, {"value": 80, "color": "red"}]
    percent = th.get("mode") == "percentage"
    a, b = (0, 100) if percent else (lo, hi)
    first, last = gauge_step_color(steps, a), gauge_step_color(steps, b)
    out = [(a, g.theme.color(first.get("color")))]
    skip = True
    for i, st in enumerate(steps):
        if skip:
            if st is first:
                skip = False
            continue
        prev = steps[i - 1]
        v = st.get("value")
        out.append((v if isinstance(v, (int, float)) and math.isfinite(v) else a,
                    g.theme.color(prev.get("color"))))
        if st is last:
            break
    lastv = last.get("value")
    if b > (lastv if isinstance(lastv, (int, float)) else -math.inf):
        out.append((b, g.theme.color(last.get("color"))))
    return out, percent


def gauge_scale_labels(g, ox, oy, dim, conf, stops, percent, lo, hi, start, sweep, shape):
    """The threshold values around the gauge, turned to sit on its arc."""
    fs = dim["label_fs"]
    radius = dim["label_r"] - fs * 1.2
    path_len = sweep / 360 * 2 * math.pi * radius
    labels = []
    for v, _ in stops:
        real = (v / 100 * (hi - lo) + lo) if percent else v
        if lo <= real <= hi:
            labels.append((real, f"{v:g}%" if percent else format_value(real, conf.get("unit"),
                                                                       None)[0]))
    if not labels:
        return
    vmin, vmax = min(r for r, _ in labels), max(r for r, _ in labels)
    f = g.font(fs)
    for real, text in labels:
        off = (real - lo) / (hi - lo) * path_len
        tw = stat_width(g, text, fs)
        if sweep >= 360:
            pad = tw * 0.65
            off += pad if real == vmin else -pad if real == vmax else 0
        else:
            if real == vmin and off - tw / 2 < 0:
                off += tw / 2
            if real == vmax and off + tw / 2 > path_len:
                off -= tw / 2
        ang = start + off / path_len * sweep if path_len else start
        gauge_rotated(g, ox, oy, dim, radius + fs * 0.4, ang, text, f)


def gauge_rotated(g, ox, oy, dim, radius, angle, text, f):
    """text centered at radius, angle (degrees clockwise from 12), turned to follow the arc."""
    tw = int(g.d.textlength(text, font=f)) + 4
    th = int(f.size * 1.6) + 2
    tile = Image.new("L", (tw, th), 0)
    ImageDraw.Draw(tile).text((tw / 2, th / 2), text, font=f, fill=255, anchor="mm")
    tile = tile.rotate(-angle, expand=True, resample=Image.Resampling.BICUBIC)
    a = math.radians(angle - 90)
    px = ox + dim["cx"] * g.u + radius * g.u * math.cos(a)
    py = oy + dim["cy"] * g.u + radius * g.u * math.sin(a)
    g.img.paste(Image.new("RGB", tile.size, g.theme.ink), (round(px - tile.size[0] / 2),
                                                           round(py - tile.size[1] / 2)), tile)


def gauge_text(g, ox, oy, dim, shape, mode, name, text, suffix, color, man, has_spark=False):
    """The value (and name) in the middle of the gauge, sized from its radius."""
    r, bar = dim["radius"], dim["bar"]
    show_value, show_name = mode in ("value", "value_and_name"), mode in ("name", "value_and_name")
    if mode == "none":
        return
    max_w = r * 2 - bar - r / 7
    mvh = 0.82 * max(r, 0) ** 0.86
    mnh = (0.45 * max(r, 0) ** 0.86) if (show_value and show_name) else r / 4
    LH = 1.21
    vfs = float(man["valueSize"]) if man.get("valueSize") else max(
        stat_fit(g, text, max_w, mvh, LH, None, False, suffix), 1)
    nfs = float(man["titleSize"]) if man.get("titleSize") else max(
        stat_fit(g, name or " ", max_w, mnh, LH), 10)
    vh, nh = vfs * LH, nfs * LH
    vy = dim["cy"] - nh * 0.3 if show_name else dim["cy"]
    ny = vy + vh * 0.7 if show_value else dim["cy"]
    off = (vh + nh) / 4 if shape == "gauge" else 0
    if show_value:
        stat_run(g, ox, oy, dim["cx"], vy - off - vh / 2, text, suffix, vfs, color, False, "m", LH)
    if show_name:
        label = squeeze(g.d, name or "", g.font(nfs), max_w * g.u)
        stat_put(g, ox, oy, dim["cx"], ny - off - nh / 2, label, nfs,
                 g.theme.ink_2 if show_value else g.theme.ink, False, "m")


def gauge_one(g, rect, it, i, count, opts):
    """One radial gauge in rect (CSS px)."""
    x, y, w, h = rect
    ox, oy = g.box[0] + x * g.u, g.box[1] + y * g.u
    conf = dict(it["conf"])
    lo, hi = gauge_range(conf)
    conf["min"], conf["max"] = lo, hi
    it = dict(it, conf=conf)
    text, suffix, color = stat_show(g, it, i)
    shape = opts["shape"]
    start, end = (250, 110) if shape == "gauge" else (0, 360)
    sweep = (360 % (start or 1)) + end if shape == "gauge" else 360
    thr_bar = opts["markers"]
    dim = gauge_dimensions(w, h, end if shape == "gauge" else 360, opts["rounded"],
                           opts["bar_width"], 0, thr_bar, opts["labels"])
    value = it["value"] if isinstance(it["value"], (int, float)) and math.isfinite(it["value"]) else lo
    value = min(max(value, lo), hi)
    angle = (value - lo) / (hi - lo) * sweep
    s0, s1 = 0.0, angle
    neutral = opts["neutral"]
    if isinstance(neutral, (int, float)):
        n_ang = (min(max(neutral, lo), hi) - lo) / (hi - lo) * sweep
        s0, s1 = (n_ang, angle - n_ang) if n_ang <= angle else (angle, n_ang - angle)
    track = mix(g.theme.surface, g.theme.ink, 0.09)
    r, bw, caps = dim["radius"], dim["bar"], opts["rounded"]
    if s0:
        gauge_arc(g, ox, oy, dim, r, bw, start, s0, track, caps)
    gauge_arc(g, ox, oy, dim, r, bw, start + s0 + s1, sweep - s0 - s1, track, caps)
    gauge_arc(g, ox, oy, dim, r, bw, start + s0, s1, color, caps)
    if thr_bar or opts["labels"]:
        stops, percent = gauge_threshold_stops(g, conf, lo, hi)
        if opts["labels"]:
            gauge_scale_labels(g, ox, oy, dim, conf, stops, percent, lo, hi, start, sweep, shape)
        if thr_bar and it["conf"].get("min") != it["conf"].get("max"):
            cur = 0.0
            for v, col in stops[1:]:
                frac = (v / 100) if percent else (v - lo) / (hi - lo)
                deg = min(max(frac * sweep, 0), sweep)
                if deg - cur > 0:
                    gauge_arc(g, ox, oy, dim, dim["thr_r"], dim["thr_w"], start + cur, deg - cur, col)
                    cur = deg
    mode = opts["text_mode"]
    if mode == "auto":
        mode = "value_and_name" if count > 1 or opts["has_name"] else "value"
    gauge_text(g, ox, oy, dim, shape, mode, it["name"], text, suffix, color, opts["text"])


def gauge_draw(g):
    """Draw a gauge panel: one arc per value, sharing the space the way Grafana does."""
    items = stat_items(g)
    if not items:
        return g.no_data()
    o = g.options
    manual = o.get("sizing") == "manual"
    opts = {"shape": "circle" if o.get("shape") == "circle" else "gauge",
            "rounded": o.get("barShape") == "rounded",
            "bar_width": o.get("barWidthFactor") if isinstance(o.get("barWidthFactor"), (int, float)) else 0.5,
            "markers": o.get("showThresholdMarkers", True) is not False,
            "labels": bool(o.get("showThresholdLabels")),
            "neutral": o.get("neutral"), "text": o.get("text") or {},
            "text_mode": o.get("textMode") or "auto",
            "has_name": bool(((g.panel.get("fieldConfig") or {}).get("defaults") or {}).get("displayName"))}
    x0, y0, x1, y1 = g.box
    w, h = (x1 - x0) / g.u, (y1 - y0) / g.u
    rects, _ = stat_repeat(w, h, len(items), stat_orientation(g), 16, True,
                           (o.get("minVizWidth") or 75) if manual else 0,
                           (o.get("minVizHeight") or 75) if manual else 0)
    for i, (it, rect) in enumerate(zip(items, rects)):
        if i and (rect[1] + rect[3] > h + 1 or rect[0] + rect[2] > w + 1):
            break
        gauge_one(g, rect, it, i, len(items), opts)


@drawer("gauge")
def draw_gauge(g):
    """A gauge panel: a ring filled to the value, with its threshold band and labels."""
    gauge_draw(g)


# --- bar gauge --------------------------------------------------------------------------------

def bargauge_title(g, w, h, vertical, title, placement, man):
    """Grafana's calculateTitleDimensions: (font size, placement, width, height) of the name."""
    if not title:
        return 0, "above", 0, 0
    if placement == "hidden":
        return 0, "hidden", 0, 0
    if vertical:
        fs = float(man.get("titleSize") or 14)
        return fs, "below", w, fs * 1.5
    above = (h > 40 and placement in (None, "auto")) or placement == "top"
    if above:
        if man.get("titleSize"):
            fs = float(man["titleSize"])
            return fs, "above", 0, fs * 1.5
        th = max(min(h * 0.45, 50), 17)
        return th / 1.5, "above", 0, th
    th = max(h * 0.6, 18)
    fs = th / 1.5
    tw = min(stat_width(g, title, fs) + 15, w * 0.4)
    return float(man.get("titleSize") or fs), "left", tw, 0


def bargauge_dims(g, w, h, vertical, td, value_string, hide_value, man):
    """Grafana's calculateBarAndValueDimensions: the value box and the room for the bar."""
    fs, place, tw, th = td
    measure = float(man.get("valueSize") or max(fs, 12))
    real = stat_width(g, value_string, measure) + 20
    if vertical:
        vh = float(man["valueSize"]) if man.get("valueSize") else min(max(h * 0.1, 18), 50)
        vw = w
        if hide_value:
            vh = vw = 0
        return {"vw": vw, "vh": vh, "bar_w": w, "bar_h": h - (th + vh), "wrap_h": h - th, "wrap_w": w}
    if hide_value:
        vw = vh = 0
    else:
        vh = h - th
        vw = max(min(w * 0.2, 150), real)
    return {"vw": vw, "vh": vh, "bar_w": w - vw - tw, "bar_h": h - th, "wrap_h": h if place != "above" else h - th,
            "wrap_w": w - tw if place != "above" else w}


def bargauge_stops(g, conf, lo, hi, value, size):
    """The colors along a gradient bar max size px long -> [(position px, color)], as
    Grafana's getBarGradient lays them out."""
    mode = (conf.get("color") or {}).get("mode") or "thresholds"
    if mode == "thresholds":
        th = conf.get("thresholds") or {}
        steps = th.get("steps") or [{"value": None, "color": "green"}, {"value": 80, "color": "red"}]
        stops, last = [], 0.0
        for i, st in enumerate(steps):
            col = g.theme.color(st.get("color"))
            sv = st.get("value")
            if i == 0:
                stops += [(0.0, col)]
                continue
            tv = lo + (hi - lo) * sv / 100 if th.get("mode") == "percentage" else sv
            if not isinstance(tv, (int, float)) or value < tv:
                break
            pos = min(max((tv - lo) / (hi - lo), 0), 1) * size
            off = round(pos - (pos - last) / 2)
            if len(stops) == 1:
                stops.append((off / 2, stops[0][1]))
            stops.append((float(off), col))
            last = pos
        return stops
    if mode in CONTINUOUS:
        cols = [g.theme.color(c) for c in CONTINUOUS[mode]]
        return [(i / (len(cols) - 1) * size, c) for i, c in enumerate(cols)]
    return None


def bargauge_fill(g, x0, y0, x1, y1, vertical, stops, color, size_px, radius):
    """Paint the rectangle with a gradient (stops in px from its start edge) or a flat color."""
    w, h = round(x1 - x0), round(y1 - y0)
    if w < 1 or h < 1:
        return
    if not stops or len(stops) < 2:
        g.d.rounded_rectangle((x0, y0, x1, y1), radius=radius, fill=(stops[0][1] if stops else color))
        return
    n = h if vertical else w
    pos = np.array([s[0] * g.u for s in stops])
    cols = np.array([s[1] for s in stops], float)
    xs = np.arange(n) + 0.5
    if vertical:
        xs = size_px - xs  # a vertical bar fills from the bottom up
    line = np.stack([np.interp(xs, pos, cols[:, k]) for k in range(3)], axis=1)
    arr = np.repeat(line[:, None, :], w, axis=1) if vertical else np.repeat(line[None, :, :], h, axis=0)
    img = Image.fromarray(arr.astype("uint8"), "RGB")
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
    g.img.paste(img, (round(x0), round(y0)), mask)


def bargauge_one(g, rect, it, i, count, opts, align):
    """One bar in rect (CSS px)."""
    x, y, w, h = rect
    ox, oy = g.box[0] + x * g.u, g.box[1] + y * g.u
    u = g.u
    vertical = opts["vertical"]
    conf = dict(it["conf"])
    lo = conf["min"] if isinstance(conf.get("min"), (int, float)) else 0
    hi = conf["max"] if isinstance(conf.get("max"), (int, float)) else 100
    conf["min"], conf["max"] = lo, hi
    it = dict(it, conf=conf)
    text, suffix, color = stat_show(g, it, i)
    num = it["value"] if isinstance(it["value"], (int, float)) and math.isfinite(it["value"]) else None
    title = it["name"] if opts["show_name"] else ""
    man = opts["text"]
    td = bargauge_title(g, w, h, vertical, align["title"] if count > 1 else title, opts["name_placement"], man)
    hide_value = opts["value_mode"] == "hidden" or not opts["show_value"]
    vstr = (align["text"] + align["suffix"]) if count > 1 else text + suffix
    dm = bargauge_dims(g, w, h, vertical, td, vstr, hide_value, man)
    fs, place, tw, th = td
    ratio = 0 if num is None or hi == lo else min((num - lo) / (hi - lo), 1)
    ratio = max(ratio, 0) if num is not None else 0
    vcolor = g.theme.ink if opts["value_mode"] == "text" else color
    surface_bar = mix(g.theme.surface, g.theme.ink, 0.07)

    # the name
    if place != "hidden" and title:
        label = squeeze(g.d, title, g.font(fs), (w if place in ("above", "below") else tw - 10) * u)
        if place == "above":
            stat_put(g, ox, oy, 0, 0, label, fs, g.theme.ink_2, False, "l", 1.5)
        elif place == "below":
            stat_put(g, ox, oy, w / 2, h - th, label, fs, g.theme.ink_2, False, "m", 1.5)
        else:
            stat_put(g, ox, oy, tw - 10, (h - fs * 1.5) / 2, label, fs, g.theme.ink_2, False, "r", 1.5)

    # where the bar and value go inside the tile
    if vertical:
        bx, by = 0, dm["vh"]
        bw_, bh_ = dm["bar_w"], dm["bar_h"]
    else:
        bx = tw if place == "left" else 0
        by = th if place == "above" else 0
        bw_, bh_ = dm["bar_w"], dm["bar_h"]
    bar_len = bh_ if vertical else bw_

    if opts["mode"] == "lcd":
        spc = 2
        lcd_w = 12
        cells = max(1, math.floor(bar_len / lcd_w))
        size = max(1, math.floor((bar_len - spc * cells) / cells))
        for c in range(cells):
            pv = lo + (hi - lo) / cells * c
            col = value_color(conf, pv, g.theme, i, lo, hi)
            lit = num is not None and pv <= num
            fill = col if lit else mix(g.theme.surface, col, 0.18)
            if vertical:
                bot = by + bh_ - c * (size + spc)
                box = (ox + bx * u, oy + (bot - size) * u, ox + (bx + bw_) * u, oy + bot * u)
            else:
                cx = bx + c * (size + spc)
                box = (ox + cx * u, oy + by * u, ox + (cx + size) * u, oy + (by + bh_) * u)
            g.d.rounded_rectangle(box, radius=2 * u, fill=fill)
    else:
        r = 2 * u
        g.d.rounded_rectangle((ox + bx * u, oy + by * u, ox + (bx + bw_) * u, oy + (by + bh_) * u),
                              radius=r, fill=surface_bar) if opts["unfilled"] else None
        amount = max(ratio * bar_len, 1)
        if vertical:
            fx0, fy0, fx1, fy1 = ox + bx * u, oy + (by + bh_ - amount) * u, ox + (bx + bw_) * u, oy + (by + bh_) * u
        else:
            fx0, fy0, fx1, fy1 = ox + bx * u, oy + by * u, ox + (bx + amount) * u, oy + (by + bh_) * u
        if opts["mode"] == "basic":
            g.d.rounded_rectangle((fx0, fy0, fx1, fy1), radius=r, fill=color + (89,))
            e = max(1, round(2 * u))
            edge = (fx0, fy0, fx1, fy0 + e) if vertical else (fx1 - e, fy0, fx1, fy1)
            g.d.rectangle(edge, fill=color)
        else:
            stops = bargauge_stops(g, conf, lo, hi, num if num is not None else lo, bar_len)
            bargauge_fill(g, fx0, fy0, fx1, fy1, vertical, stops, color,
                          bar_len * u, r)

    # the value
    if not hide_value:
        a_text = (align["text"], align["suffix"]) if count > 1 else (text, suffix)
        if vertical:
            vfs = float(man["valueSize"]) if man.get("valueSize") else stat_fit(
                g, a_text[0], dm["vw"], dm["vh"], 1.0, None, False, a_text[1])
            stat_run(g, ox, oy, w / 2, 0, text, suffix, vfs, vcolor, False, "m", 1.0)
        else:
            avail = dm["vw"] - 20
            vfs = float(man["valueSize"]) if man.get("valueSize") else stat_fit(
                g, a_text[0], avail, dm["vh"], 1.0, None, False, a_text[1])
            right = bx + bw_ + dm["vw"] - 10 - (15 if opts["overflow"] else 0)
            stat_run(g, ox, oy, right, by + (dm["vh"] - vfs) / 2, text, suffix, vfs, vcolor, False, "r", 1.0)


@drawer("bargauge")
def draw_bargauge(g):
    """A bar gauge panel: a bar per value, gradient / lcd / basic, horizontal or vertical."""
    items = stat_items(g)
    if not items:
        return g.no_data()
    o = g.options
    mode = o.get("displayMode") if o.get("displayMode") in ("lcd", "basic") else "gradient"
    x0, y0, x1, y1 = g.box
    w, h = (x1 - x0) / g.u, (y1 - y0) / g.u
    spacing = 2 if mode == "lcd" else 10
    manual = o.get("sizing") == "manual"
    orient = o.get("orientation") or "auto"
    resolved = orient if orient in ("horizontal", "vertical") else "vertical" if w > h else "horizontal"
    min_w = (o.get("minVizWidth") or 8) if manual and resolved == "vertical" else 8
    min_h = (o.get("minVizHeight") or 16) if manual and resolved == "horizontal" else 16
    max_h = (o.get("maxVizHeight") or 300) if manual and resolved == "horizontal" else 300
    rects, resolved = stat_repeat(w, h, len(items), resolved, spacing, False, min_w, min_h, max_h)
    text_mode = o.get("textMode") or "auto"
    count = len(items)
    has_name = bool(((g.panel.get("fieldConfig") or {}).get("defaults") or {}).get("displayName"))
    show_name = (count != 1 or has_name) if text_mode == "auto" else text_mode in ("name", "value_and_name")
    shown = [stat_show(g, it, i) for i, it in enumerate(items)]
    align = {"text": max((s[0] for s in shown), key=len, default=""),
             "suffix": max((s[1] for s in shown), key=len, default=""),
             "title": max((it["name"] for it in items), key=len, default="") if show_name else ""}
    opts = {"vertical": resolved == "vertical", "mode": mode, "text": o.get("text") or {},
            "unfilled": o.get("showUnfilled", True) is not False,
            "value_mode": o.get("valueMode") or "color",
            "show_value": text_mode not in ("name", "none"), "show_name": show_name,
            "name_placement": o.get("namePlacement") or "auto"}
    last = rects[-1] if rects else (0, 0, 0, 0)
    opts["overflow"] = (last[1] + last[3] > h + 1) if resolved == "horizontal" else False
    for i, (it, rect) in enumerate(zip(items, rects)):
        if i and (rect[1] + rect[3] > h + 1 or rect[0] + rect[2] > w + 1):
            break
        bargauge_one(g, rect, it, i, count, opts, align)


# --- a whole dashboard, once (--snapshot) ------------------------------------------------------

class Board:
    """A dashboard, ready to draw: its JSON, variables, time range and layout."""

    def __init__(self, grafana, doc, meta, overrides=None, frm=None, to=None):
        self.grafana, self.doc, self.meta = grafana, check_dashboard(doc), meta or {}
        self.title = self.doc.get("title") or "dashboard"
        t = self.doc.get("time") or {}
        self.frm, self.to = frm or t.get("from") or "now-6h", to or t.get("to") or "now"
        self.variables = Variables((self.doc.get("templating") or {}).get("list"), grafana)
        self.overrides = overrides or {}
        self.library = {}
        self.sections = []
        self.resolved = False

    def time_range(self, now=None):
        now = now or time.time()
        return parse_time(self.frm, now), parse_time(self.to, now, end=True)

    def refresh_every(self):
        return parse_duration(self.doc.get("refresh") or "") or None

    def prepare(self):
        """Fill in the variables (the first time), then lay the dashboard out."""
        start, end = self.time_range()
        if not self.resolved:
            self.variables.resolve(start, end, self.overrides)
            self.resolved = True
        self.relayout()

    def relayout(self):
        self.sections = layout(self.doc, self.variables, self.grafana, self.library)

    def builtins_for(self, item, width_css=800):
        """The built-in variables ($__interval and the rest) as a panel sees them."""
        start, end = panel_range(item.panel, *self.time_range(), time.time())
        ds = resolve_ds(self.variables, item.panel.get("datasource"), self.datasources()) or {}
        jd = ds.get("jsonData") or {}
        iv = calc_interval_ms(start, end, int(item.panel.get("maxDataPoints") or width_css),
                              item.panel.get("interval") or jd.get("timeInterval"))
        return builtin_vars(start, end, iv, parse_duration(jd.get("timeInterval")) or 15, self.title)

    def datasources(self):
        try:
            return self.grafana.datasources()
        except RuntimeError:
            return []

    def title_of(self, item):
        title = str(item.panel.get("title") or "")
        if not title or "$" not in title and "[[" not in title:
            return title
        # Grafana fills in titles the plain way (a list as {a,b}), and leaves
        # $__rate_interval as it is.
        builtins = {k: v for k, v in self.builtins_for(item).items() if k != "__rate_interval"}
        return self.variables.interpolate(title, "", builtins, scoped_values(item.scoped))

    def interp_for(self, item):
        """A function that fills in variables the way a panel's own text wants them."""
        builtins, scoped = self.builtins_for(item), scoped_values(item.scoped)
        return lambda text: self.variables.interpolate(text, "", builtins, scoped, default="html")

    def row_title(self, section):
        title = str((section.row or {}).get("title") or "")
        return self.variables.interpolate(title, "", {}, scoped_values(section.scoped),
                                          default="text") if title else ""

    def fetch(self, item, width_css):
        """Run a panel's queries -> PanelData."""
        start, end = panel_range(item.panel, *self.time_range(), time.time())
        try:
            queries, hidden, note = build_queries(item, self.variables, self.grafana.datasources(),
                                                  start, end, width_css, self.title)
        except Exception as e:
            traceback.print_exc(file=LOG)
            return PanelData(error=f"couldn't build its queries: {e}", updated=time.time())
        if note:
            return PanelData(note="its data comes from another panel" if note == "dashboard" else note,
                             updated=time.time())
        if not queries:
            return PanelData(updated=time.time())
        try:
            results = self.grafana.query(queries, start, end)
        except RuntimeError as e:
            return PanelData(error=str(e), updated=time.time())
        frames, errors = [], []
        for q in queries:
            fr, err = results.get(q["refId"], ([], None))
            if err:
                errors.append(f"{q['refId']}: {err}" if len(queries) > 1 else str(err))
            if not hidden.get(q["refId"]):
                frames += fr
        transforms = [t for t in item.panel.get("transformations") or []
                      if isinstance(t, dict) and not t.get("disabled")]
        if transforms and frames:
            try:
                frames, _ = apply_transformations(frames, transforms, item.panel)
            except Exception as e:
                traceback.print_exc(file=LOG)
                errors.append(f"couldn't transform its data: {e}")
        return PanelData(frames, "; ".join(errors) or None, time.time())


def snapshot(board, path, width, cw, ch, density, theme):
    """Draw the whole dashboard into one picture, with the panels where a terminal width
    pixels wide would show them."""
    board.prepare()
    spots, rows = place(board.sections, width, cw, ch)
    k = density
    canvas = theme.backdrop(round(width * k), round(rows * ch * k)) or \
        Image.new("RGB", (round(width * k), round(rows * ch * k)), theme.screen)
    canvas = canvas.convert("RGBA")
    d = ImageDraw.Draw(canvas)
    start, end = board.time_range()
    panels = [s for s in spots if s.item]
    with concurrent.futures.ThreadPoolExecutor(6) as pool:
        datas = list(pool.map(lambda s: board.fetch(s.item, s.w * CSS_PER_CELL / ch), panels))
    kinds, failed = {}, 0
    for s, data in zip(panels, datas):
        img = render_panel(s.item, data, s.w * k, s.h * k, ch * k, theme, start, end, time.time(),
                           board.title_of(s.item), board.interp_for(s.item))
        canvas.alpha_composite(img, (round(s.x * k), round(s.y * k)))
        kind = s.item.panel.get("type")
        kinds[kind] = kinds.get(kind, 0) + 1
        failed += bool(data.error)
    for s in spots:
        if s.section:
            title = board.row_title(s.section)
            f = font(theme, ch * k * 0.62, bold=True)
            cx, cy, r = GRID_GAP * k + f.size * 0.3, (s.y + ch / 2) * k, f.size * 0.3
            d.polygon([(cx - r, cy - r * 0.8), (cx + r, cy), (cx - r, cy + r * 0.8)]
                      if s.section.collapsed else
                      [(cx - r, cy - r * 0.5), (cx + r, cy - r * 0.5), (cx, cy + r * 0.6)],
                      fill=theme.ink_2)
            d.text((cx + r * 2.2, cy), title, font=f, fill=theme.ink, anchor="lm")
    canvas.convert("RGB").save(path)
    drawn = sum(n for t, n in kinds.items() if t in PANELS)
    others = sorted(t for t in kinds if t not in PANELS)
    print(f"dashboard: {path}: {len(panels)} panels, {drawn} drawn"
          + (f", {failed} with errors" if failed else "")
          + (f"; not drawn: {', '.join(others)}" if others else ""))


# --- the terminal, taken over ------------------------------------------------------------------

KEY_NAMES = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end",
             "Z": "shift-tab", "5~": "pgup", "6~": "pgdn", "1~": "home", "7~": "home",
             "4~": "end", "8~": "end", "3~": "delete"}


def parse_keys(buf):
    """Bytes from the terminal -> ([keys], what's left: an escape sequence cut short).
    A key is a name ("up", "enter", "esc", ...), a character, or ("mouse", button,
    col, row, pressed) with col and row counted from 0."""
    keys, i = [], 0
    while i < len(buf):
        c = buf[i:i + 1]
        if c == b"\x1b":
            if i + 1 == len(buf):
                return keys, buf[i:]  # maybe the start of a sequence; maybe Esc
            nxt = buf[i + 1:i + 2]
            if nxt == b"[":
                m = re.match(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])", buf[i:])
                if m:
                    keys.append(("mouse", int(m.group(1)), int(m.group(2)) - 1,
                                 int(m.group(3)) - 1, m.group(4) == b"M"))
                    i += m.end()
                    continue
                m = re.match(rb"\x1b\[([0-9;?<]*)([\x40-\x7e])", buf[i:])
                if not m:
                    if re.fullmatch(rb"\x1b\[[0-9;?<]*", buf[i:]):
                        return keys, buf[i:]
                    keys.append("esc")
                    i += 1
                    continue
                params, final = m.group(1).decode(), m.group(2).decode()
                name = KEY_NAMES.get(final if final != "~" else params.split(";")[0] + "~")
                if name:
                    keys.append(name)
                i += m.end()
                continue
            if nxt == b"O" and i + 2 < len(buf):
                name = KEY_NAMES.get(buf[i + 2:i + 3].decode("latin-1"))
                if name:
                    keys.append(name)
                i += 3
                continue
            keys.append("esc")
            i += 1
            continue
        if c in (b"\r", b"\n"):
            keys.append("enter")
        elif c == b"\t":
            keys.append("tab")
        elif c in (b"\x7f", b"\x08"):
            keys.append("backspace")
        elif c == b"\x15":
            keys.append("ctrl-u")
        elif c[0] >= 0x20:
            # A character: as many bytes as its UTF-8 takes.
            n = 1 if c[0] < 0xC0 else 2 if c[0] < 0xE0 else 3 if c[0] < 0xF0 else 4
            if i + n > len(buf):
                return keys, buf[i:]
            keys.append(buf[i:i + n].decode("utf-8", "replace"))
            i += n
            continue
        i += 1
    return keys, b""


class Screen:
    """The terminal, taken over: its alternate screen, keys as they're pressed, the
    mouse, no cursor. leave() puts it all back."""

    def __init__(self):
        self.fd_in, self.fd_out = sys.stdin.fileno(), sys.stdout.fileno()
        if not (os.isatty(self.fd_in) and os.isatty(self.fd_out)):
            raise RuntimeError("this needs a terminal to show the dashboard in "
                               "(or use --snapshot to draw it into a picture)")
        self.saved = termios.tcgetattr(self.fd_in)
        self.pending = b""
        self.active = False

    def enter(self):
        attrs = termios.tcgetattr(self.fd_in)
        attrs[0] &= ~(termios.IXON | termios.ICRNL)              # ^S is a key; Enter is \r
        attrs[3] &= ~(termios.ECHO | termios.ICANON | termios.IEXTEN)
        attrs[6][termios.VMIN], attrs[6][termios.VTIME] = 0, 0
        termios.tcsetattr(self.fd_in, termios.TCSANOW, attrs)
        # The alternate screen, no cursor, no line wrap, mouse clicks and wheel (SGR).
        self.send(b"\x1b[?1049h\x1b[?25l\x1b[?7l\x1b[?1000h\x1b[?1006h\x1b[0m\x1b[2J")
        self.active = True

    def leave(self, goodbye=b""):
        if not self.active:
            return
        self.active = False
        self.send(goodbye + b"\x1b[?1000l\x1b[?1006l\x1b[0m\x1b[?7h\x1b[?25h\x1b[?1049l")
        termios.tcsetattr(self.fd_in, termios.TCSADRAIN, self.saved)

    def size(self):
        """(cols, rows, cell width, cell height) in the pixels the terminal reports."""
        rows, cols, xpx, ypx = struct.unpack(
            "HHHH", fcntl.ioctl(self.fd_out, termios.TIOCGWINSZ, b"\0" * 8))
        cols, rows = max(cols, 1), max(rows, 1)
        return cols, rows, (xpx / cols if xpx else 9.0), (ypx / rows if ypx else 19.0)

    def send(self, data):
        view = memoryview(data)
        while view:
            try:
                view = view[os.write(self.fd_out, view):]
            except InterruptedError:
                pass
            except BlockingIOError:
                select.select([], [self.fd_out], [], 1)

    def keys(self, timeout):
        """Keys pressed, waiting up to timeout seconds for the first."""
        try:
            ready = select.select([self.fd_in], [], [], timeout)[0]
        except InterruptedError:
            return []
        if ready:
            try:
                self.pending += os.read(self.fd_in, 4096)
            except (InterruptedError, BlockingIOError):
                pass
        keys, self.pending = parse_keys(self.pending)
        if self.pending:  # a lone Esc, or a sequence still arriving: give it a moment
            if select.select([self.fd_in], [], [], 0.03)[0]:
                self.pending += os.read(self.fd_in, 4096)
                more, self.pending = parse_keys(self.pending)
                keys += more
            if self.pending.startswith(b"\x1b") and len(self.pending) <= 2:
                keys.append("esc")
                self.pending = self.pending[1:]
        return keys


def sgr(fg=None, bg=None, bold=False):
    """The escape sequence for these colors (24-bit) and weight."""
    parts = ["0"]
    if bold:
        parts.append("1")
    if fg:
        parts.append("38;2;%d;%d;%d" % tuple(fg[:3]))
    if bg:
        parts.append("48;2;%d;%d;%d" % tuple(bg[:3]))
    return "\x1b[" + ";".join(parts) + "m"


def cut(text, width):
    """text cut to width columns (characters; the bars are plain text)."""
    return text if len(text) <= width else text[:max(0, width - 1)] + "…"


# --- menus ---------------------------------------------------------------------------------------

class Menu:
    """A list to pick from, drawn over the dashboard: the time ranges, the variables, a
    variable's values, the dashboards. Typing filters it. multi: Space ticks items, Enter
    takes the ticked ones."""

    def __init__(self, title, items, on_pick, multi=False, ticked=(), selected=0, note=""):
        self.title, self.items, self.on_pick, self.multi = title, items, on_pick, multi
        self.ticked = set(ticked)
        self.filter, self.note = "", note
        self.sel, self.top = selected, 0
        self.version = 0  # counts up each time it changes, so its picture is redrawn

    def shown(self):
        """(index, (label, value, detail)) for the items the filter lets through."""
        f = self.filter.lower()
        return [(i, it) for i, it in enumerate(self.items)
                if not f or f in it[0].lower() or f in str(it[2] or "").lower()]

    def key(self, k):
        """Act on a key -> "close", or None to stay open."""
        shown = self.shown()
        self.version += 1
        if k == "esc":
            return "close"
        if k in ("up", "down", "pgup", "pgdn", "home", "end"):
            step = {"up": -1, "down": 1, "pgup": -10, "pgdn": 10, "home": -10 ** 6, "end": 10 ** 6}[k]
            self.sel = max(0, min(len(shown) - 1, self.sel + step))
        elif k == " " and self.multi and shown:
            i = shown[min(self.sel, len(shown) - 1)][0]
            self.ticked ^= {i}
        elif k == "enter" and shown:
            i = shown[min(self.sel, len(shown) - 1)][0]
            if self.multi:
                picked = sorted(self.ticked) or [i]
                self.on_pick([self.items[j][1] for j in picked])
            else:
                self.on_pick(self.items[i][1])
            return "close"
        elif k == "backspace":
            self.filter, self.sel = self.filter[:-1], 0
        elif k == "ctrl-u":
            self.filter, self.sel = "", 0
        elif isinstance(k, str) and len(k) == 1 and k.isprintable():
            self.filter, self.sel = self.filter + k, 0
        return None

    def render(self, w, h, ch, theme):
        """The menu as a picture, w x h pixels, for cells ch pixels high."""
        S = 2
        u = ch * S / CSS_PER_CELL
        W, H = round(w * S), round(h * S)
        img = Image.new("RGB", (W, H), mix(theme.surface, (0, 0, 0), 0.25))
        d = ImageDraw.Draw(img, "RGBA")
        pad = 12 * u
        f_title, f_item, f_small = font(theme, 14 * u, True), font(theme, 13 * u), font(theme, 11 * u)
        d.text((pad, pad), self.title, font=f_title, fill=theme.ink)
        y = pad + f_title.size * 1.5
        hint = ("type to filter" if not self.filter else f"filter: {self.filter}") + \
            ("  ·  Space ticks, Enter takes them" if self.multi else "  ·  Enter picks, Esc closes")
        d.text((pad, y), hint, font=f_small, fill=theme.muted if not self.filter else theme.ink_2)
        y += f_small.size * 1.8
        if self.note:
            for ln in wrap(d, self.note, f_small, W - 2 * pad)[:3]:
                d.text((pad, y), ln, font=f_small, fill=theme.warning)
                y += f_small.size * 1.4
        lh = f_item.size * 1.55
        rows = max(1, int((H - y - pad) / lh))
        shown = self.shown()
        self.sel = max(0, min(self.sel, len(shown) - 1))
        if self.sel < self.top:
            self.top = self.sel
        elif self.sel >= self.top + rows:
            self.top = self.sel - rows + 1
        if not shown:
            d.text((pad, y), "nothing matches", font=f_item, fill=theme.muted)
        for n, (i, (label, value, detail)) in enumerate(shown[self.top:self.top + rows]):
            yy = y + n * lh
            if self.top + n == self.sel:
                d.rounded_rectangle((pad * 0.5, yy - lh * 0.12, W - pad * 0.5, yy + lh * 0.88),
                                    radius=6 * u, fill=theme.focus + (70,))
            x = pad
            if self.multi:
                box = f_item.size * 0.8
                by = yy + (lh * 0.76 - box) / 2
                d.rounded_rectangle((x, by, x + box, by + box), radius=2 * u,
                                    outline=theme.ink_2, width=max(1, round(1.5 * u)),
                                    fill=theme.focus if i in self.ticked else None)
                x += box + 8 * u
            d.text((x, yy), squeeze(d, label, f_item, W - x - pad - (W * 0.35 if detail else 0)),
                   font=f_item, fill=theme.ink)
            if detail:
                d.text((W - pad, yy + (f_item.size - f_small.size) / 2),
                       squeeze(d, str(detail), f_small, W * 0.33), font=f_small,
                       fill=theme.muted, anchor="ra")
        if len(shown) > rows:
            d.text((W - pad, pad), f"{self.sel + 1} of {len(shown)}", font=f_small,
                   fill=theme.muted, anchor="ra")
        return finish(img, round(w), round(h), u, theme, 250)


TIME_RANGES = [("Last 5 minutes", "now-5m"), ("Last 15 minutes", "now-15m"),
               ("Last 30 minutes", "now-30m"), ("Last 1 hour", "now-1h"),
               ("Last 3 hours", "now-3h"), ("Last 6 hours", "now-6h"),
               ("Last 12 hours", "now-12h"), ("Last 24 hours", "now-24h"),
               ("Last 2 days", "now-2d"), ("Last 7 days", "now-7d"),
               ("Last 30 days", "now-30d"), ("Last 90 days", "now-90d"),
               ("Today so far", "now/d"), ("This week so far", "now/w")]


# --- the dashboard, full screen -----------------------------------------------------------------

class Slot:
    """One panel on the screen, and everything about it that outlives a frame: its data,
    its picture, and the image it's showing as."""

    def __init__(self, n, item):
        self.n, self.item = n, item
        self.data = PanelData()
        self.data_version = 0      # counts up with each new result
        self.fetching = False
        self.fetched_at = None     # monotonic time of the last result
        self.picture = None        # (key, PNG bytes, (cols, rows)) once drawn
        self.rendering = None      # the key being drawn, if any
        self.shown = None          # the image id on screen, if any
        self.placed = None         # the placement last sent, to skip sending it again
        self.sent_key = None       # the picture last sent
        self.spot = None           # where it is now; None while it's hidden (a closed row,
                                   # or another panel full screen), keeping its data
        self.future = None         # its query, while one is in flight
        self.failed = None         # a drawing that failed, not to be tried again as is

    def ids(self):
        """The two image ids this panel swaps between: a new picture goes in under the
        other one and replaces the old in the same write, so it can't blink."""
        return IMAGE_ID + 2 * self.n, IMAGE_ID + 2 * self.n + 1


FOCUS_ID, MENU_ID, BACKDROP_ID = IMAGE_ID - 1, IMAGE_ID - 2, IMAGE_ID - 3


class App:
    """The dashboard, full screen, live. The main loop reads keys, takes in what the
    worker threads have finished (query results, drawn panels), starts more, and sends
    whatever changed on screen in one write."""

    def __init__(self, grafana, theme, args, target, overrides, frm, to, view):
        self.grafana, self.theme, self.args = grafana, theme, args
        self.target, self.overrides, self.frm, self.to = target, overrides, frm, to
        self.view_id = view
        self.density = args.density
        self.refresh_override = parse_duration(args.refresh) if args.refresh else None
        self.board = None
        self.problem = None         # why there's no dashboard to show, if there isn't
        self.slots, self.next_n = {}, 0
        self.order = []             # what Tab moves through: Spots, in reading order
        self.focus = None           # the focused Spot's key
        self.view = None            # a slot key: that panel full screen
        self.scroll = 0
        self.collapsed = {}         # section key -> collapsed, as the person left it
        self.menu = None
        self.menu_placed = None
        self.status, self.status_until = "", 0
        self.loading = None         # what's being loaded, for the status line
        self.results = queue.Queue()
        # Three pools: one for panels' queries, one for drawing, and one for the rest
        # (finding and loading dashboards, variables), so those never wait behind a
        # screenful of slow queries.
        self.fetchers = concurrent.futures.ThreadPoolExecutor(6)
        self.painters = concurrent.futures.ThreadPoolExecutor(4)
        self.control = concurrent.futures.ThreadPoolExecutor(2)
        self.epoch = 0              # counts up when the time range or variables change
        self.loads = 0              # counts up with each dashboard asked for
        self.want_resize = self.want_suspend = False  # set by signals, done by the loop
        self.size = None
        self.layout_dirty = True
        self.text_dirty = True
        self.quit = False
        self.ids_used = set()
        self.via_file = not (os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"))
        self.files = []
        self.focus_ring = None      # (size key, PNG) of the ring around the focused panel
        self.backdrop_sent = None
        self.last_refresh = None
        self.dashboards = None      # every dashboard, for the picker

    # running

    def run(self):
        self.screen = Screen()
        # The signal handlers only leave a note; the loop does the work, between writes,
        # so a resize or ^Z never lands in the middle of one.
        old = {s: signal.getsignal(s) for s in (signal.SIGWINCH, signal.SIGTSTP, signal.SIGTERM)}
        signal.signal(signal.SIGWINCH, lambda *_: setattr(self, "want_resize", True))
        signal.signal(signal.SIGTSTP, lambda *_: setattr(self, "want_suspend", True))
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "quit", True))
        self.screen.enter()
        try:
            self.size = self.screen.size()
            self.start()
            while not self.quit:
                for k in self.screen.keys(0.04):
                    self.on_key(k)
                if self.quit:
                    break
                if self.want_suspend:
                    self.suspend()
                if self.want_resize:
                    self.want_resize = False
                    self.resized()
                self.take_results()
                self.tick()
        except KeyboardInterrupt:
            pass
        finally:
            self.screen.leave(self.forget_all())
            for s, h in old.items():
                signal.signal(s, h)
            for pool in (self.fetchers, self.painters, self.control):
                pool.shutdown(wait=False, cancel_futures=True)
            for path, _ in self.files:
                try:
                    os.remove(path)
                except OSError:
                    pass
        if self.problem and not self.board:
            print(f"dashboard: {self.problem}", file=sys.stderr)
            return 1
        return 0

    def resized(self):
        self.layout_dirty = self.text_dirty = True
        self.size = self.screen.size()
        self.backdrop_sent = None

    def suspend(self):
        """^Z: give the terminal back and stop. When fg starts us again, take it back and
        send everything again (the terminal may have been reset meanwhile)."""
        self.want_suspend = False
        self.screen.leave(self.forget_all())
        os.kill(os.getpid(), signal.SIGSTOP)  # here until fg
        self.screen.enter()
        self.resized()

    def forget_all(self):
        """Commands that delete every image this has made, pixels and all; everything
        is sent again if it's needed again."""
        out = b"".join(apc(f"a=d,d=I,i={i},q=2") for i in sorted(self.ids_used))
        self.ids_used.clear()
        for s in self.slots.values():
            s.shown = s.placed = s.sent_key = None
        self.menu_placed = self.backdrop_sent = self.focus_ring = self.focus_placed = None
        return out

    def say(self, text, seconds=6):
        self.status, self.status_until = text, time.monotonic() + seconds
        self.text_dirty = True

    # loading a dashboard

    def start(self):
        if self.target:
            self.loading = f"looking for {self.target}…"
            self.control.submit(self.work, "found", lambda: find_dashboard(self.grafana, self.target))
        else:
            self.pick_dashboard()

    def work(self, kind, fn, *extra):
        """Run fn in a worker; its result (or error) comes back through the queue."""
        try:
            self.results.put((kind, extra, fn(), None))
        except Exception as e:
            if not isinstance(e, (RuntimeError, Unsupported)):
                traceback.print_exc(file=LOG)
            self.results.put((kind, extra, None, e))

    def load(self, uid):
        self.loading = "loading the dashboard…"
        self.text_dirty = True

        def fetch():
            doc, meta = self.grafana.dashboard(uid)
            board = Board(self.grafana, doc, meta, self.overrides, self.frm, self.to)
            board.prepare()
            return board

        self.loads += 1  # only the last dashboard asked for gets shown
        self.control.submit(self.work, "board", fetch, self.loads)

    def pick_dashboard(self, hits=None, note=""):
        def show(hits):
            items = [(h["title"], h["uid"], h["folder"]) for h in hits]
            self.menu = Menu("Dashboards", items, self.choose_dashboard, note=note)
            self.menu.filter = "" if hits is not None else ""
        if hits is not None:
            show(hits)
            return
        self.loading = "listing dashboards…"
        self.control.submit(self.work, "list", lambda: self.grafana.search(""))

    def choose_dashboard(self, uid):
        # A new dashboard starts from its own time range and variables.
        self.overrides, self.frm, self.to, self.view_id = {}, None, None, None
        self.load(uid)

    def take_results(self):
        while True:
            try:
                kind, extra, value, err = self.results.get_nowait()
            except queue.Empty:
                return
            if kind == "found":
                self.loading = None
                if err:
                    self.problem = str(err)
                    self.say(str(err), 30)
                    continue
                uid, hits = value
                if uid:
                    self.load(uid)
                elif hits:
                    self.pick_dashboard(hits, note=f"More than one dashboard matches "
                                                  f"“{self.target}”.")
                else:
                    self.say(f"nothing matches “{self.target}”", 30)
                    self.pick_dashboard()
            elif kind == "list":
                self.loading = None
                if err:
                    self.problem = str(err)
                    self.say(str(err), 30)
                else:
                    self.dashboards = value
                    self.pick_dashboard(value)
            elif kind == "board":
                if extra[0] != self.loads:
                    continue  # another dashboard was asked for since
                self.loading = None
                if err:
                    self.problem = str(err)
                    self.say(str(err), 30)
                    if not self.board:
                        self.pick_dashboard(self.dashboards, note=str(err)) if self.dashboards \
                            else None
                    continue
                self.set_board(value)
            elif kind == "vars":
                self.loading = None
                if err:
                    self.say(f"couldn't update the variables: {err}")
                self.board.relayout()
                self.new_epoch()
            elif kind == "data":
                # A panel has one query in flight at most; a result from before the time
                # range or variables changed is dropped, and the panel is fetched again.
                key, epoch = extra
                slot = self.slots.get(key)
                if slot:
                    slot.fetching, slot.future = False, None
                if slot and epoch == self.epoch:
                    slot.data, slot.fetched_at = value or PanelData(error=str(err), updated=time.time()), \
                        time.monotonic()
                    slot.data_version += 1
            elif kind == "picture":
                key, pkey = extra
                slot = self.slots.get(key)
                if slot and slot.rendering == pkey:
                    slot.rendering = None
                    if value:
                        slot.picture = (pkey, value[0], value[1])
                    else:  # don't try the same drawing again until something changes
                        slot.failed = pkey
                        LOG.write(f"drawing {key} failed: {err}\n")

    def set_board(self, board):
        for s in self.slots.values():
            self.drop(s, forget=True)
        self.slots, self.board, self.problem = {}, board, None
        self.collapsed, self.scroll, self.focus, self.view = {}, 0, None, None
        self.last_refresh = time.monotonic()
        self.layout_dirty = self.text_dirty = True
        self.epoch += 1

    def new_epoch(self):
        """The time range or the variables changed: everything is fetched again. Queries
        not started yet are called off; ones already running finish, and are dropped."""
        self.epoch += 1
        for s in self.slots.values():
            s.fetched_at = None
            if s.future is not None and s.future.cancel():
                s.fetching, s.future = False, None
        self.last_refresh = time.monotonic()
        self.layout_dirty = self.text_dirty = True

    # keys

    def on_key(self, k):
        if self.menu:
            if isinstance(k, tuple):
                return self.menu_mouse(k)
            menu = self.menu
            if menu.key(k) == "close" and self.menu is menu:  # (picking may open another)
                self.menu = None
            self.text_dirty = True
            return
        if isinstance(k, tuple):
            return self.mouse(k)
        if k in ("q", "Q"):
            self.quit = True
        elif k in ("down", "j"):
            self.scroll_by(1 if k == "down" else 3)
        elif k in ("up", "k"):
            self.scroll_by(-1 if k == "up" else -3)
        elif k in ("pgdn", " "):
            self.scroll_by(self.body_rows() - 2)
        elif k == "pgup":
            self.scroll_by(-(self.body_rows() - 2))
        elif k in ("home", "g"):
            self.scroll_by(-10 ** 6)
        elif k in ("end", "G"):
            self.scroll_by(10 ** 6)
        elif k in ("tab", "right", "l"):
            self.move_focus(1)
        elif k in ("shift-tab", "left", "h"):
            self.move_focus(-1)
        elif k == "enter":
            self.activate()
        elif k == "esc":
            if self.view:
                self.view = None
                self.layout_dirty = self.text_dirty = True
            elif self.focus:
                self.focus = None
                self.text_dirty = True
        elif k == "r":
            if self.board:
                self.new_epoch()
                self.say("refreshing…", 2)
        elif k == "t":
            self.time_menu()
        elif k == "v":
            self.vars_menu()
        elif k == "d":
            if self.dashboards is None:
                self.pick_dashboard()
            else:
                self.pick_dashboard(self.dashboards)
        elif k == "?":
            items = [(ln.strip(), None, "") for ln in KEYS_HELP.splitlines()[1:] if ln.strip()]
            self.menu = Menu("Keys", items, lambda v: None)

    def mouse(self, ev):
        _, button, col, row, pressed = ev
        if button == 64:
            self.scroll_by(-3)
        elif button == 65:
            self.scroll_by(3)
        elif button == 0 and pressed:
            spot = self.spot_at(col, row)
            if spot is None:
                return
            key = self.spot_key(spot)
            if spot.section is not None or self.focus == key or self.view:
                self.focus = key
                self.activate()
            else:
                self.focus = key
            self.text_dirty = True

    def menu_mouse(self, ev):
        _, button, col, row, pressed = ev
        if button in (64, 65):
            self.menu.key("up" if button == 64 else "down")
            self.text_dirty = True
        elif button == 0 and pressed:
            self.menu = None  # a click outside, or anywhere: close (keys pick)
            self.text_dirty = True

    def scroll_by(self, n):
        top = max(0, min(self.max_scroll(), self.scroll + n))
        if top != self.scroll:
            self.scroll = top
            self.text_dirty = True

    def move_focus(self, step):
        if not self.order or self.view:
            return
        keys = [self.spot_key(s) for s in self.order]
        if self.focus in keys:
            i = (keys.index(self.focus) + step) % len(keys)
        else:  # start from what's on screen
            visible = [i for i, s in enumerate(self.order) if self.row_of(s) >= self.scroll]
            i = visible[0] if visible and step > 0 else (visible[-1] if visible else 0)
        self.focus = keys[i]
        self.reveal(self.order[i])
        self.text_dirty = True

    def reveal(self, spot):
        """Scroll so the spot is on screen."""
        cols, rows, cw, ch = self.size or self.screen.size()
        r0, r1 = int(spot.y // ch), int(math.ceil((spot.y + spot.h) / ch))
        body = self.body_rows()
        if r0 < self.scroll:
            self.scroll = r0
        elif r1 > self.scroll + body:
            self.scroll = min(r0, r1 - body)
        self.scroll = max(0, min(self.max_scroll(), self.scroll))

    def activate(self):
        """Enter: a panel goes full screen (or comes back); a row opens or closes."""
        if self.view:
            self.view = None
            self.layout_dirty = self.text_dirty = True
            return
        spot = next((s for s in self.order if self.spot_key(s) == self.focus), None)
        if spot is None:
            return
        if spot.section is not None:
            sec = spot.section
            self.collapsed[sec.key] = not self.is_collapsed(sec)
            self.layout_dirty = self.text_dirty = True
        elif spot.item is not None:
            self.view = self.spot_key(spot)
            self.layout_dirty = self.text_dirty = True

    def time_menu(self):
        if not self.board:
            return
        items = [(label, frm, "") for label, frm in TIME_RANGES]
        t = self.board.doc.get("time") or {}
        if t.get("from"):
            items.insert(0, (f"The dashboard's: {human_range(t['from'], t.get('to', 'now'))}",
                             (t["from"], t.get("to", "now")), ""))
        current = next((i for i, it in enumerate(items) if it[1] in (self.board.frm, (self.board.frm, self.board.to))), 0)

        def pick(value):
            frm, to = value if isinstance(value, tuple) else (value, "now")
            self.board.frm, self.board.to = frm, to
            self.refresh_vars_for_time()

        self.menu = Menu("Time range", items, pick, selected=current)

    def refresh_vars_for_time(self):
        start, end = self.board.time_range()
        self.loading = "updating…"
        self.control.submit(self.work, "vars", lambda: self.board.variables.refresh_for_time(start, end))

    def vars_menu(self):
        if not self.board:
            return
        vs = [v for v in self.board.variables.vars if v.hide < 2 and v.type not in ("constant", "adhoc")]
        if not vs:
            self.say("this dashboard has no variables to change")
            return
        items = [(v.label, v.name, ("! " + v.error) if v.error else ", ".join(v.texts()) or "—")
                 for v in vs]
        self.menu = Menu("Variables", items, self.var_values_menu)

    def var_values_menu(self, name):
        var = self.board.variables.get(name)
        if var is None:
            return
        if var.type == "textbox":
            self.say("a textbox variable: set it with --var or in the link", 5)
            return
        items = [(o["text"], o["value"], "") for o in var.options]
        ticked = [i for i, o in enumerate(var.options) if o["value"] in var.values]

        def pick(values):
            start, end = self.board.time_range()
            self.loading = "updating…"
            self.control.submit(self.work, "vars",
                                 lambda: self.board.variables.set(name, values, start, end))

        self.menu = Menu(var.label, items, pick, multi=var.multi, ticked=ticked if var.multi else (),
                         selected=ticked[0] if ticked else 0)
        self.text_dirty = True

    # layout

    def bars(self):
        """Rows the bars take: the title bar, the variables bar (if any), the key bar."""
        has_vars = bool(self.board and any(v.hide < 2 and v.type not in ("constant", "adhoc")
                                           for v in self.board.variables.vars))
        return (2 if has_vars else 1), 1

    def body_rows(self):
        rows = (self.size or self.screen.size())[1]
        top, bottom = self.bars()
        return max(1, rows - top - bottom)

    def max_scroll(self):
        return max(0, getattr(self, "total_rows", 0) - self.body_rows())

    def is_collapsed(self, sec):
        return self.collapsed.get(sec.key, sec.collapsed)

    def spot_key(self, spot):
        return ("row:" + spot.section.key) if spot.section is not None else spot.item.key

    def row_of(self, spot):
        ch = (self.size or self.screen.size())[3]
        return int(spot.y // ch)

    def spot_at(self, col, row):
        """The panel or row title at a cell on the screen, if any."""
        if not self.size or self.view:
            return None
        cols, rows, cw, ch = self.size
        r = row - self.bars()[0] + self.scroll
        if not 0 <= row - self.bars()[0] < self.body_rows():
            return None
        x, y = (col + 0.5) * cw, (r + 0.5) * ch
        for spot in self.order:
            if spot.section is not None:
                if int(spot.y // ch) == r:
                    return spot
            elif spot.x <= x < spot.x + spot.w and spot.y <= y < spot.y + spot.h:
                return spot
        return None

    def relayout(self):
        """Lay the dashboard out for the window as it is now."""
        self.size = self.screen.size()
        cols, rows, cw, ch = self.size
        self.layout_dirty = False
        self.text_dirty = True
        if not self.board:
            self.order, self.total_rows = [], 0
            return
        for sec in self.board.sections:
            sec.collapsed = self.is_collapsed(sec)
        spots, total = place(self.board.sections, cols * cw, cw, ch)
        if self.view and not any(s.item and s.item.key == self.view for s in spots):
            self.view = None
        if self.view:
            item = next(s.item for s in spots if s.item and s.item.key == self.view)
            gap = GRID_GAP * ch / CSS_PER_CELL
            spots = [Spot(gap, gap, cols * cw - 2 * gap, self.body_rows() * ch - 2 * gap, item=item)]
            total = self.body_rows()
            self.scroll = 0
        self.total_rows = total
        self.order = spots if not self.view else []
        self.scroll = max(0, min(self.max_scroll(), self.scroll))
        shown = set()
        for spot in spots:
            if spot.item is None:
                continue
            key = spot.item.key
            slot = self.slots.get(key)
            if slot is None:
                slot = self.slots[key] = Slot(self.next_n, spot.item)
                self.next_n += 1
            slot.item, slot.spot = spot.item, spot
            shown.add(key)
        # A panel that's only hidden (in a closed row, or behind one full screen) keeps
        # its data and picture; one the dashboard no longer has is forgotten.
        exists = {it.key for sec in self.board.sections for it in sec.items}
        for key in [k for k in self.slots if k not in shown]:
            if key in exists:
                if self.slots[key].spot is not None:
                    self.drop(self.slots[key], forget=True)
                    self.slots[key].spot = None
            else:
                self.drop(self.slots.pop(key), forget=True)
        if self.view_id and not self.view:  # viewPanel= in the link
            for s in spots:
                if s.item and str(s.item.panel.get("id")) == re.sub(r"^panel-", "", str(self.view_id)):
                    self.view_id, self.view = None, s.item.key
                    self.layout_dirty = True

    def drop(self, slot, forget=False):
        """Take a panel off the screen (and, forget: out of the terminal's memory)."""
        if forget:
            self.pending_out = getattr(self, "pending_out", b"") + b"".join(
                apc(f"a=d,d=I,i={i},q=2") for i in slot.ids() if i in self.ids_used)
            self.ids_used -= set(slot.ids())
        elif slot.shown:
            self.pending_out = getattr(self, "pending_out", b"") + apc(f"a=d,d=i,i={slot.shown},q=2")
        slot.shown = slot.placed = None

    # each turn of the loop

    def refresh_every(self):
        return self.refresh_override or (self.board.refresh_every() if self.board else None) \
            or DEFAULT_REFRESH

    def tick(self):
        if self.size is None or self.layout_dirty:
            self.relayout()
        out = getattr(self, "pending_out", b"")
        self.pending_out = b""
        if self.files:
            self.check_files()
        out += self.backdrop()
        if self.board:
            self.schedule()
            out += self.place_panels()
        out += self.place_menu()
        if self.text_dirty:
            out += self.draw_text()
        if out:
            self.screen.send(out)

    def visible_rows(self):
        return self.scroll, self.scroll + self.body_rows()

    def cells(self, spot):
        """(col0, row0, cols, rows) the spot's picture covers, rows counted in the layout."""
        cols, rows, cw, ch = self.size
        c0, r0 = int(spot.x // cw), int(spot.y // ch)
        c1, r1 = int(math.ceil((spot.x + spot.w) / cw - 1e-6)), int(math.ceil((spot.y + spot.h) / ch - 1e-6))
        return c0, r0, max(1, min(cols, c1) - c0), max(1, r1 - r0)

    def schedule(self):
        """Start fetching panels on (or near) the screen that are due, and drawing the
        ones whose picture is out of date."""
        top, bottom = self.visible_rows()
        near = self.body_rows()
        every = self.refresh_every()
        now = time.monotonic()
        busy = sum(1 for s in self.slots.values() if s.fetching)
        cols, rows, cw, ch = self.size
        for key, slot in self.slots.items():
            if slot.spot is None:
                continue
            c0, r0, nc, nr = self.cells(slot.spot)
            on_screen = r0 < bottom and r0 + nr > top
            if not (r0 < bottom + near and r0 + nr > top - near):
                continue
            if not slot.fetching and busy < 12 and slot.item.panel.get("targets") is not None and (
                    slot.fetched_at is None or now - slot.fetched_at >= every):
                slot.fetching, busy = True, busy + 1
                slot.future = self.fetchers.submit(self.fetch, key, slot.item,
                                                   slot.spot.w * CSS_PER_CELL / ch, self.epoch)
            elif slot.item.panel.get("targets") is None and slot.data.updated is None:
                slot.data = PanelData(updated=time.time())
                slot.data_version += 1
            if not on_screen:
                continue
            sp = slot.spot
            pkey = (slot.data_version, (nc, nr), (round(cw, 2), round(ch, 2)), self.density,
                    round(sp.x - c0 * cw), round(sp.y - r0 * ch), round(sp.w), round(sp.h),
                    int(time.time() // 60) if slot.data.frames else 0)
            if slot.rendering is None and slot.failed != pkey and (
                    slot.picture is None or slot.picture[0] != pkey):
                slot.rendering = pkey
                self.painters.submit(self.paint, key, pkey, slot.item, slot.data, slot.spot,
                                     (c0, r0, nc, nr), self.size)

    def fetch(self, key, item, width_css, epoch):
        try:
            data = self.board.fetch(item, width_css)
            self.results.put(("data", (key, epoch), data, None))
        except Exception as e:
            traceback.print_exc(file=LOG)
            self.results.put(("data", (key, epoch), None, e))

    def paint(self, key, pkey, item, data, spot, cells, size):
        """Draw a panel's picture, in a worker: the panel where it sits within the cells
        it covers, the rest of them transparent."""
        try:
            cols, rows, cw, ch = size
            c0, r0, nc, nr = cells
            k = self.density
            start, end = panel_range(item.panel, *self.board.time_range(), time.time())
            img = render_panel(item, data, spot.w * k, spot.h * k, ch * k, self.theme, start, end,
                               time.time(), self.board.title_of(item), self.board.interp_for(item))
            canvas = Image.new("RGBA", (max(1, round(nc * cw * k)), max(1, round(nr * ch * k))), (0, 0, 0, 0))
            canvas.paste(img, (round((spot.x - c0 * cw) * k), round((spot.y - r0 * ch) * k)))
            self.results.put(("picture", (key, pkey), (png(canvas, small=not self.via_file), (nc, nr)), None))
        except Exception as e:
            traceback.print_exc(file=LOG)
            self.results.put(("picture", (key, pkey), None, e))

    def transmit(self, image_id, data):
        """The command that sends a picture: by temporary file the terminal reads and
        deletes, or failing that (over SSH), in the command itself."""
        self.ids_used.add(image_id)
        if self.via_file:
            try:
                fd, path = tempfile.mkstemp(prefix="tty-graphics-protocol-dashboard-", suffix=".png")
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                self.files.append((path, time.monotonic()))
                return apc(f"a=t,f=100,t=t,i={image_id},q=2", base64.standard_b64encode(path.encode()))
            except OSError:
                self.via_file = False
        return chunked(f"a=t,f=100,i={image_id},q=2", data)

    def check_files(self):
        """A terminal that can read our files deletes each one as it takes it; one still
        there after a few seconds means it can't (it's on another machine, say). From
        then on, the pixels go in the commands, and every panel is sent again."""
        now = time.monotonic()
        if any(now - t > 5 and os.path.exists(p) for p, t in self.files):
            self.via_file = False
            for p, _ in self.files:
                try:
                    os.remove(p)
                except OSError:
                    pass
            self.files = []
            # Delete what went before (if any of it arrived), and send it all again.
            self.pending_out = getattr(self, "pending_out", b"") + self.forget_all()
            for s in self.slots.values():
                s.picture = None  # drawn again, as the smaller kind
            self.say("pictures now go through the terminal itself (it can't read our files)")
            return
        self.files = [(p, t) for p, t in self.files if now - t <= 5]

    def place_panels(self):
        """Commands that put each panel's picture where it belongs now: a new picture
        goes in under the panel's other image id and replaces the old one in the same
        write; a panel that moved is re-placed; one that left the screen is taken off."""
        cols, rows, cw, ch = self.size
        top_rows = self.bars()[0]
        top, bottom = self.visible_rows()
        out = []
        for key, slot in self.slots.items():
            if slot.picture is None or slot.spot is None:
                continue
            c0, r0, nc, nr = self.cells(slot.spot)
            pc, pr = slot.picture[2]
            v0, v1 = max(r0, top), min(r0 + pr, bottom)
            if v1 <= v0:
                if slot.shown:
                    out.append(apc(f"a=d,d=i,i={slot.shown},q=2"))
                    slot.shown = slot.placed = None
                continue
            k = self.density
            hpx = round(pr * ch * k)
            y = round((v0 - r0) * ch * k)
            h = min(hpx - y, round((v1 - v0) * ch * k))
            new = slot.shown is None or slot.picture[0] != getattr(slot, "sent_key", None)
            image_id = slot.shown or slot.ids()[0]
            if new:
                a, b = slot.ids()
                image_id = b if slot.shown == a else a
            placement = (image_id, c0, top_rows + v0 - top, y, h, pc, v1 - v0)
            if not new and placement == slot.placed:
                continue
            body = b""
            if new:
                body += self.transmit(image_id, slot.picture[1])
                slot.sent_key = slot.picture[0]
            z = 1
            body += apc(f"a=p,i={image_id},p=1,x=0,y={y},h={max(1, h)},c={pc},r={v1 - v0},z={z},C=1,q=2")
            if slot.shown and slot.shown != image_id:
                body += apc(f"a=d,d=I,i={slot.shown},q=2")
                self.ids_used.discard(slot.shown)
            out.append(f"\x1b[{top_rows + v0 - top + 1};{c0 + 1}H".encode() + body)
            slot.shown, slot.placed = image_id, placement
        # The ring around the focused panel.
        out.append(self.place_focus())
        return b"".join(out)

    def place_focus(self):
        cols, rows, cw, ch = self.size
        slot = self.slots.get(self.focus) if self.focus and not self.view else None
        if slot is None or not slot.placed:
            if getattr(self, "focus_placed", None):
                self.focus_placed = None
                return apc(f"a=d,d=i,i={FOCUS_ID},q=2")
            return b""
        c0, r0, nc, nr = self.cells(slot.spot)
        k = self.density
        key = (nc, nr, round(slot.spot.w), round(slot.spot.h), round(slot.spot.x - c0 * cw),
               round(slot.spot.y - r0 * ch), k)
        out = b""
        if not self.focus_ring or self.focus_ring[0] != key:
            S = 2
            W, H = round(nc * cw * k * S), round(nr * ch * k * S)
            ring = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            u = ch * k * S / CSS_PER_CELL
            x0, y0 = (slot.spot.x - c0 * cw) * k * S, (slot.spot.y - r0 * ch) * k * S
            ImageDraw.Draw(ring).rounded_rectangle(
                (x0, y0, x0 + slot.spot.w * k * S - 1, y0 + slot.spot.h * k * S - 1),
                radius=self.theme.radius * u, outline=self.theme.focus + (255,), width=max(2, round(2 * u)))
            ring = ring.resize((max(1, W // S), max(1, H // S)), Image.Resampling.LANCZOS)
            self.focus_ring = (key, png(ring, small=not self.via_file))
            out += self.transmit(FOCUS_ID, self.focus_ring[1])
            self.focus_placed = None
        image_id, _, row, y, h, pc, pr = slot.placed
        placement = (key, row, y, h, pc, pr, c0)
        if placement != getattr(self, "focus_placed", None):
            out += f"\x1b[{row + 1};{c0 + 1}H".encode() + apc(
                f"a=p,i={FOCUS_ID},p=1,x=0,y={y},h={max(1, h)},c={pc},r={pr},z=2,C=1,q=2")
            self.focus_placed = placement
        return out

    def place_menu(self):
        cols, rows, cw, ch = self.size
        if not self.menu:
            if self.menu_placed:
                self.menu_placed = None
                return apc(f"a=d,d=i,i={MENU_ID},q=2")
            return b""
        top_rows = self.bars()[0]
        mc = max(30, min(cols - 4, int(cols * 0.5)))
        mr = max(6, min(rows - top_rows - 2, len(self.menu.items) + 6))
        key = (id(self.menu), self.menu.version, mc, mr, cw, ch, self.menu.filter)
        if self.menu_placed == key:
            return b""
        k = self.density
        img = self.menu.render(mc * cw * k, mr * ch * k, ch * k, self.theme)
        col = (cols - mc) // 2
        self.menu_placed = key
        return f"\x1b[{top_rows + 1};{col + 1}H".encode() + self.transmit(MENU_ID, png(img, not self.via_file)) + \
            apc(f"a=p,i={MENU_ID},p=1,c={mc},r={mr},z=5,C=1,q=2")

    def backdrop(self):
        """The theme's picture behind everything, if it has one."""
        cols, rows, cw, ch = self.size
        key = (cols, rows, cw, ch)
        if self.backdrop_sent == key:
            return b""
        self.backdrop_sent = key
        img = self.theme.backdrop(round(cols * cw), round(rows * ch))
        if img is None:
            return b""
        return b"\x1b[1;1H" + self.transmit(BACKDROP_ID, png(img, not self.via_file)) + \
            apc(f"a=p,i={BACKDROP_ID},p=1,c={cols},r={rows},z=-1073741825,C=1,q=2")

    # the text: the bars, row titles, and messages

    def draw_text(self):
        self.text_dirty = False
        cols, rows, cw, ch = self.size
        th = self.theme
        top_rows, _ = self.bars()
        lines = {}
        bar = sgr(th.ink, th.surface, True)
        dim = sgr(th.muted, th.surface)
        val = sgr(th.ink_2, th.surface)
        # The title bar: the dashboard, the time range, how fresh it is.
        if self.board:
            b = self.board
            left = f" {b.title} "
            right = f"{human_range(b.frm, b.to)}  ·  every {duration_text(self.refresh_every())}  ·  via {self.grafana.via} "
            if self.view:
                slot = self.slots.get(self.view)
                if slot:
                    left += f"› {b.title_of(slot.item) or 'panel'} "
            space = cols - len(left) - len(right)
            lines[0] = bar + cut(left, cols) + (dim + " " * space + right if space > 0 else
                                                 dim + " " * max(0, cols - len(left)))
        else:
            lines[0] = bar + cut(" dashboard.py ", cols).ljust(cols)
        # The variables bar.
        if self.board and top_rows > 1:
            parts = []
            for v in self.board.variables.vars:
                if v.hide >= 2 or v.type in ("constant", "adhoc"):
                    continue
                shown = "All" if v.is_all() else ", ".join(v.texts()) or "—"
                mark = sgr(th.warning, th.surface) + " !" if v.error else ""
                parts.append(dim + (v.label + " " if v.hide < 1 else "") + val + cut(shown, 30) + mark)
            text = " " + (dim + "   ").join(parts)
            lines[1] = text + dim + " " * cols  # padding; the line is cut at the edge (no wrap)
        # The key bar, or a message.
        now = time.monotonic()
        if self.loading:
            msg = sgr(th.ink_2, th.surface) + " " + self.loading
        elif self.status and now < self.status_until:
            msg = sgr(th.warning, th.surface) + " " + cut(self.status, cols - 2)
        elif self.menu:
            msg = dim + " ↑↓ choose  ·  Enter pick  ·  Esc close  ·  type to filter"
        elif self.view:
            msg = dim + " Esc or Enter back  ·  r refresh  ·  t time  ·  v variables  ·  q quit"
        else:
            top, bottom = self.visible_rows()
            pending = sum(1 for s in self.slots.values() if s.data.updated is None and s.spot
                          and self.cells(s.spot)[1] < bottom
                          and sum(self.cells(s.spot)[1::2]) > top)
            msg = dim + " ↑↓ scroll  ·  Tab panel  ·  Enter open  ·  t time  ·  v variables  ·  " \
                        "r refresh  ·  d dashboards  ·  ? keys  ·  q quit"
            if pending:
                msg += f"   ({pending} loading)"
        lines[rows - 1] = msg + " " * cols
        # Row titles in the body; everything else blank (panels cover it).
        body_rows = self.body_rows()
        if self.board and not self.view:
            for spot in self.order:
                if spot.section is None:
                    continue
                r = int(spot.y // ch) - self.scroll
                if 0 <= r < body_rows:
                    focused = self.focus == self.spot_key(spot)
                    arrow = "▸" if self.is_collapsed(spot.section) else "▾"
                    title = self.board.row_title(spot.section) or "Row"
                    n = len(spot.section.items)
                    extra = f"  ({n} panel{'s' if n != 1 else ''})" if self.is_collapsed(spot.section) else ""
                    style = sgr(th.ink, mix(th.surface, th.focus, 0.35), True) if focused else sgr(th.ink, None, True)
                    lines[top_rows + r] = " " + style + f"{arrow} {title}" + sgr(th.muted) + extra
        if not self.board and self.problem and not self.menu:
            lines[top_rows + 1] = " " + sgr(th.warning) + cut(self.problem, cols - 2)
            lines[top_rows + 2] = " " + sgr(th.muted) + "d: pick another dashboard  ·  q: quit"
        out = []
        for r in range(rows):
            out.append(f"\x1b[{r + 1};1H\x1b[0m" + lines.get(r, "") + "\x1b[0m\x1b[K")
        return "".join(out).encode()


def duration_text(seconds):
    s = int(seconds)
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n and s % n == 0:
            return f"{s // n}{unit}"
    return f"{s}s"


# --- finding the dashboard -------------------------------------------------------------------

def find_dashboard(grafana, target):
    """(uid, or None, [search hits]) for what was asked for: a uid, or a title (or part
    of one). An exact title, or a single hit, wins; otherwise the hits are to pick from."""
    if target and re.fullmatch(r"[\w.-]{1,40}", target):
        try:
            grafana.dashboard(target)
            return target, []
        except Unsupported:
            return target, []
        except RuntimeError as e:
            if not re.search(r"no dashboard|404|not found", str(e), re.I):
                raise
    hits = grafana.search(target or "")
    exact = [h for h in hits if h["title"].lower() == (target or "").lower()]
    if len(exact) == 1:
        return exact[0]["uid"], hits
    if len(hits) == 1 and target:
        return hits[0]["uid"], hits
    return None, hits


def parse_vars(pairs):
    """--var name=value (repeatable; a name given twice gets both values)."""
    out = {}
    for p in pairs or []:
        name, _, value = p.partition("=")
        out.setdefault(name.strip(), []).append(value)
    return out


def main():
    ap = argparse.ArgumentParser(
        description="Show a Grafana dashboard in your terminal, full screen.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=KEYS_HELP)
    ap.add_argument("dashboard", nargs="?", default="",
                    help="its title (or part of it), its uid, or its link from your browser")
    ap.add_argument("--var", action="append", metavar="NAME=VALUE",
                    help="set a variable (again for more values; $__all for All)")
    ap.add_argument("--from", dest="frm", metavar="TIME", help="start of the time range, like now-6h")
    ap.add_argument("--to", metavar="TIME", help="end of the time range (default: now)")
    ap.add_argument("--refresh", metavar="DURATION",
                    help="how often to fetch again (default: the dashboard's, or 30s)")
    ap.add_argument("--theme", choices=sorted(THEMES), default="graph", help="the look (default graph)")
    ap.add_argument("--grafana", metavar="URL", help="Grafana's URL (default: $GRAFANA_URL)")
    ap.add_argument("--mcp", metavar="COMMAND|URL", help="through a Grafana MCP server "
                    "(default: $GRAFANA_MCP_COMMAND)")
    ap.add_argument("--gcx", action="store_true", help="through the gcx CLI")
    ap.add_argument("--gcx-context", metavar="NAME", help="gcx's context to use")
    ap.add_argument("--density", type=float, default=2.0,
                    help="pixels drawn per pixel the terminal reports (default 2, sharp on Retina)")
    ap.add_argument("--snapshot", metavar="PNG", help="draw the dashboard once into a picture and exit")
    ap.add_argument("--width", type=int, default=1600,
                    help="with --snapshot: the width to lay it out for, in pixels (default 1600)")
    ap.add_argument("--cell", default="9x19", help="with --snapshot: a cell's size, WxH pixels")
    ap.add_argument("--log", metavar="FILE", help="write tracebacks here (for bugs)")
    args = ap.parse_args()
    if args.log:
        LOG.file = open(args.log, "a")

    link = read_link(args.dashboard)
    overrides = parse_vars(args.var)
    frm, to, view = args.frm, args.to, None
    target = args.dashboard
    if link:
        target, base, link_vars, link_from, link_to, view = link
        overrides = dict(link_vars, **overrides)
        frm, to = frm or link_from, to or link_to
    grafana = connect(args, link[1] if link else None)
    theme = THEMES[args.theme]()
    try:
        if args.snapshot:
            uid, hits = find_dashboard(grafana, target)
            if not uid:
                if not hits:
                    sys.exit(f"dashboard: nothing matches {target!r}")
                sys.exit("dashboard: more than one matches; pick one by uid:\n" + "\n".join(
                    f"  {h['uid']:<40} {h['title']}" + (f"  ({h['folder']})" if h["folder"] else "")
                    for h in hits[:30]))
            try:
                doc, meta = grafana.dashboard(uid)
                board = Board(grafana, doc, meta, overrides, frm, to)
            except Unsupported as e:
                sys.exit(f"dashboard: {e}")
            cw, ch = (float(x) for x in args.cell.split("x"))
            snapshot(board, args.snapshot, args.width, cw, ch, args.density, theme)
            return
        code = App(grafana, theme, args, target, overrides, frm, to, view).run()
        # Queries still running in worker threads would hold up the exit until they
        # time out (Python waits for its threads). The terminal is back; go now.
        grafana.close()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)
    except RuntimeError as e:
        sys.exit(f"dashboard: {e}")
    finally:
        grafana.close()


KEYS_HELP = """keys:
  ↑ ↓ PgUp PgDn, the mouse wheel   scroll
  Tab / Shift-Tab                   the next / previous panel
  Enter, or a click                 a panel full screen (Enter or Esc again: back);
                                    on a row's title, open or close the row
  t   the time range                v   the variables
  r   refresh now                   d   another dashboard
  ?   these keys                    q   quit
"""


if __name__ == "__main__":
    main()
