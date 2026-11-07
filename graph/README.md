# graph

Puts a live line graph over a corner of your terminal while you keep working. It can
graph a Grafana or Prometheus query, any command that prints a number, or numbers
coming in on a pipe. Or it can show a live table instead, like a `kubectl get pods`
that lights up what changed. Like [background](../background/), it runs as an
ordinary background job and works over SSH.

It's also made for agents like Claude Code. Their commands run without a terminal of
their own, so `--detach` finds the terminal the agent is running in, starts the graph
there, and gives the agent its prompt back.

It needs a terminal that supports the
[kitty graphics protocol](https://sw.kovidgoyal.net/kitty/graphics-protocol/).
It was made for [Ghostty](https://ghostty.org); [kitty](https://sw.kovidgoyal.net/kitty/)
should work too.

## Run it

With [uv](https://docs.astral.sh/uv/), straight from GitHub, pinned to a commit:

```sh
uv run https://raw.githubusercontent.com/misterek/terminal-fun/<commit>/graph/graph.py --promql node_load1 &
```

Replace `<commit>` with the full hash of a commit you've looked at. Or from a clone:

```sh
git clone https://github.com/misterek/terminal-fun
cd terminal-fun/graph
./graph.py --promql node_load1 &
```

Without uv, install the dependencies yourself (`pip install numpy pillow`) and run
`python3 graph.py`.

Stop it with `kill %1`, or `./graph.py --stop all`. It removes its graph on the way out.

## Sources

Pick one:

```sh
# PromQL, through Grafana or straight at Prometheus
./graph.py --promql node_load1 --title "Load average" &
./graph.py --promql 'sum by (instance) (rate(node_network_receive_bytes_total[1m]))' \
           --legend '{{instance}}' --title "Network in" --unit B/s &

# any command that prints a number, run every couple of seconds
./graph.py --cmd "ping -c1 1.1.1.1 | sed -n 's/.*time=\([0-9.]*\).*/\1/p'" --title Ping --unit ms &

# numbers as they come in on a pipe
some-producer | ./graph.py --stdin --title "Queue depth" &
```

**`--promql`** fills in the whole window as soon as it starts, so there's history from
the first frame. It can reach your metrics three ways. A flag wins over an environment
variable.

- **Grafana's API:** `$GRAFANA_URL` and `$GRAFANA_TOKEN` (or
  `$GRAFANA_SERVICE_ACCOUNT_TOKEN`), or `--grafana URL`.
- **A Grafana MCP server**, such as [mcp-grafana](https://github.com/grafana/mcp-grafana):
  `$GRAFANA_MCP_COMMAND` or `--mcp`. Give it either the command that starts the server,
  like `--mcp "uvx mcp-grafana"`, which then gets the same `GRAFANA_URL` and token
  variables, or its URL, like `--mcp https://mcp.example.com/mcp`, with
  `$GRAFANA_MCP_TOKEN` if it needs a token. It uses the server's `list_datasources` and
  `query_prometheus` tools. The server is started once and kept for every update after
  that, and only replaced after something goes wrong. It gets 2 minutes to start (uvx,
  npx and docker can be slow, the first time especially), and each query gets 30 seconds.
- **Prometheus directly** (or VictoriaMetrics, and the like): `--prom URL`, with
  `$PROM_TOKEN` if it needs one.

Through Grafana, it queries the default Prometheus datasource unless you pass `--ds UID`.

**`--cmd`** runs a shell command every `--every` seconds (default 2). It can print:

- one number, like `4.2`
- `label number` lines, one per series, like `api 4.2`
- Prometheus-style JSON. A range result fills in the whole window, so a `curl` to
  any Prometheus-style API works.

The command gets the window it should cover in `$GRAPH_START` and `$GRAPH_END` (unix
seconds), `$GRAPH_START_ISO` and `$GRAPH_END_ISO`, `$GRAPH_STEP` (seconds), and
`$GRAPH_WINDOW` (like `15m`).

**`--stdin`** reads numbers, or `label number` lines, as they arrive.

## Behind the text

`--behind` draws the graph under the text instead of over it, faded to 40% so what
you're working on stays easy to read (`--opacity` changes that):

```sh
./graph.py --promql node_load1 --title "Load average" --behind &
```

It stays in its corner like any other graph. `--at fill` stretches it over the whole
window instead, like [background](../background/).

It goes under the text but over the cells' background colors, so it also shows
through apps that paint their own backgrounds, like vim and htop.

## Tables

`--table` shows rows instead of a line, from `--cmd` or `--promql`:

```sh
# text in columns, the way kubectl, docker and df print it
./graph.py --table --cmd "kubectl get pods" --title Pods &

# JSON: a list of objects, or one object per line
./graph.py --table --cmd "docker ps --format json" --columns Names,Image,Status &

# PromQL: a row per series
./graph.py --table --promql 'kube_pod_container_status_waiting_reason == 1' \
           --title "Waiting containers" &
```

When a cell changes, it lights up and fades over ten seconds. A new row lights up
whole, and a row that goes away stays for ten seconds, struck through. Time passing
doesn't count as a change, so an age going from `12m` to `13m` or `Up 2 hours` to
`Up 3 hours` stays dark. A cell that changes every time it's fetched, like a CPU
number, stops lighting up after the second time. In columns named like a status
(STATUS, STATE, PHASE, READY and so on), words like `Running`, `Pending` and
`CrashLoopBackOff` are colored, and so is a ready count that's short, like `0/1`.

The panel is only as big as what's in it, up to `--size` (80x20 by default). When
it doesn't all fit, the text gets smaller first. Then long values are cut in the
middle, so a pod's name keeps the end that tells it apart. Then columns go, from the
right. Rows that don't fit are counted at the bottom, and the rows kept are the ones
with something to see (a problem, a change, a row coming or going), then the rest
from the top.

`--columns` picks the columns, and their order. Names match whatever their case.

**`--cmd`** can print:

- text in columns under a header line. A column ends where every line has a space,
  so values can have spaces in them, like `7 (40s ago)`. Tab-separated text works
  too.
- JSON: a list of objects, one object per line (`docker ... --format json`), or
  kubectl's `{"items": [...]}`. Nested fields become columns like `metadata.name`.
- Prometheus-style JSON, like a `curl` to the query API: a row per series.

If the command prints nothing, the panel says so, with the last thing it said on
stderr (kubectl's "No resources found"). If it fails, the last table stays up,
dimmed, with the error under it. It runs every 5 seconds.

**`--promql`** runs an instant query every 30 seconds. Each series is a row: a column
for each label that tells the rows apart, and the value, with `--unit`. The value is
left out when it's the same in every row, as with `== 1`. `sort_desc()` or `topk()`
puts the biggest first.

Text in columns can't always be read. Some programs let a wide value push the rest
of its line over, so the columns stop lining up: `ps aux` and `ps -ef` on macOS, `w`,
and sometimes `docker stats`. Ask for fewer columns (`ps -o pid,pcpu,comm`), or use
JSON, which always works. Output with title lines above the header, or several
tables (`netstat -rn`, `kubectl get all`), needs those lines cut off first.

A table can't go `--behind`: text behind text is hard to read.

## From Claude Code (or another agent)

```sh
./graph.py --cmd "..." --title "..." --detach   # start one; prints its id and returns
./graph.py --list                               # what's showing, and where
./graph.py --stop ID                            # an id, a corner of this terminal, or all
./graph.py --cmd "..." --snapshot /tmp/g.png    # one frame to a file, to look at first
```

`--detach` runs the source once first, so a broken query or command fails right there,
where the agent can read the error. Then it starts the graph in the background. It
finds the terminal by walking up the processes that started it, to the first one that
has a terminal: Claude Code itself. A detached graph goes away after 30 minutes unless
`--for` says otherwise (`--for 0` keeps it until it's stopped).

A new graph in the same corner of the same terminal replaces the old one. The old one
stays up until the new one is ready to draw. If the new one's source fails its check,
the old one isn't touched. The background copy starts with the data that check
fetched, so even with a slow source, the line is there from the first picture.

`--snapshot` also prints how many series and points it got (rows and columns, for a
table), or the error, so an agent can tell without opening the picture.

### The Claude Code skill

[skill/](skill/) is a [Claude Code skill](https://code.claude.com/docs/en/skills)
that tells Claude when and how to use all this. That covers which source to pick,
checking a command before graphing it, keeping tokens off the command line, and sending
a snapshot when you're following along from another device. Its `graph.py` is a link
to the script. Either link the folder, so it stays in step with your clone:

```sh
ln -s "$PWD/skill" ~/.claude/skills/terminal-graph
```

or copy it, which turns the link into a real copy of the script:

```sh
cp -RL skill ~/.claude/skills/terminal-graph
```

Then ask Claude for a graph ("keep an eye on the ping to 1.1.1.1 for me") or a table
("watch my pods for me"). It's a
starting point: edit `SKILL.md` to fit how you work, such as which Grafana to use, or
which corner you like. Put it in a project's `.claude/skills/` instead to keep it to
that project.

## Options

```
--table            a table instead of a graph, from --cmd or --promql
--columns A,B,...  with --table: which columns, in this order
--title TEXT       panel title (default: the query or command)
--unit TEXT        shown after values, like ms, %, req/s
--window DURATION  how much history to show (default 15m)
--every DURATION   how often to fetch (default 30s for --promql, 2s for --cmd, 5s for
                   --cmd with --table)
--step DURATION    --promql resolution (default window/240, at least 15s)
--legend TEXT      series names, Grafana style: '{{instance}}'
--grafana URL      --promql through this Grafana's API (default: $GRAFANA_URL)
--mcp COMMAND|URL  --promql through a Grafana MCP server (default: $GRAFANA_MCP_COMMAND)
--prom URL         --promql straight at Prometheus
--ds UID           which Grafana datasource (default: its default Prometheus one)
--ymin, --ymax N   pin the y axis (default: from 0, or the data, to a little over the top)
--at PLACE         top-right (default), top-left, bottom-right, bottom-left, center,
                   or fill (the whole window)
--behind           draw behind the text instead of over it, faded
--opacity N        1 is solid (default 0.93, or 0.4 with --behind)
--size COLSxROWS   size in cells, shrunk to fit the window (default 64x15; for a
                   table, the most it grows to, 80x20)
--density N        pixels drawn per pixel the terminal reports (default 2, sharp on Retina;
                   1 with --behind, where it looks the same and costs half as much)
--tty DEVICE       terminal to draw on (default: this one, or the nearest parent's)
--for DURATION     go away after this long; 0 means never (default: never, or 30m with --detach)
--snapshot PNG     draw one frame to a file and exit
--detach           check the source, start in the background, print the id and return
--list             list the graphs that are showing
--stop ID          stop a graph: an id, a corner of this terminal, or all
```

Durations look like `90`, `90s`, `15m`, `2h` or `1d`.

## How it works

It draws the graph with Pillow and sends it with the kitty graphics protocol, above
the text (the panel is 93% opaque). Once a second it draws fresh pixels, so the time
axis slides along. A picture that came out the same as the last one isn't sent
again, except every 10 seconds, which brings it back after `reset` and inside vim.
That's most of a table's pictures. In between, forty times a second, it re-pins the
image to its corner. The drawing happens in a thread of its own, so the re-pinning
never waits for it. Images in that protocol are pinned to cells, not to the window,
so they scroll away with your output. Re-pinning brings the graph back within a
moment.

Each picture goes over in a temporary file. The terminal reads it and deletes it, so
sending a picture is a write of about 150 bytes. Sent in the command itself, it would
be tens of KB, and macOS can split a write that big. Other output that lands inside
a picture command ends it early, and the rest of the picture then shows up on your
screen as text. Over SSH the terminal can't see the file, so there the pixels go in
the command, as 256-color PNGs, about a fifth the size of full color. It sends them
that way from the start when `$SSH_CONNECTION` is set. Otherwise it switches as soon as
a file goes 5 seconds without the terminal taking it.

`clear`, and full-screen redraws like the ones Claude Code sometimes does, delete what's
on the screen. They also free any image that nothing refers to anymore. So along with
each picture, it adds a hidden "virtual" placement (`U=1`), the same trick background
uses. That placement is only shown where special placeholder characters appear in the
text, so it's never seen, but it keeps the picture alive. The next re-pin then brings
the graph straight back instead of waiting up to a second for a fresh picture.

It follows the same rules as background for drawing from a background job: it never
reads from the terminal, every command asks the terminal not to reply (`q=2`), and
each frame is a single write that saves and restores the cursor.

While it runs, each graph keeps a small file in a private folder (under
`$XDG_RUNTIME_DIR`, or your temp folder). That's how `--list`, `--stop` and replacing
find it.

## Limitations

- **Line charts and tables only**, one query or command each. A query that returns
  several series draws up to 8 of them, with a legend.
- **One graph or table per place on a terminal** (a corner, center, or fill): a new
  one replaces the old one.
- **It rides along when the text scrolls**, for up to 1/40 of a second, then pops
  back. Images in the kitty graphics protocol are pinned to text cells, and nothing
  in it pins one to the window. A background job can't see the scroll coming either,
  because it never reads from the terminal. So all it can do is put the graph back
  quickly, and it does.
- **SSH into a Mac:** there the pictures go through the terminal, and macOS can split
  a big write. So while a lot of output scrolls by, a line can go missing from the
  screen, or a piece of a picture can show up as text. Linux keeps each write whole,
  and on your own machine the pictures go in a file.
- **Not inside tmux or screen.** They don't pass the kitty graphics protocol through.
- **Fixed look:** dark theme, about 93% opaque, no thresholds or annotations.

## Check

`./check.py` runs the script against fake terminals, with its own private list of
running graphs. Nothing is drawn on your screen, and graphs you have showing are left
alone. It checks:

- every command is quiet, the cursor is always put back, and the image is deleted on exit
- a picture goes over in a file, and a terminal that leaves the files alone gets the
  pixels instead
- `--detach` returns right away, finds the terminal on its own, and replaces a graph in
  the same corner
- a broken source makes `--detach` fail instead of starting
- `--list` and `--stop` work
- `--snapshot` writes a picture
- `--table` reads text in columns (values with spaces in them included) and JSON, and
  `--columns` picks columns
- `--promql` works through a Grafana MCP server, both one it starts and one at a URL
  (using a stand-in server), for a graph and for a table, and a server it started
  stops with the graph
- the script exits quietly when its terminal closes
