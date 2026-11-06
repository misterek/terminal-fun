---
name: terminal-graph
description: Put a live line graph over a corner of the user's terminal (Ghostty or kitty), fed by a PromQL query (through Grafana, a Grafana MCP server, or Prometheus) or by any shell command that prints a number. Use when the user wants to see, graph, chart, plot, watch or keep an eye on a metric or a number over time while they work, or wants such a graph taken down.
---

# Terminal graph

`graph.py`, in this skill's base directory, draws a live line graph over a corner of the
terminal Claude Code is running in, and keeps it updating while the user works. Run it
by its full path (below, `graph.py` means that path). It needs `uv`, or `python3` with
numpy and Pillow.

## Put one up

Always pass `--detach`. Your shell has no terminal of its own, so `--detach` finds the
one Claude Code is running in. It runs the source once, so a bad query or command fails
right there with its error, then starts the graph in the background and returns with
an id.

```sh
graph.py --detach --title "Ping 1.1.1.1" --unit ms \
  --cmd "ping -c1 1.1.1.1 | sed -n 's/.*time=\([0-9.]*\).*/\1/p'"
```

Pick a source:

- **`--cmd 'COMMAND'`** for anything you can measure from a shell. It must print one
  number, `label number` lines (one per series), or Prometheus-style JSON. Run it once
  by itself first to check it prints what you expect. It runs every 2 seconds
  (`--every 10s` to change that); a `--promql` graph updates every 30 seconds.
- **`--promql 'QUERY'`** for metrics in Prometheus or Grafana. It reaches them through
  whichever of these is set up (a flag beats an environment variable):
  - Grafana's API: `$GRAFANA_URL` with `$GRAFANA_TOKEN`, or `--grafana URL`
  - a Grafana MCP server: `$GRAFANA_MCP_COMMAND`, or `--mcp '<command that starts it>'`,
    or `--mcp https://...` (with `$GRAFANA_MCP_TOKEN` if it needs one)
  - Prometheus directly: `--prom URL`

  If none is set up, ask the user how to reach their Grafana. `claude mcp get <name>`
  shows how Claude Code starts its own Grafana MCP server, which is usually the right
  `--mcp` command. Don't copy tokens out of config files into a command line. If a
  token isn't in the environment, ask the user to export it. If you have Grafana tools
  of your own, use them first to find the right metric and check the query returns data.
- **`--stdin`** is for the user's own pipelines. It can't `--detach`.

Always give a short `--title`, and a `--unit` when there is one. Also handy:
`--window 1h` (default 15m), `--legend '{{instance}}'` to name series by a label,
`--ymin 0 --ymax 100`, `--size 50x12` (in cells), and
`--at top-left|top-right|bottom-left|bottom-right|center`. If the user wants it
behind their text instead of over it, add `--behind` (faded; `--opacity` to taste).
It stays in its corner; use `--at fill` (the whole window) only if they ask for that.

## After

- Tell the user what you put up, where, and its id. It goes away by itself after 30
  minutes. Use `--for 2h` for longer, or `--for 0` to keep it until it's stopped, but
  only if they ask.
- There's one graph per corner: a new one in the same corner replaces the old one. For
  several at once, use different corners. The top two are best, because the bottom
  ones can cover Claude Code's input box.
- `graph.py --list` shows what's up. `graph.py --stop ID` takes one down (or a corner
  name, or `all`).

## Look first, or show it somewhere else

`graph.py --snapshot /tmp/graph.png` with the same source flags, and no `--detach`,
draws one frame to a file. It prints how many series and points it got, or the error.
Read the PNG to see the graph. The user may be following along from another device
(Remote Control, the web, a phone), and the live graph only shows on the computer's
terminal. Send them the snapshot too.

## When it doesn't work

- **"can't find a terminal"**: Claude Code isn't running in a terminal on this machine,
  as in a cloud session. Use `--snapshot` instead.
- **It starts, but the user sees nothing**: their terminal doesn't support the kitty
  graphics protocol, or it's running inside tmux or screen, which don't pass it through.
- **`--detach` fails**: the message is the source's own error. Fix the query or command
  and run it again.
