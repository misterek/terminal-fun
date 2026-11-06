# graph

Puts a live line graph over a corner of your terminal while you keep working. It can
graph a Grafana or Prometheus query, any command that prints a number, or numbers
coming in on a pipe. Like [background](../background/), it runs as an ordinary
background job and works over SSH.

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
  `query_prometheus` tools.
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
`--for` says otherwise (`--for 0` keeps it until it's stopped). A new graph in the same
corner of the same terminal replaces the old one.

`--snapshot` also prints how many series and points it got, or the error, so an agent
can tell without opening the picture.

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

Then ask Claude for a graph ("keep an eye on the ping to 1.1.1.1 for me"). It's a
starting point: edit `SKILL.md` to fit how you work, such as which Grafana to use, or
which corner you like. Put it in a project's `.claude/skills/` instead to keep it to
that project.

## Options

```
--title TEXT       panel title (default: the query or command)
--unit TEXT        shown after values, like ms, %, req/s
--window DURATION  how much history to show (default 15m)
--every DURATION   how often to fetch (default 30s for --promql, 2s for --cmd)
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
--size COLSxROWS   size in cells, shrunk to fit the window (default 64x15)
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
the text (the panel is 93% opaque). Once a second it sends fresh pixels, so the time
axis slides along. In between, forty times a second, it re-pins the image to its
corner. The drawing happens in a thread of its own, so the re-pinning never waits
for it. Images in that protocol are pinned to cells, not to the window, so they scroll
away with your output, and `clear` deletes them. Re-pinning brings the graph back
within a moment.

It follows the same rules as background for drawing from a background job: it never
reads from the terminal, every command asks the terminal not to reply (`q=2`), and
each frame is a single write that saves and restores the cursor. Frames go out as
256-color PNGs of about 13 KB, a fifth of the full-color size. Smaller frames give
other output less chance to land in the middle of one on macOS (see below).

While it runs, each graph keeps a small file in a private folder (under
`$XDG_RUNTIME_DIR`, or your temp folder). That's how `--list`, `--stop` and replacing
find it.

## Limitations

- **Line charts only**, one query or command per graph. A query that returns several
  series draws up to 8 of them, with a legend.
- **One graph per place on a terminal** (a corner, center, or fill): a new one
  replaces the old one.
- **It rides along when the text scrolls**, for up to 1/40 of a second, then pops
  back. Images in the kitty graphics protocol are pinned to text cells, and nothing
  in it pins one to the window. A background job can't see the scroll coming either,
  because it never reads from the terminal. So all it can do is put the graph back
  quickly, and it does.
- **macOS, nonstop output:** like background, while a lot of output scrolls by
  without a break, a line can occasionally go missing from the screen. Linux, including
  a Linux machine you SSH into from a Mac, keeps each write whole.
- **Not inside tmux or screen.** They don't pass the kitty graphics protocol through.
- **Fixed look:** dark theme, about 93% opaque, no thresholds or annotations.

## Check

`./check.py` runs the script against fake terminals, with its own private list of
running graphs. Nothing is drawn on your screen, and graphs you have showing are left
alone. It checks:

- every command is quiet, the cursor is always put back, and the image is deleted on exit
- `--detach` returns right away, finds the terminal on its own, and replaces a graph in
  the same corner
- a broken source makes `--detach` fail instead of starting
- `--list` and `--stop` work
- `--snapshot` writes a picture
- `--promql` works through a Grafana MCP server, both one it starts and one at a URL
  (using a stand-in server), and a server it started stops with the graph
- the script exits quietly when its terminal closes
