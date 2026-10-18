"""Layout operations: opening, closing (compaction) and move by insertion.
All of them return normalized, valid trees; they never mutate."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Hashable, Optional

from .geometry import DEFAULT_AREA, Rect, find_neighbor, layout_rects
from .model import (H, V, Leaf, Node, Split, compact, leaves, normalize, parent_of, perp,
                    replace_leaf, validate, without)


def remove(tree: Optional[Node], wid: Hashable) -> Optional[Node]:
    """Extract a window and compact the space it leaves."""
    return normalize(without(tree, [wid]))


def autosplit_orient(rect: Rect) -> str:
    """Orientation when opening a window: split vertical if the focused
    window is taller than wide; otherwise (ties included), horizontal."""
    return V if rect.h > rect.w else H


def insert_new(tree: Optional[Node], wid: Hashable, focused: Hashable = None,
               area: Rect = DEFAULT_AREA) -> Node:
    return insert_new_ex(tree, wid, focused, area)[0]


def insert_new_ex(tree, wid, focused=None, area: Rect = DEFAULT_AREA):
    """Like ``insert_new`` but returns ``(tree, action)``; the action is an
    ``Insert``, or None for the first window."""
    """Place a new window: split the focused window according to its
    dimensions and put the new one to its right/below.

    On 16:9 this gives the usual spiral: ``A`` -> ``H[A,B]`` ->
    ``H[A,V[B,C]]`` -> ``H[A,V[B,H[C,D]]]``...

    The layout rules take precedence over the dimensions:

    * if the chosen axis matches the parent's (it would create three
      sibling columns/rows), the other one is used;
    * two windows are always side by side (e.g. on a portrait monitor).

    Without a valid focused window, the largest region is split.
    """
    if tree is None:
        return Leaf(wid), None
    rects = layout_rects(tree, area)
    if focused in rects:
        target = focused
    else:
        target = min(rects, key=lambda i: (-round(rects[i].w * rects[i].h, 3),
                                           round(rects[i].y, 3), round(rects[i].x, 3), str(i)))
    parent = parent_of(tree, target)
    orient = autosplit_orient(rects[target])
    if parent is None:
        orient = H
    elif orient == parent.orient:
        orient = perp(orient)
    new = normalize(replace_leaf(tree, target, Split(orient, (Leaf(target), Leaf(wid)))))
    return new, Insert(wid, target, orient, False)


@dataclass(frozen=True)
class Insert:
    """Elementary action: ``src`` comes to share ``target``'s region, split
    with ``orient``; ``src_first`` = src on the left/top."""
    src: Hashable
    target: Hashable
    orient: str
    src_first: bool


def move(tree: Optional[Node], wid: Hashable, direction: str,
         rects: Optional[Dict[Hashable, Rect]] = None) -> Optional[Node]:
    result = move_ex(tree, wid, direction, rects)
    return result[0] if result else None


def move_ex(tree, wid, direction, rects=None):
    """Like ``move`` but returns ``(tree, Insert)`` or None."""
    """Move ``wid`` by insertion towards ``direction``.

    1. Geometric neighbor T in that direction (real coordinates if given).
    2. The window is extracted and its source region is compacted.
    3. T's region is subdivided and the window is inserted on the side it
       enters from (moving right puts it to the left of T, etc.). If the
       move axis is not allowed there (it would create three sibling
       columns/rows or two stacked windows), the other axis is used,
       keeping the original relative position.
    4. If the result is identical to the starting tree (T was its direct
       sibling), they are reordered: the window goes to the other side of T.

    Returns None when there is no target or no variant is valid.
    """
    if tree is None or wid not in leaves(tree):
        return None
    if rects is None:
        rects = layout_rects(tree)
    rects = {k: v for k, v in rects.items() if k in set(leaves(tree))}
    target = find_neighbor(rects, wid, direction)
    if target is None:
        return None
    base = compact(without(tree, [wid]))
    axis = H if direction in ('left', 'right') else V
    parent = parent_of(base, target)
    if parent is None:
        allowed = H if len(leaves(base)) == 1 else axis
    else:
        allowed = perp(parent.orient)
    if allowed == axis:
        orient, first = axis, direction in ('right', 'down')
    else:
        orient = allowed
        s, t = rects[wid], rects[target]
        first = (s.cx <= t.cx) if orient == H else (s.cy <= t.cy)
    for src_first in (first, not first):
        kids = (Leaf(wid), Leaf(target)) if src_first else (Leaf(target), Leaf(wid))
        new = normalize(replace_leaf(base, target, Split(orient, kids)))
        if new != tree and not validate(new) and sorted(map(str, leaves(new))) == sorted(map(str, leaves(tree))):
            return new, Insert(wid, target, orient, src_first)
    return None
