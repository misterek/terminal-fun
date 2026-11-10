#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["numpy", "Pillow>=10.1"]
# ///
"""Quick check that dashboard.py behaves. It runs against a stand-in Grafana (in this
process) and a fake terminal, so nothing is drawn on your screen and no real Grafana
is asked anything.

    ./check.py        (or: uv run check.py)

It checks that --snapshot draws a dashboard: its query variable filled in from Grafana,
a panel repeated for each of the variable's values, and a panel type it doesn't know
left as an empty frame. That a dashboard's link brings its variables and time range
along. That a dashboard in Grafana's newer (V2) format is turned down with a reason.
And that full screen takes the terminal and gives it back: the alternate screen, every
graphics command quiet (q=2), and every picture deleted on the way out.
"""

import fcntl
import json
import math
import os
import pty
import re
import select
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image

SCRIPT = Path(__file__).resolve().parent / "dashboard.py"
SIZE = (40, 160, 1440, 760)  # rows, cols, width px, height px

DASHBOARD = {
    "uid": "fake", "title": "Fake board", "time": {"from": "now-6h", "to": "now"},
    "templating": {"list": [
        {"name": "job", "type": "query", "datasource": {"uid": "prom1"}, "multi": True,
         "includeAll": True, "current": {"text": "All", "value": "$__all"},
         "query": {"query": "label_values(up, job)", "refId": "A"}},
    ]},
    "panels": [
        {"id": 1, "type": "timeseries", "title": "Load", "gridPos": {"x": 0, "y": 0, "w": 16, "h": 8},
         "datasource": {"uid": "prom1"}, "targets": [{"refId": "A", "expr": 'load{job=~"$job"}'}]},
        {"id": 2, "type": "text", "title": "Notes", "gridPos": {"x": 16, "y": 0, "w": 8, "h": 8},
         "options": {"mode": "markdown", "content": "# Jobs\n$job"}},
        {"id": 3, "type": "row", "title": "Per job", "collapsed": False,
         "gridPos": {"x": 0, "y": 8, "w": 24, "h": 1}},
        {"id": 4, "type": "timeseries", "title": "Up: $job", "repeat": "job", "maxPerRow": 2,
         "gridPos": {"x": 0, "y": 9, "w": 12, "h": 6}, "datasource": {"uid": "prom1"},
         "targets": [{"refId": "A", "expr": 'up{job="$job"}'}]},
        {"id": 5, "type": "some-plugin-panel", "title": "A plugin",
         "gridPos": {"x": 0, "y": 15, "w": 24, "h": 5}},
    ],
}
V2 = {"title": "New layout", "elements": {}, "layout": {"kind": "TabsLayout", "spec": {}}}


class Grafana(BaseHTTPRequestHandler):
    """Just enough of Grafana's API for dashboard.py. It keeps the queries it's sent."""

    queries = []

    def log_message(self, *args):
        pass

    def answer(self, doc, code=200):
        body = json.dumps(doc).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/frontend/settings":
            return self.answer({"defaultDatasource": "prom", "datasources": {"prom": {
                "uid": "prom1", "name": "prom", "type": "prometheus", "isDefault": True,
                "jsonData": {"timeInterval": "15s"}}}})
        if path == "/api/search":
            return self.answer([{"uid": "fake", "title": "Fake board", "folderTitle": ""},
                                {"uid": "v2", "title": "New layout", "folderTitle": ""}])
        if path == "/api/dashboards/uid/fake":
            return self.answer({"dashboard": DASHBOARD, "meta": {}})
        if path == "/api/dashboards/uid/v2":
            return self.answer({"dashboard": V2, "meta": {}})
        if re.fullmatch(r"/api/datasources/uid/prom1/resources/api/v1/label/job/values", path):
            return self.answer({"status": "success", "data": ["api", "web"]})
        self.answer({"message": "Not found"}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path != "/api/ds/query":
            return self.answer({"message": "Not found"}, 404)
        start, end = int(body["from"]) // 1000, int(body["to"]) // 1000
        results = {}
        for q in body["queries"]:
            Grafana.queries.append((q.get("expr"), start, end))
            times = list(range(start, end, max(60, (end - start) // 120)))
            values = [3 + math.sin(t / 900) for t in times]
            results[q["refId"]] = {"status": 200, "frames": [{
                "schema": {"refId": q["refId"], "fields": [
                    {"name": "Time", "type": "time"},
                    {"name": "Value", "type": "number", "labels": {"job": "api"},
                     "config": {"displayNameFromDS": "api"}}]},
                "data": {"values": [[t * 1000 for t in times], values]}}]}
        self.answer({"results": results})


def dashboard(url, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GRAFANA_", "SSH_"))}
    return subprocess.run([sys.executable, str(SCRIPT), "--grafana", url, *args],
                          capture_output=True, text=True, timeout=60, env=env,
                          stdin=subprocess.DEVNULL)


def check_snapshot(url, tmp):
    Grafana.queries.clear()
    out = os.path.join(tmp, "board.png")
    r = dashboard(url, "Fake board", "--snapshot", out)
    found = []
    if r.returncode != 0:
        found.append(f"exit code {r.returncode}: {r.stderr.strip()[-300:]}")
    elif not os.path.exists(out) or Image.open(out).width != 3200:
        found.append("no picture, or not 1600 pixels wide at density 2")
    if "5 panels, 4 drawn" not in r.stdout:  # the repeat makes two copies of one
        found.append(f"expected 5 panels, 4 drawn: {r.stdout.strip()}")
    exprs = [q[0] for q in Grafana.queries]
    if 'load{job=~"(api|web)"}' not in exprs:
        found.append(f"the variable wasn't filled in as All: {exprs}")
    if not {'up{job="api"}', 'up{job="web"}'} <= set(exprs):
        found.append(f"the repeated panel didn't query each value: {exprs}")
    return report("--snapshot", found)


def check_link(url, tmp):
    Grafana.queries.clear()
    link = f"{url}/d/fake/fake-board?orgId=1&var-job=web&from=now-1h&to=now"
    r = dashboard(url, link, "--snapshot", os.path.join(tmp, "link.png"))
    found = [] if r.returncode == 0 else [f"exit code {r.returncode}: {r.stderr.strip()[-300:]}"]
    load = [q for q in Grafana.queries if q[0] and q[0].startswith("load")]
    if not load or load[0][0] != 'load{job=~"web"}':
        found.append(f"the link's variable didn't reach the query: {load}")
    elif abs((load[0][2] - load[0][1]) - 3600) > 5:
        found.append(f"the link's time range wasn't used: {load[0][2] - load[0][1]} s")
    return report("a dashboard's link", found)


def check_v2(url, tmp):
    r = dashboard(url, "v2", "--snapshot", os.path.join(tmp, "v2.png"))
    found = []
    if r.returncode == 0 or "newer format" not in r.stderr:
        found.append(f"expected a refusal that says why: {r.returncode} {r.stderr.strip()[-200:]}")
    return report("a V2 dashboard", found)


def read_for(master, seconds):
    buf, end = b"", time.monotonic() + seconds
    while time.monotonic() < end:
        if select.select([master], [], [], 0.05)[0]:
            try:
                buf += os.read(master, 1 << 20)
            except OSError:
                break
    return buf


def check_full_screen(url):
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", *SIZE))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GRAFANA_", "SSH_"))}
    proc = subprocess.Popen([sys.executable, str(SCRIPT), "--grafana", url, "fake"],
                            stdin=slave, stdout=slave, stderr=slave, env=env,
                            start_new_session=True)
    os.close(slave)
    buf = read_for(master, 6)
    os.write(master, b"\t\r")  # the first panel, full screen
    buf += read_for(master, 2)
    os.write(master, b"\x1bq")  # back, and quit
    deadline = time.monotonic() + 10
    while proc.poll() is None and time.monotonic() < deadline:
        buf += read_for(master, 0.1)
    if proc.poll() is None:
        proc.kill()
        proc.wait()
    buf += read_for(master, 0.3)
    os.close(master)
    keys = re.findall(rb"\x1b_G([^;\x1b]*)", buf)
    sent = set(re.findall(rb"a=t,f=100,t=t,i=(\d+)", buf))
    deleted = set(re.findall(rb"a=d,d=I,i=(\d+)", buf))
    found = []
    if b"\x1b[?1049h" not in buf or not buf.rstrip().endswith(b"\x1b[?1049l"):
        found.append("didn't take the alternate screen, or didn't give it back last")
    if b"\x1b[?25h" not in buf:
        found.append("didn't show the cursor again")
    if not sent:
        found.append("no picture was sent (by temporary file)")
    if not all(b"q=2" in k for k in keys):
        found.append("a command is missing q=2, so the terminal would reply at the prompt")
    if sent - deleted:
        found.append(f"pictures left in the terminal: {sorted(sent - deleted)}")
    if proc.returncode != 0:
        tail = re.sub(rb"\x1b[^a-zA-Z]*[a-zA-Z]|\x1b_G[^\x1b]*\x1b\\", b"", buf)[-300:]
        found.append(f"exit code {proc.returncode}: {tail.decode(errors='replace')}")
    return report("full screen", found, f"{len(sent)} pictures")


def report(label, problems, note=""):
    print(f"{'ok  ' if not problems else 'FAIL'}  {label:<28} {note}")
    for p in problems:
        print(f"        {p}")
    return not problems


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Grafana)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    with tempfile.TemporaryDirectory() as tmp:
        results = [check_snapshot(url, tmp), check_link(url, tmp), check_v2(url, tmp),
                   check_full_screen(url)]
    server.shutdown()
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
