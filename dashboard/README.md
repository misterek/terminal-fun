# dashboard

Shows a Grafana dashboard in your terminal, full screen. It reads the dashboard the way
Grafana's own page does, has Grafana run each panel's queries, and draws the panels
itself, where the dashboard puts them. Variables, repeated panels and rows, collapsed
rows, library panels and the common transformations all work. A panel it doesn't know
how to draw keeps its place, as an empty frame with its title.

Unlike the others here, this one isn't a background job. It takes over the terminal,
the way `htop` or `less` does, so it can read keys and the mouse. `q` gives the
terminal back.

It needs a terminal that supports the
[kitty graphics protocol](https://sw.kovidgoyal.net/kitty/graphics-protocol/).
It was made for [Ghostty](https://ghostty.org); [kitty](https://sw.kovidgoyal.net/kitty/)
should work too.

## Run it

With [uv](https://docs.astral.sh/uv/), straight from GitHub, pinned to a commit:

```sh
uv run https://raw.githubusercontent.com/misterek/terminal-fun/<commit>/dashboard/dashboard.py node
```

Replace `<commit>` with the full hash of a commit you've looked at. Or from a clone:

```sh
git clone https://github.com/misterek/terminal-fun
cd terminal-fun/dashboard
./dashboard.py node
```

Without uv, install the dependencies yourself (`pip install numpy pillow`) and run
`python3 dashboard.py`.

## Which dashboard

```sh
./dashboard.py node                 # by title, or part of one; a list to pick from if several match
./dashboard.py abc123               # by uid
./dashboard.py 'https://grafana.example.com/d/abc123/node?var-job=api&from=now-6h'
./dashboard.py                      # a list of every dashboard
```

A link pasted from your browser brings its variables (`var-...`) and time range
(`from`, `to`) along, and `viewPanel` opens that panel full screen.

## Getting to Grafana

Three ways. A flag wins over an environment variable.

- **Grafana's API:** `$GRAFANA_URL` and `$GRAFANA_TOKEN` (or
  `$GRAFANA_SERVICE_ACCOUNT_TOKEN`), or `--grafana URL`. A viewer's token is enough.
- **A Grafana MCP server**, such as [mcp-grafana](https://github.com/grafana/mcp-grafana):
  `$GRAFANA_MCP_COMMAND` or `--mcp`. Give it either the command that starts the server,
  like `--mcp "uvx mcp-grafana"`, which gets the same `GRAFANA_URL` and token variables,
  or its URL, with `$GRAFANA_MCP_TOKEN` if it needs one. Its query tools are one per
  kind of datasource, and this uses the Prometheus one, so panels on other datasources
  say they can't be drawn this way.
- **Grafana's gcx CLI**, already logged in: `--gcx`.
  It's also what's used when none of the above is set and gcx is installed. Every
  request goes through `gcx api`, so the token stays in gcx.

It shows what its login is allowed to read, the same as Grafana's own search does.
That's the catch with a service account: it sees the dashboards whose permissions
include its role (Viewer, Editor), or the account itself. A dashboard or folder whose
permissions leave those out doesn't show up in the list, and asking for it by name
says it isn't there. To fix that, give the folder (Folder actions → Manage
permissions) or the dashboard (Settings → Permissions) a **Role: Viewer → View**
permission.

## Keys

```
↑ ↓ PgUp PgDn, the mouse wheel   scroll
Tab / Shift-Tab                   the next / previous panel
Enter, or a click                 a panel full screen (Enter or Esc again: back);
                                  on a row's title, open or close the row
t   the time range                v   the variables
r   refresh now                   d   another dashboard
?   these keys                    q   quit
```

In the lists (`t`, `v`, `d`), typing filters, Enter picks, and Esc closes. A variable
that takes several values gets ticks: Space ticks, Enter takes them.

## What it draws

Time series (and the old graph panel), stat (and the old singlestat), gauge, bar
gauge, table, text, heatmap, state timeline, status history, pie chart, bar chart,
histogram, logs, and xy chart. Each follows what the dashboard says about it: units,
decimals, thresholds and their colors, color modes, value mappings, overrides, legends,
stacking, and so on.

Anything else (plugins, alert lists, maps, traces) is an empty frame with its title.

Before drawing, a panel's transformations run, the same ones Grafana runs in your
browser: merge, organize, reduce, join, labels to fields, series to rows, calculate
field, group by, filter, sort, limit, rename and convert. Ones it doesn't know are
skipped.

Dashboards in Grafana's newer layout (V2: tabs, auto grids) come from Grafana 13's API
already turned into the classic layout, tabs as rows, and are drawn like that. Through
an MCP server they come as they are, and it says it can't draw them.

## Options

```
--var NAME=VALUE      set a variable (again for more values; $__all for All)
--from TIME           start of the time range, like now-6h
--to TIME             end of the time range (default: now)
--refresh DURATION    how often to fetch again (default: the dashboard's, or 30s)
--theme graph|space   the look (default graph)
--grafana URL         Grafana's URL (default: $GRAFANA_URL)
--mcp COMMAND|URL     through a Grafana MCP server (default: $GRAFANA_MCP_COMMAND)
--gcx                 through the gcx CLI
--gcx-context NAME    gcx's context to use
--density N           pixels drawn per pixel the terminal reports (default 2, sharp on Retina)
--snapshot PNG        draw the dashboard once into a picture and exit
--width N             with --snapshot: the width to lay it out for, in pixels (default 1600)
--cell WxH            with --snapshot: a cell's size, in pixels (default 9x19)
--log FILE            write tracebacks here (for bugs)
```

## Themes

Two looks come with it:

- **`--theme graph`**, the default, is the same dark style as [graph](../graph/).
- **`--theme space`** is deep blue, with neon lines and stars behind everything.

Everything about how it looks is in one class in `dashboard.py`, `Theme`:
- the colors of the panels, text, grid lines and series
- the fonts
- how round the corners are and how see-through the panels are
- an optional picture to put behind it all

The bars at the top and bottom take their colors from it too. A new look is a subclass
that changes what it wants, plus a line in `THEMES`:

```python
class GreenScreenTheme(Theme):
    name = "green"
    screen = surface = (8, 16, 8)
    ink, ink_2, muted = (120, 255, 120), (90, 200, 90), (50, 120, 50)
    grid, axis = (18, 40, 18), (28, 60, 28)
    series = [(120, 255, 120), (200, 255, 120), (90, 200, 160)]
    regular = bold = MONO  # everything in a terminal-like font

THEMES["green"] = GreenScreenTheme
```

and then `./dashboard.py node --theme green`. For a picture behind the panels, give it
a `backdrop(w, h)` method that returns one (`SpaceTheme`'s draws the stars).

Colors a dashboard asks for by name (green, red, semi-dark-red and so on) come out as
the theme's own green and red, so a threshold still reads as good or bad in any theme.
Colors it gives as hex (`#ff8800`) stay as they are.

## How it works

It reads the dashboard's JSON from `/api/dashboards/uid/<uid>`, fills in its variables
(running their queries, in the order they depend on each other), copies out repeated
panels and rows, and lays the panels out on Grafana's 24-column grid, scaled so a CSS
pixel is what [graph](../graph/) calls one.

Each panel's queries go to Grafana's `/api/ds/query`, with the variables filled in, the
same as the browser sends them. So any datasource Grafana can query works, and nothing
here needs to know PromQL, LogQL or SQL. The answers come back as Grafana's data frames,
which the panels draw from. It only fetches panels that are on screen, or close to it,
and fetches them again every refresh.

Each panel is its own picture, drawn with Pillow and sent with the kitty graphics
protocol. Scrolling moves them without sending them again; a panel cut off at the edge
of the screen is shown cropped. A new picture goes in under the panel's other image id
and replaces the old one in the same write, so it can't blink. Pictures go over in a
temporary file the terminal reads and deletes. Over SSH, the pixels go in the command
itself.

The bars at the top and bottom, and row titles, are ordinary text.

## Limitations

- **No hover.** Tooltips, crosshairs and links in panels aren't here.
- **Ad hoc filters are ignored.** The variable shows, but nothing is filtered.
- **Variables' own queries** run for Prometheus-style datasources only. Others keep the
  values the dashboard was saved with.
- **Through an MCP server**, only Prometheus panels draw, library panels can't be read,
  and V2 dashboards aren't drawn.
- **Annotations** aren't drawn.
- **Not inside tmux or screen.** They don't pass the kitty graphics protocol through.

## Check

`./check.py` runs the script against a stand-in Grafana and a fake terminal, so nothing
is drawn on your screen and no real Grafana is asked anything. It checks that
`--snapshot` draws a dashboard, with a query variable filled in, a panel repeated for
each of its values, and an unknown panel type left as an empty frame; that a
dashboard's link brings its variables and time range; that a dashboard in the V2 format is
turned down with a reason; and that full screen takes the terminal and gives it back,
with every picture deleted on the way out.
