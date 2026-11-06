# background

Puts a slow animated plasma, or a picture, behind the text in your terminal
while you keep working. It runs as an ordinary background job, so there's no
terminal config to change, and it works over SSH.

It needs a terminal that supports the
[kitty graphics protocol](https://sw.kovidgoyal.net/kitty/graphics-protocol/).
It was made for [Ghostty](https://ghostty.org); [kitty](https://sw.kovidgoyal.net/kitty/)
should work too.

## Run it

With [uv](https://docs.astral.sh/uv/), straight from GitHub, pinned to a commit:

```sh
uv run https://raw.githubusercontent.com/misterek/terminal-fun/<commit>/background/background.py &
```

Replace `<commit>` with the full hash of a commit you've looked at, so you run
exactly that code even if the repo changes later. uv installs the two
dependencies (numpy and Pillow) into a throwaway environment the first time.

Or from a clone:

```sh
git clone https://github.com/misterek/terminal-fun
cd terminal-fun/background
./background.py &
```

Without uv, install the dependencies yourself (`pip install numpy pillow`) and
run `python3 background.py &`.

Stop it with `kill %1` (or `kill` and its process id). It removes its image on
the way out.

## Options

```
--color COLOR   plasma color: rainbow (the default), red, orange, yellow, green,
                teal, blue, purple, pink, or a hex color like ff8800
--image PATH    show a picture instead of the plasma
--opacity N     1 is solid; lower fades it into your terminal's background
                (default 0.25)
--tty DEVICE    draw on another terminal; run `tty` there to get its name
--fps N         redraw rate (default 8 for the plasma, 4 for a picture)
```

For example:

```sh
./background.py --color teal &
./background.py --color ff8800 --opacity 0.4 &
./background.py --image ~/Pictures/beach.jpg &
```

Fading uses transparency rather than darkening, so it works on light and dark
themes alike. A picture is cropped to fill the window and is never sent larger
than 1280 pixels wide.

## How it works

The script draws an image with the kitty graphics protocol at a z-index below
the text, stretched over the whole window. Images in that protocol are pinned
to cells, not to the window, so they scroll away with your output and `clear`
deletes them. The script re-pins the image several times a second, so it is
always back within a moment.

Drawing from a background job takes some care:

- It never reads from the terminal or changes its settings, because a
  background job that does gets stopped. Every command asks the terminal not
  to reply (`q=2`); otherwise the replies would show up at your prompt as if
  you had typed them.
- Each frame goes out in a single write, and saves and restores the cursor
  around itself, so the cursor you're typing at never moves.
- The plasma is a tiny image (96 pixels wide) that the terminal stretches and
  smooths. That keeps it to about 70 KB/s. A picture is sent once, and after
  that each update is about 60 bytes.

## Limitations

- **macOS, nonstop output:** while a lot of output scrolls by without a break
  (a big `cat`, a long build log), about 1 line in 1,000 can go missing from
  the screen. macOS can let other output land in the middle of a frame, and
  the terminal then swallows it along with the frame. Normal interactive use
  lost nothing in testing. Linux keeps each write whole, so this doesn't happen
  on a Linux machine, including one you SSH into from a Mac.
- **Saved cursor position:** each frame saves and restores the cursor. Some
  programs (a few progress bars, for example) save and restore it too, and if a
  frame lands in between, that program can end up drawing in the wrong place.
- **Full-screen apps:** inside vim, less and the like, a picture can take up to
  20 seconds to appear. They use a separate screen, which has its own images.
- **One at a time:** two copies on the same terminal fight over the same image.

## Check

`./check.py` runs the script against a fake terminal, so nothing is drawn on
your screen. For each mode it checks that every command is quiet, that the
cursor is always put back, and that the image is deleted on exit. It also
checks that a picture is re-sent after a resize, and that the script exits
quietly when its terminal closes.
