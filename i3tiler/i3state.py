"""Reads the i3 tree (``i3ipc.Con`` objects) into the logical model.

It changes nothing. Floating windows are recorded only to check afterwards
that they were not touched; they never enter the model.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .geometry import Rect
from .model import H, V, Leaf, Node, Split

ORIENT = {'splith': H, 'splitv': V}


@dataclass
class Workspace:
    id: int
    name: str
    raw: Optional[Node]                 # exact i3 structure (may be "dirty")
    orient: Optional[str]               # orientation of the workspace itself
    tiling: List[int]                   # tiling windows in tree order
    floating: List[int]                 # floating windows (never touched)
    rects: Dict[int, Rect]              # real coordinates of each tiling window
    parent: Dict[int, int]              # window -> parent container id
    recency: List[int]                  # tiling windows, most to least recently focused
    focus_first_tiling: bool            # is the first child in the focus stack tiling?
    area: Rect
    managed: bool = True
    reason: str = ''
    marks: Dict[int, list] = field(default_factory=dict)
    shape: Optional[dict] = None        # tree with container ids: {id, kind, orient, children}


@dataclass
class Snapshot:
    workspaces: Dict[int, Workspace]
    by_name: Dict[str, Workspace]
    focused_window: Optional[int]       # con_id of the focused window (tiling or floating)
    focused_ws: Optional[int]
    all_windows: set                    # every window in the tree (safety check)
    window_ws: Dict[int, int]           # window -> workspace


def _rect(r) -> Rect:
    return Rect(r.x, r.y, r.width, r.height)


def _is_window(con) -> bool:
    return con.window is not None


def read(root, exclude=()) -> Snapshot:
    """Build a ``Snapshot`` from ``i3.get_tree()``."""
    workspaces: Dict[int, Workspace] = {}
    all_windows: set = set()
    window_ws: Dict[int, int] = {}
    focused = root.find_focused()
    focused_window = focused.id if focused is not None and _is_window(focused) else None
    focused_ws = None
    if focused is not None:
        ws = focused if focused.type == 'workspace' else focused.workspace()
        focused_ws = ws.id if ws is not None else None

    def collect_all(con):
        if _is_window(con):
            all_windows.add(con.id)
        for c in list(con.nodes) + list(con.floating_nodes):
            collect_all(c)

    collect_all(root)

    for ws in root.workspaces():
        if ws.name.startswith('__') or ws.name in exclude:
            continue
        info = _read_ws(ws)
        workspaces[ws.id] = info
        for w in info.tiling + info.floating:
            window_ws[w] = ws.id
    return Snapshot(workspaces, {w.name: w for w in workspaces.values()},
                    focused_window, focused_ws, all_windows, window_ws)


def _read_ws(ws) -> Workspace:
    tiling: List[int] = []
    rects: Dict[int, Rect] = {}
    parent: Dict[int, int] = {}
    marks: Dict[int, list] = {}
    problems: List[str] = []

    def raw(con, parent_id, out):
        if _is_window(con):
            out.append({'id': con.id, 'kind': 'win', 'orient': None, 'children': []})
            tiling.append(con.id)
            rects[con.id] = _rect(con.rect)
            parent[con.id] = parent_id
            marks[con.id] = list(con.marks or [])
            if con.fullscreen_mode:
                problems.append('fullscreen window')
            return Leaf(con.id)
        if con.layout not in ORIENT:
            problems.append(f'{con.layout} container')
            orient = H
        else:
            orient = ORIENT[con.layout]
        if not con.nodes and con.type == 'con':
            problems.append('container without a window (placeholder)')
        node = {'id': con.id, 'kind': 'ws' if con.type == 'workspace' else 'con',
                'orient': orient, 'children': []}
        out.append(node)
        return Split(orient, tuple(raw(c, con.id, node['children']) for c in con.nodes))

    holder: list = []
    tree = raw(ws, ws.id, holder)
    floating: List[int] = []
    for fc in ws.floating_nodes:
        for leaf in fc.leaves():
            floating.append(leaf.id)

    by_id = {c.id: c for c in ws.descendants()}
    recency: List[int] = []

    def rec(con):
        order = [by_id[i] for i in con.focus if i in by_id and by_id[i] in con.nodes]
        order += [c for c in con.nodes if c not in order]
        for c in order:
            if _is_window(c):
                recency.append(c.id)
            else:
                rec(c)

    rec(ws)
    floating_ids = {fc.id for fc in ws.floating_nodes}
    first_tiling = bool(ws.focus) and ws.focus[0] not in floating_ids

    return Workspace(
        id=ws.id, name=ws.name, raw=tree, orient=ORIENT.get(ws.layout),
        tiling=tiling, floating=floating, rects=rects, parent=parent,
        recency=recency, focus_first_tiling=first_tiling, area=_rect(ws.rect),
        managed=not problems, reason='; '.join(sorted(set(problems))), marks=marks,
        shape=holder[0])
