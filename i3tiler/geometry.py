"""Geometry: model rectangles and directional neighbor selection."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Hashable, Optional

from .model import H, Leaf, Node

DIRECTIONS = ('left', 'right', 'up', 'down')


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float

    @property
    def x2(self):
        return self.x + self.w

    @property
    def y2(self):
        return self.y + self.h

    @property
    def cx(self):
        return self.x + self.w / 2

    @property
    def cy(self):
        return self.y + self.h / 2


DEFAULT_AREA = Rect(0, 0, 1366, 768)


def layout_rects(node: Optional[Node], area: Rect = DEFAULT_AREA) -> Dict[Hashable, Rect]:
    """Rectangle of each leaf if every split divides 50/50 (as i3 does after
    a rebuild)."""
    out: Dict[Hashable, Rect] = {}

    def walk(n, r):
        if isinstance(n, Leaf):
            out[n.id] = r
            return
        k = len(n.children)
        for i, c in enumerate(n.children):
            if n.orient == H:
                walk(c, Rect(r.x + r.w * i / k, r.y, r.w / k, r.h))
            else:
                walk(c, Rect(r.x, r.y + r.h * i / k, r.w, r.h / k))

    if node is not None:
        walk(node, area)
    return out


def find_neighbor(rects: Dict[Hashable, Rect], src: Hashable, direction: str,
                  adjacency: float = 40.0, eps: float = 2.0) -> Optional[Hashable]:
    """Target window in ``direction`` starting from ``src``.

    Only windows lying entirely in that direction and overlapping ``src``
    on the perpendicular axis are candidates. Priority:

    1. adjacent ones (edge gap <= ``adjacency``, covers gaps/borders);
    2. largest perpendicular overlap;
    3. smallest edge gap, then smallest distance between centers;
    4. deterministic tie-break: top/left first, then the id.
    """
    if direction not in DIRECTIONS:
        raise ValueError(direction)
    s = rects[src]
    best_key, best = None, None
    for wid, r in rects.items():
        if wid == src:
            continue
        if direction == 'left':
            ok, gap = r.x2 <= s.x + eps, s.x - r.x2
        elif direction == 'right':
            ok, gap = r.x >= s.x2 - eps, r.x - s.x2
        elif direction == 'up':
            ok, gap = r.y2 <= s.y + eps, s.y - r.y2
        else:
            ok, gap = r.y >= s.y2 - eps, r.y - s.y2
        if not ok:
            continue
        if direction in ('left', 'right'):
            overlap = min(s.y2, r.y2) - max(s.y, r.y)
            tie = (r.y, r.x)
        else:
            overlap = min(s.x2, r.x2) - max(s.x, r.x)
            tie = (r.x, r.y)
        if overlap <= eps:
            continue
        gap = max(gap, 0.0)
        center = abs(r.cx - s.cx) + abs(r.cy - s.cy)
        key = (0 if gap <= adjacency else 1, -round(overlap, 3), round(gap, 3),
               round(center, 3), tie, str(wid))
        if best_key is None or key < best_key:
            best_key, best = key, wid
    return best
