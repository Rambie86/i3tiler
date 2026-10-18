# i3tiler

![i3tiler demo](assets/demo.gif)

A layout manager for i3 (tested on 4.22). It moves windows by **inserting**
them into the neighboring region, compacts free space automatically, and always keeps the layout rules below.

## Rules it guarantees

A binary tree of alternating splits (`model.validate`):

* Opening a window splits the **focused window**: `split vertical` if it is taller than
  wide and `split horizontal` otherwise (ties included). The new window goes
  to its right or below. On 16:9 this gives the spiral `H[A,B]` →
  `H[A,V[B,C]]` → `H[A,V[B,H[C,D]]]`… The 2×2 grid forms when you open a
  window with focus on a full column. The rules win over the dimensions:
  2 windows are always side by side (even on a portrait monitor), and if the
  chosen axis equals the parent's, the other one is used so you never get
  3 columns.
* Every split has exactly 2 children: no empty containers, no redundant
  ones, no 3 siblings.
* A split never contains another split of the same orientation, so there
  are never three sibling columns (or rows).
* With 3 or more windows the root may be vertical (e.g. a split top row and
  one full-width window below). The rules allow it; with 2 windows it never
  happens.

## Moving (`Mod+Shift+h/j/k/l`)

1. Take the focused window (if it is floating, nothing happens).
2. Find the neighbor using i3's real coordinates: adjacency, then largest
   perpendicular overlap, then shortest distance, then top/left and id as
   tie-breakers.
3. Remove the window and compact its source region.
4. Split the target region and insert the window on the side it enters
   from. If that axis would create 3 columns/rows or 2 stacked windows, the
   other axis is used, keeping the relative position.
5. If the target was its direct sibling, they are reordered
   (`H[A,B]` + `A→` = `H[B,A]`).
6. With no valid target (screen edge), nothing changes and focus stays.

Example: `H[V[A,C],V[B,D]]` + A right = `H[C,V[H[A,B],D]]`, and A left puts
it back into exactly the grid.

## Installation

Requirements: i3 4.22+, Python 3.8+ and [`i3ipc`](https://pypi.org/project/i3ipc/).

Install `i3ipc` with your distro's package manager:

Debian / Ubuntu
```sh
sudo apt install python3-i3ipc
```
Arch
```sh
sudo pacman -S python-i3ipc
```
Fedora
```sh
sudo dnf install python3-i3ipc
```


Then clone the repository:

```sh
git clone https://github.com/Rambie86/i3tiler.git
```

Add to your i3 config:

```
bindsym $mod+Shift+h nop i3tiler move left
bindsym $mod+Shift+j nop i3tiler move down
bindsym $mod+Shift+k nop i3tiler move up
bindsym $mod+Shift+l nop i3tiler move right

exec_always --no-startup-id /path/to/i3tiler/run.sh
```

Only one instance runs per i3 session: every reload replaces the previous
daemon.

**Run only one layout manager.** Other tools that rearrange windows
automatically (changing split orientation on focus, or switching layouts)
will fight with i3tiler. Remove them from your config and stop any instance
still running before reloading i3.

On startup the daemon normalizes every workspace (for example, it removes
redundant containers).

### Usage and diagnostics

```sh
python3 -m i3tiler dump                  # real vs logical tree (read-only)
python3 -m i3tiler send pause|resume|toggle|reconcile
tail -f ~/.cache/i3tiler/i3tiler.log
```

Run these from the repo folder, or use the `i3tiler` command if you
installed it with pip.

To leave a workspace unmanaged, pass `--exclude <name>` (repeatable), e.g.
`exec_always --no-startup-id /path/to/i3tiler/run.sh --exclude 10`.

## Architecture

| Module | Responsibility |
|---|---|
| `model.py` | Logical tree, compaction (`normalize`), validation |
| `geometry.py` | Rectangles and geometric neighbor selection |
| `ops.py` | Open, close and move-by-insertion (pure, no i3) |
| `i3state.py` | Reads the i3 tree → model (floating windows apart) |
| `realize.py` | Applies to i3 with a single IPC command batch |
| `local.py` | Minimal local plans plus a simulator that checks them |
| `daemon.py` | Events (`window`, `workspace`, `binding`, `tick`) and reconciliation |

### Applying changes without closing windows

`append_layout` only works for new windows: it creates placeholders that
"swallow" a window when it is mapped, so with windows already open it would
require unmapping them. It is not used.

Measured with `xev` on real i3 4.22:

* Inside a command batch, `split`, `move container to mark`, `focus` and
  `layout` do not redraw.
* **When a container becomes empty, i3 closes it and redraws right then.**
  That is why the first version, which flattened and rebuilt everything,
  made each window go through 2–3 intermediate sizes (kitty then shows
  "106 by 58 cells").
* `[con_id=X] layout split*` changes X's **parent**. On a workspace with
  children it creates a redundant container.
* `move container to mark <workspace>` descends into the workspace's
  focused child. If that child is a floating window, i3 loses the moved
  window.

So `local.py` applies only the local change, in this order:

1. `split` the target and move the window. The source is left with a
   single child and nothing gets closed.
2. `layout` on the containers that change orientation.
3. Finally, remove single-child containers. Such a container takes up
   exactly the same space as its child, so the redraw its closing triggers
   already shows the final geometry.

Before sending anything, the plan runs in a simulator of those primitives,
which checks that every intermediate redraw matches the final geometry.
Result: **each window is resized at most once, and only if its final size
changes**. If a change has no local plan (rotating the workspace itself, foreign
trees), the full rebuild in `realize.py` is used. After applying, the tree
is read back and verified.

A single root container under the workspace (workspace → K) is allowed. It
takes exactly the same space as the workspace, and thanks to it the root can
be rotated without intermediate redraws.

## Known limitations

* **"106 by 58 cells" in kitty**: kitty draws this placeholder while it
  waits `resize_debounce_time` (0.1 s by default) before redrawing after a
  resize. i3tiler only resizes the windows that need it, once each; those
  will still show it for an instant. To remove it, set
  `resize_debounce_time 0 0` in `kitty.conf`.
* **New windows**: i3 maps them where it wants first and i3tiler moves them
  right after. A brief flash may be visible.
* **Internal focus history**: after a rebuild, the window that gets focus
  when entering a container with `Mod+h/j/k/l` may not be the last one you
  used inside it.
* **Tabbed/stacked and fullscreen**: a workspace with tabbed/stacked
  containers or a fullscreen window is left alone until it is a normal split
  again.
* **Unfocused workspaces**: if a change needs to move focus (rotating the
  workspace, or a floating window on top of the focus stack and no direct
  window), it is deferred until you visit that workspace.
* **Native commands**: `Mod+Shift+arrows` (native move) and `Mod+b/v/e`
  still belong to i3. i3tiler normalizes the result on the next event,
  which can give unexpected results. Recommendation: point the arrows to
  `nop i3tiler move ...` as well.
* **Daemon not running**: `Mod+Shift+h/j/k/l` do nothing (`nop`).

## License

[MIT](LICENSE)
