# picture

Puts a picture up in a corner of your terminal while you keep working: a file, or a
URL. A file is watched, so when something writes a new picture over it (a script
redrawing a chart, say) the new one shows within a second. A URL can be fetched again
every so often, for a webcam or a weather radar. An animated GIF plays. Like
[graph](../graph/), it runs as an ordinary background job, works over SSH, and an
agent like Claude Code can pop one up from its own shell.

It needs a terminal that supports the
[kitty graphics protocol](https://sw.kovidgoyal.net/kitty/graphics-protocol/).
It was made for [Ghostty](https://ghostty.org); [kitty](https://sw.kovidgoyal.net/kitty/)
should work too.

## Run it

With [uv](https://docs.astral.sh/uv/), straight from GitHub, pinned to a commit:

```sh
uv run https://raw.githubusercontent.com/misterek/terminal-fun/<commit>/picture/picture.py cat.jpg &
```

Replace `<commit>` with the full hash of a commit you've looked at. Or from a clone:

```sh
git clone https://github.com/misterek/terminal-fun
cd terminal-fun/picture
./picture.py cat.jpg &
```

Without uv, install Pillow yourself (`pip install pillow`) and run `python3 picture.py`.

Stop it with `kill %1`, or `./picture.py --stop all`. It removes its picture on the way out.

```sh
./picture.py plot.png --at bottom-left --size 40x16 &     # where, and the most it covers
./picture.py https://example.com/radar.png --every 5m &   # fetch it again every 5 minutes
./picture.py cat.jpg --behind &                           # faded, behind the text
```

Anything Pillow can open works: PNG, JPEG, GIF, WebP, and so on. An animated GIF (or
animated PNG or WebP) plays, over and over. The picture keeps its shape, as big as fits
in `--size`.

## From Claude Code (or another agent)

```sh
./picture.py plot.png --detach   # start one; prints its id and returns
./picture.py --list              # what's showing, and where
./picture.py --stop ID           # an id, a corner of this terminal, or all
```

`--detach` loads the picture first, so a missing file or a bad URL fails right there,
where the agent can read the error. Then it shows the picture from the background. It
finds the terminal the same way graph does, by walking up the processes that started
it to the first one that has a terminal: Claude Code itself. A detached picture goes
away after 30 minutes unless `--for` says otherwise (`--for 0` keeps it until it's
stopped). A new picture in the same corner of the same terminal replaces the old one.

Since the file is watched, an agent can put a chart up once and then just save over the
file each time it redraws it.

### The Claude Code skill

[skill/](skill/) is a [Claude Code skill](https://code.claude.com/docs/en/skills)
that tells Claude when and how to use this: always `--detach`, saving over the file to
update a chart, a dark background for charts it makes, and showing you the picture
itself too when you're following along from another device. Its `picture.py` is a link
to the script. Either link the folder, so it stays in step with your clone:

```sh
ln -s "$PWD/skill" ~/.claude/skills/terminal-picture
```

or copy it, which turns the link into a real copy of the script:

```sh
cp -RL skill ~/.claude/skills/terminal-picture
```

Then ask Claude to show you something ("plot the response times and put it up in the
corner", "show me that screenshot"). Edit `SKILL.md` to fit how you work, or put it in
a project's `.claude/skills/` to keep it to that project.

## Options

```
--at PLACE         top-right (default), top-left, bottom-right, bottom-left or center
--size COLSxROWS   the most it covers, in cells; it keeps its shape, and shrinks to fit
                   the window (default 60x20)
--every DURATION   how often to look for a new picture (default: every second for a
                   file; a URL is fetched once unless this is set)
--behind           draw behind the text instead of over it, faded
--opacity N        1 is solid (default 1, or 0.4 with --behind)
--density N        the most pixels sent per pixel the terminal reports (default 2,
                   sharp on Retina; 1 with --behind)
--tty DEVICE       terminal to draw on (default: this one, or the nearest parent's)
--for DURATION     go away after this long; 0 means never (default: never, or 30m with --detach)
--detach           load the picture, start in the background, print the id and return
--list             list the pictures that are showing
--stop ID          stop a picture: an id, a corner of this terminal, or all
```

Durations look like `90`, `90s`, `15m`, `2h` or `1d`.

## How it works

It's graph's way of staying on screen, with a picture in place of a chart. Forty
times a second it re-pins the picture to its corner, so scrolling and `clear` only
move or hide it for a moment. A hidden "virtual" placement keeps the pixels alive
through `clear`, so the re-pin brings it straight back. It sends the pixels again only
when the picture or the window's size changes, and every 10 seconds, which brings it
back after `reset` and inside vim. Each picture goes over in a temporary file the
terminal reads and deletes. Over SSH, where the terminal can't see the file, the
pixels go in the command. [graph's README](../graph/README.md#how-it-works) has the
details.

It sends no more pixels than the picture has, and at most twice what the terminal
reports (sharp on a Retina screen). The terminal stretches it to fit. The cells it
covers are rounded up, and the sliver left over stays transparent, so the picture
keeps its exact shape.

An animation plays from this end, not with the protocol's own animation (which
Ghostty has had since 1.4). Each frame goes over once, as an image of its own, the
first time it's shown. After that, playing it is the same tiny re-pin as always, of a
different frame each time, and the frame before has its placement taken away in the
same write. No pixels are sent to play it, so it can't flicker from re-sending them,
and it works in any terminal with the kitty graphics protocol. The protocol's own
animation would also restart every time the frames went again (every 10 seconds, see
above). Here each frame goes again on its own, just before it's next shown.

## Limitations

- **One picture per place on a terminal**: a new one replaces the old one. A picture and
  a graph can share a corner, but they'll overlap.
- **It rides along when the text scrolls**, for up to 1/40 of a second, then pops back,
  the same as graph.
- **Over SSH a photo is big**: a few hundred KB to a MB or two, sent again every 10
  seconds. An animation sends every frame that often. A smaller `--size` or
  `--density 1` cuts that down.
- **Long animations get softer.** The terminal keeps every frame, so an animation is
  kept to 128 MB there (Ghostty and kitty allow 320 MB for all pictures). A long one
  gets fewer pixels a frame. Past 500 frames, it plays every other one (or every
  third, ...).
- **Animations always loop**, even a GIF that says to play only a few times. Frame
  timing is in steps of 1/40 of a second.
- **Not inside tmux or screen.** They don't pass the kitty graphics protocol through.

## Check

`./check.py` runs the script against fake terminals, with its own private list of
what's showing. Nothing is drawn on your screen. It checks that every command is
quiet, the cursor is always put back, and the image is deleted on exit; that a picture
keeps its shape and is sent again when its file changes; that an animated GIF plays,
each frame sent once, and every frame is deleted at the end; that a URL works; and that
`--detach`, `--list` and `--stop` work, and `--detach` fails on a file it can't read.
