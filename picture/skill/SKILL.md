---
name: terminal-picture
description: Show a picture in a corner of the user's terminal (Ghostty or kitty) and keep it there while they work. That can be an image file, a chart or plot you made, a screenshot, an animated GIF, or an image at a URL, like a webcam or a weather radar. Use when the user wants to see, show, view, preview or pop up an image or picture in their terminal, when they asked to see a chart or image you made, or when they want one taken down.
---

# Terminal picture

`picture.py`, in this skill's base directory, shows a picture in a corner of the terminal
Claude Code is running in, and keeps it there while the user works. Run it by its full
path (below, `picture.py` means that path). It needs `uv`, or `python3` with Pillow.

It covers part of the user's screen, so put one up when they ask to see something, or
when they asked for a chart or image and seeing it is the point.

## Put one up

Always pass `--detach`. Your shell has no terminal of its own, so `--detach` finds the
one Claude Code is running in. It loads the picture first, so a missing file or a bad
URL fails right there with its error, then shows it from the background and returns
with an id.

```sh
picture.py /full/path/to/plot.png --detach
picture.py https://example.com/radar.png --every 5m --detach
```

- **A file:** give its full path. It's watched: write a new picture to the same path and
  the new one shows within a second. So for a chart you'll redraw, put it up once and
  then save over the file. Keep the file where it is while it's up; if it goes away,
  the last picture stays but stops updating.
- **A URL** (http or https) is fetched once. Add `--every 5m` to fetch it again, for a
  webcam, a radar map or a status image.
- PNG, JPEG, GIF, WebP and most else Pillow reads. An animated GIF (or PNG, or WebP)
  plays, over and over. Not SVG or PDF: turn those into a PNG first.
- For a chart you make: the terminal is dark, so a dark background suits it (in
  matplotlib, `plt.style.use("dark_background")`). A wide picture fits a corner better
  than a tall one.
- `--size 40x16` sets the most it covers, in cells (default 60x20); it keeps its shape.
  `--at top-left|top-right|bottom-left|bottom-right|center` (default top-right). If the
  user wants it behind their text instead of over it, add `--behind` (faded;
  `--opacity` to taste).

## After

- Tell the user what you put up, where, and its id. It goes away by itself after 30
  minutes. Use `--for 2h` for longer, or `--for 0` to keep it until it's stopped, but
  only if they ask.
- There's one picture per corner: a new one in the same corner replaces the old one.
  For several at once, use different corners. The top two are best, because the bottom
  ones can cover Claude Code's input box. A graph from the terminal-graph skill in the
  same corner would overlap it, so give them different corners.
- `picture.py --list` shows what's up. `picture.py --stop ID` takes one down (or a
  corner name, or `all`).

## Somewhere else

The user may be following along from another device (Remote Control, the web, a phone),
and the picture only shows on the computer's terminal. Show them the picture itself too.

## When it doesn't work

- **"can't find a terminal"**: Claude Code isn't running in a terminal on this machine,
  as in a cloud session. Show the user the picture another way.
- **It starts, but the user sees nothing**: their terminal doesn't support the kitty
  graphics protocol, or it's running inside tmux or screen, which don't pass it through.
- **`--detach` fails**: the message says why: the file isn't there, it isn't a picture
  Pillow can read, or the URL gave an HTTP error.
