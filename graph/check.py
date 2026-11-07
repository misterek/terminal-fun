#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["numpy", "Pillow>=10.1"]
# ///
"""Quick check that graph.py behaves. It runs against fake terminals, with its own
private list of running graphs, so nothing is drawn on your screen and graphs you
have showing are left alone.

    ./check.py        (or: uv run check.py)

It checks that every command is quiet (q=2, so nothing gets typed at your prompt),
that the cursor is always put back, and that the image is deleted on exit; that a
picture goes over in a file, and a terminal that leaves the files alone gets the
pixels instead. Then that
--detach starts a graph and returns, finds the terminal on its own, replaces a graph
in the same corner, and reports a broken source instead of starting; that --list and
--stop work; that --snapshot writes a picture; that --table reads text in columns and
JSON; that --promql works through a Grafana MCP server, both one it starts and one at a
URL (a stand-in for each), for a graph and for a table; and that the script exits
quietly when its terminal closes.
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
import threading
import time
from pathlib import Path

from PIL import Image

SCRIPT = Path(__file__).resolve().parent / "graph.py"
SIZE = (34, 134, 2152, 1184)  # rows, cols, width px, height px

# A stand-in for a Grafana MCP server, with the two tools graph.py uses. Run as a
# command it talks over stdin and stdout, and is chatty there, as some real ones are;
# with --http it serves MCP over HTTP, wants a bearer token and a session id, and
# answers tool calls as server-sent events.
FAKE_MCP = r'''
import calendar, json, math, os, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def handle(msg):
    if msg["method"] == "initialize":
        return {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-grafana", "version": "0"}}
    name, args = msg["params"]["name"], msg["params"]["arguments"]
    if name == "list_datasources":
        out = [{"uid": "logs", "type": "loki"},
               {"uid": "prom1", "type": "prometheus", "isDefault": True}]
    elif args.get("queryType") == "instant":
        out = [{"metric": {"instance": f"host-{i}", "job": "node"},
                "value": [time.time(), str(5 + 3 * i)]} for i in range(2)]
    else:
        start, end = (calendar.timegm(time.strptime(args[k], "%Y-%m-%dT%H:%M:%SZ"))
                      for k in ("startTime", "endTime"))
        out = [{"metric": {"instance": f"host-{i}"},
                "values": [[t, str(5 + 3 * i + math.sin(t / 60))]
                           for t in range(start, end + 1, args["stepSeconds"])]}
               for i in range(2)]
    return {"content": [{"type": "text", "text": json.dumps(out)}]}

if os.environ.get("FAKE_MCP_PIDS"):
    with open(os.environ["FAKE_MCP_PIDS"], "a") as f:
        f.write(f"{os.getpid()}\n")

if "--http" not in sys.argv:
    print("fake grafana MCP server starting", flush=True)
    for line in sys.stdin:
        msg = json.loads(line)
        if "id" in msg:
            print(json.dumps({"jsonrpc": "2.0", "method": "notifications/message",
                              "params": {"level": "info", "data": "working"}}), flush=True)
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": handle(msg)}),
                  flush=True)
    sys.exit()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.headers.get("Authorization") != "Bearer sekrit":
            return self.reply(401)
        if msg.get("method") != "initialize" and self.headers.get("Mcp-Session-Id") != "s1":
            return self.reply(400)
        if "id" not in msg:
            return self.reply(202)
        body = json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": handle(msg)})
        if msg["method"] == "initialize":
            return self.reply(200, body.encode(), "application/json", {"Mcp-Session-Id": "s1"})
        self.reply(200, f"event: message\ndata: {body}\n\n".encode(), "text/event-stream")

    def reply(self, code, body=b"", ctype="application/json", headers={}):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
print(server.server_address[1], flush=True)
server.serve_forever()
'''


def set_size(fd, size):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", *size))


def fake_terminal():
    master, slave = pty.openpty()
    set_size(slave, SIZE)
    return master, slave


def graph(*args, **kw):
    """Run graph.py to completion (for --detach, --list, --stop, --snapshot)."""
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True,
                          text=True, timeout=30, stdin=subprocess.DEVNULL, **kw)


class Reader:
    """Reads a fake terminal nonstop in the background, as a real one would. A terminal
    that stops reading leaves graph.py stuck partway through a frame."""

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


def read_for(master, seconds):
    buf, end = b"", time.monotonic() + seconds
    while time.monotonic() < end:
        if select.select([master], [], [], 0.05)[0]:
            try:
                buf += os.read(master, 1 << 20)
            except OSError:
                break
    return buf


def problems_in(buf, deleted=True):
    """What's wrong with a stream of output from graph.py."""
    keys = re.findall(rb"\x1b_G([^;\x1b]*)", buf)
    commands = [k for k in keys if not k.startswith(b"m=")]  # skip continuation chunks
    found = []
    if not any(k.startswith(b"a=T,f=100") for k in commands):
        found.append("no picture was sent")
    if not all(b"q=2" in k for k in keys):
        found.append("a command is missing q=2, so the terminal would reply at the prompt")
    if buf.count(b"\x1b7") != buf.count(b"\x1b8"):
        found.append("cursor saves and restores don't pair up")
    if deleted and not (commands and re.fullmatch(rb"a=d,d=I,i=\d+,q=2", commands[-1])):
        found.append("the image wasn't deleted at exit")
    return found


def check_live(label, *args, stdin_data=None, want=None, seconds=2.5):
    master, slave = fake_terminal()
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT), "--tty", os.ttyname(slave), *args],
        stdin=subprocess.PIPE if stdin_data else subprocess.DEVNULL, stderr=subprocess.PIPE)
    if stdin_data:
        proc.stdin.write(stdin_data)
        proc.stdin.flush()
    buf = read_for(master, seconds)
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
    if want and not re.search(want, buf):
        found.append(f"never sent {want!r}")
    if proc.returncode != 0:
        found.append(f"exit code {proc.returncode}: {err[-300:]}")
    return report(label, found, f"{len(buf) / seconds / 1024:.0f} KB/s")


def detached_id(result):
    m = re.search(r"\(id (\d+)\)", result.stdout)
    return m and m.group(1)


def check_detach():
    master, slave = fake_terminal()
    reader = Reader(master)
    found = []
    started = time.monotonic()
    r = graph("--cmd", "echo 4.2", "--title", "Detached", "--detach", "--tty", os.ttyname(slave))
    took = time.monotonic() - started
    gid = detached_id(r)
    if r.returncode != 0 or not gid:
        found.append(f"didn't start (code {r.returncode}): {(r.stdout + r.stderr)[-300:]}")
    elif took > 10:
        found.append(f"took {took:.0f}s to return")
    time.sleep(1.5)
    listed = graph("--list").stdout
    if gid and gid not in listed:
        found.append(f"--list doesn't show it: {listed!r}")
    leftovers = list(Path(os.environ["XDG_RUNTIME_DIR"]).glob("terminal-fun-graph-*/seed-*"))
    if leftovers:
        found.append(f"left the data it handed over behind: {leftovers}")
    stopped = graph("--stop", gid or "all")
    if stopped.returncode != 0:
        found.append(f"--stop failed: {(stopped.stdout + stopped.stderr)[-300:]}")
    time.sleep(0.3)
    found += problems_in(reader.stop())
    if "nothing showing" not in graph("--list").stdout:
        found.append("still listed after --stop")
    os.close(master)
    os.close(slave)
    return report("--detach, --list, --stop", found, f"returned in {took:.1f}s")


def check_finds_terminal(tmp):
    """Like an agent's shell: no terminal on stdin/stdout/stderr, but a parent that has one."""
    out = Path(tmp) / "out.txt"
    pid, master = pty.fork()
    if pid == 0:  # the child: its controlling terminal is the fake one
        # The shell stays around, like the shell an agent runs in: on macOS, once the
        # shell that owns a terminal exits, nobody else can draw on it.
        os.execvp("sh", ["sh", "-c",
                         f"'{sys.executable}' '{SCRIPT}' --cmd 'echo 1' --detach --for 3 "
                         f"</dev/null >'{out}' 2>&1; sleep 5"])
    set_size(master, SIZE)
    buf = read_for(master, 6)  # it shows for 3 seconds, then removes itself
    os.waitpid(pid, 0)
    os.close(master)
    said = out.read_text() if out.exists() else ""
    found = [] if "(id " in said else [f"didn't start: {said.strip()[-300:]!r}"]
    found += problems_in(buf)
    return report("--detach finds the terminal", found)


def check_replaces():
    master, slave = fake_terminal()
    path = os.ttyname(slave)
    reader = Reader(master)
    first = detached_id(graph("--cmd", "echo 1", "--title", "First", "--detach", "--tty", path))
    second = detached_id(graph("--cmd", "echo 2", "--title", "Second", "--detach", "--tty", path))
    listed = graph("--list").stdout
    found = []
    if not (first and second):
        found.append("didn't start both")
    elif first in listed or second not in listed:
        found.append(f"the second didn't replace the first: {listed!r}")
    graph("--stop", "all")
    reader.stop()
    os.close(master)
    os.close(slave)
    return report("same corner replaces", found)


def check_bad_source():
    """A replacement whose source fails its check doesn't start, and leaves the graph
    that was already in that corner alone."""
    master, slave = fake_terminal()
    reader = Reader(master)
    path = os.ttyname(slave)
    first = detached_id(graph("--cmd", "echo 1", "--title", "First", "--detach", "--tty", path))
    r = graph("--cmd", "echo oops >&2; exit 1", "--detach", "--tty", path)
    found = []
    if r.returncode == 0:
        found.append("started anyway")
    if "oops" not in r.stderr:
        found.append(f"didn't say what went wrong: {r.stderr.strip()!r}")
    listed = graph("--list").stdout
    if not first or first not in listed:
        found.append(f"the graph that was already there got stopped: {listed!r}")
    elif len(listed.splitlines()) != 1:
        found.append(f"left an extra graph running: {listed!r}")
    graph("--stop", "all")
    reader.stop()
    os.close(master)
    os.close(slave)
    return report("--detach, broken source", found)


def check_snapshot(tmp):
    png = Path(tmp) / "snap.png"
    r = graph("--cmd", "echo 3.5", "--title", "Snapshot", "--snapshot", str(png))
    found = []
    if r.returncode != 0 or not png.exists():
        found.append(f"no picture (code {r.returncode}): {r.stderr.strip()[-300:]}")
    else:
        img = Image.open(png)
        if img.mode != "RGBA" or img.width < 200:
            found.append(f"unexpected picture: {img.mode} {img.size}")
        elif img.getpixel((0, 0))[3] != 0:
            found.append("the rounded corners aren't transparent")
    return report("--snapshot", found)


def check_table_snapshot(tmp):
    """--table reads text in columns (values with spaces in them included) and JSON."""
    pods = ("NAME                  READY   STATUS             RESTARTS      AGE\n"
            "api-7d9f8c6b5-x2kqp   1/1     Running            0             3d\n"
            "api-7d9f8c6b5-m4n8z   0/1     CrashLoopBackOff   7 (40s ago)   12m\n"
            "redis-0               1/1     Running            1 (2d ago)    9d\n")
    text = Path(tmp) / "pods.txt"
    text.write_text(pods)
    found = []
    for args, want in (
            (["--cmd", f"cat '{text}'"], "3 rows, 5 columns"),
            (["--cmd", "printf '{\"Names\":\"web\",\"Status\":\"Up 2 hours\"}\\n"
                       "{\"Names\":\"db\",\"Status\":\"Exited (1) 3 days ago\"}\\n'"],
             "2 rows, 2 columns"),
            (["--cmd", f"cat '{text}'", "--columns", "status,name"], "3 rows, 2 columns")):
        r = graph("--table", *args, "--snapshot", str(Path(tmp) / "table.png"))
        if want not in r.stdout:
            found.append(f"{args[-1][:40]!r}: wanted {want!r}, got "
                         f"{(r.stdout + r.stderr).strip()[-300:]!r}")
    return report("--table, text and JSON", found)


def check_mcp(tmp):
    """--promql through a fake Grafana MCP server: started as a command, and over HTTP."""
    fake = Path(tmp) / "fake_mcp.py"
    fake.write_text(FAKE_MCP)
    pids = Path(tmp) / "mcp_pids.txt"
    env = {**os.environ, "FAKE_MCP_PIDS": str(pids)}
    command = f"'{sys.executable}' '{fake}'"
    ok = True

    found = []
    r = graph("--promql", "up", "--mcp", command, "--snapshot", str(Path(tmp) / "mcp.png"),
              env=env)
    if "2 series" not in r.stdout:
        found.append(f"didn't get the data: {(r.stdout + r.stderr).strip()[-300:]}")
    ok &= report("--mcp COMMAND", found)

    found = []
    r = graph("--promql", "up", "--table", "--mcp", command, "--snapshot",
              str(Path(tmp) / "mcp-table.png"), env=env)
    if "2 rows, 2 columns" not in r.stdout:  # instance and the value; job is the same
        found.append(f"didn't get the rows: {(r.stdout + r.stderr).strip()[-300:]}")
    ok &= report("--mcp, --table", found)

    # Started by a live graph, the server goes when the graph does.
    found = []
    master, slave = fake_terminal()
    reader = Reader(master)
    gid = detached_id(graph("--promql", "up", "--mcp", command, "--detach",
                            "--tty", os.ttyname(slave), env=env))
    time.sleep(1.5)
    graph("--stop", gid or "all")
    time.sleep(0.3)
    reader.stop()
    os.close(master)
    os.close(slave)
    if not gid:
        found.append("didn't start")
    for pid in map(int, pids.read_text().split() if pids.exists() else []):
        try:
            os.kill(pid, 0)
            found.append(f"MCP server {pid} still running after the graph stopped")
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    ok &= report("--mcp, server stops after", found)

    found = []
    r = graph("--promql", "up", "--mcp", "/nonexistent/mcp-server", "--detach",
              "--tty", "/dev/null")
    if r.returncode == 0 or "can't start the MCP server" not in r.stderr:
        found.append(f"unexpected: code {r.returncode}, {r.stderr.strip()!r}")
    ok &= report("--mcp, missing server", found)

    found = []
    server = subprocess.Popen([sys.executable, str(fake), "--http"], stdout=subprocess.PIPE,
                              text=True)
    try:
        url = f"http://127.0.0.1:{server.stdout.readline().strip()}/mcp"
        r = graph("--promql", "up", "--mcp", url, "--snapshot", str(Path(tmp) / "http.png"),
                  env={**os.environ, "GRAFANA_MCP_TOKEN": "sekrit"})
        if "2 series" not in r.stdout:
            found.append(f"didn't get the data: {(r.stdout + r.stderr).strip()[-300:]}")
        r = graph("--promql", "up", "--mcp", url, "--snapshot", str(Path(tmp) / "http.png"),
                  env={**os.environ, "GRAFANA_MCP_TOKEN": "wrong"})
        if "HTTP 401" not in r.stdout:
            found.append(f"a wrong token didn't show as HTTP 401: {r.stdout.strip()[-300:]}")
    finally:
        server.kill()
        server.wait()
    ok &= report("--mcp URL", found)
    return ok


def check_terminal_closes():
    master, slave = fake_terminal()
    proc = subprocess.Popen([sys.executable, str(SCRIPT), "--tty", os.ttyname(slave),
                             "--cmd", "echo 1"],
                            stdin=subprocess.DEVNULL, stderr=subprocess.PIPE)
    read_for(master, 1.5)
    os.close(master)
    os.close(slave)
    proc.send_signal(signal.SIGHUP)  # what the shell sends its jobs when the window closes
    found = []
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        found.append("still running after its terminal closed")
    err = proc.stderr.read().decode(errors="replace").strip()
    if proc.returncode != 0 or err:
        found.append(f"didn't exit quietly (code {proc.returncode}): {err[-300:]}")
    return report("terminal closes", found)


def report(label, problems, note=""):
    print(f"{'ok  ' if not problems else 'FAIL'}  {label:<28} {note}")
    for p in problems:
        print(f"        {p}")
    return not problems


def main():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_RUNTIME_DIR"] = tmp  # a private list of running graphs
        for k in ("SSH_CONNECTION", "SSH_TTY"):  # as if the terminal were this machine's
            os.environ.pop(k, None)
        results = [
            # want: the picture sent as a file, and the hidden placement that keeps it
            # alive through `clear`
            check_live("--cmd", "--cmd", "echo 4.2", "--unit", "ms",
                       want=rb"a=T,f=100,[^;]*,t=t;.*a=p,U=1,i=\d+,p=2,"),
            # A terminal that leaves the files alone (this fake one, or one over SSH)
            # gets the pixels themselves after a few seconds.
            check_live("can't read files", "--cmd", "echo 4.2",
                       want=rb"a=T,f=100,[^;\x1b]*,q=2,m=[01];", seconds=7),
            check_live("--cmd, two series", "--cmd", "printf 'api 3\\nweb 7\\n'",
                       "--at", "bottom-left", "--size", "40x10"),
            check_live("--stdin", "--stdin", stdin_data=b"1\n2\n3\n"),
            check_live("--behind --at fill", "--cmd", "echo 3", "--behind", "--at", "fill",
                       want=rb"a=T,f=100,i=\d+,p=1,c=134,r=34,z=-1,"),
            check_live("--table", "--table", "--cmd",
                       "printf 'NAME   STATUS\\napi    Running\\nweb    Pending\\n'",
                       want=rb"a=p,U=1,i=\d+,p=2,"),
            check_table_snapshot(tmp),
            check_detach(),
            check_finds_terminal(tmp),
            check_replaces(),
            check_bad_source(),
            check_snapshot(tmp),
            check_mcp(tmp),
            check_terminal_closes(),
        ]
        graph("--stop", "all")  # in case a failed check left one behind
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
