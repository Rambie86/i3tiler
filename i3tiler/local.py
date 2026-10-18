"""*Local* application of changes: a single resize per window.

Measured on i3 4.22 with ``xev``: inside a command batch, ``split``,
``move container to mark``, ``focus`` and ``layout`` do not redraw. But when
a container becomes empty and i3 closes it, **i3 redraws right then**.
Flattening and rebuilding empties every old container, so each window went
through 2-3 intermediate sizes (kitty shows them as "106 by 58 cells").

This module only makes the local change, in this order:

1. ``split`` the target and move the window (nothing is emptied: the source
   is left with a single child).
2. ``layout`` on the containers that change orientation.
3. Finally, remove single-child containers. A redundant container takes up
   exactly the same space as its child, so the redraw its closing triggers
   already shows the final geometry.

Everything runs first on a simulator of those primitives. If the simulator
does not reach exactly the target tree, ``NotLocal`` is raised and the
caller uses the full rebuild (``realize.plan``).
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import List, Optional

from .geometry import layout_rects
from .model import Leaf, Split, leaves

SPLIT = {'h': 'horizontal', 'v': 'vertical'}
_new_ids = itertools.count(1)


class NotLocal(Exception):
    """The change cannot be expressed locally: use a rebuild."""


@dataclass(eq=False)
class SNode:
    id: object
    kind: str                     # 'ws' | 'con' | 'win'
    orient: Optional[str]
    children: list = field(default_factory=list)
    parent: Optional['SNode'] = None
    new: bool = False             # created in this batch: its real id is unknown


class Sim:
    """Simulator of the i3 primitives in use, with redraw points."""

    def __init__(self, shape: dict):
        self.index = {}
        self.root = self._build(shape, None)
        self.renders: List[dict] = []

    def _build(self, d, parent):
        n = SNode(d['id'], d['kind'], d['orient'], [], parent)
        self.index[n.id] = n
        n.children = [self._build(c, n) for c in d['children']]
        return n

    # -------------------------------------------------------------- queries
    def node(self, i) -> SNode:
        return self.index[i]

    def leaves(self, n: SNode) -> list:
        if n.kind == 'win':
            return [n.id]
        return [w for c in n.children for w in self.leaves(c)]

    def model(self, n: Optional[SNode] = None):
        n = n or self.root
        if n.kind == 'win':
            return Leaf(n.id)
        return Split(n.orient, tuple(self.model(c) for c in n.children))

    def containers(self):
        out = []

        def walk(n, depth):
            if n.kind != 'win':
                out.append((depth, n))
                for c in n.children:
                    walk(c, depth + 1)
        walk(self.root, 0)
        return out

    # ----------------------------------------------------------- primitives
    def split(self, wid, orient):
        w = self.node(wid)
        p = w.parent
        if len(p.children) == 1:
            p.orient = orient                      # i3: only the parent changes
            return
        new = SNode(('new', next(_new_ids)), 'con', orient, [w], p, new=True)
        self.index[new.id] = new
        p.children[p.children.index(w)] = new
        w.parent = new

    def move_after(self, xid, anchor_id):
        """``[con_id=x] move container to mark <anchor>`` with anchor = window."""
        x, a = self.node(xid), self.node(anchor_id)
        assert a.kind == 'win' and x is not a
        self._attach_after(x, a)

    def move_after_child(self, xid, container_id, child_id):
        """``move container to mark <container>`` when the container's focused
        child is ``child``: x ends up right after child."""
        x, c, child = self.node(xid), self.node(container_id), self.node(child_id)
        assert child.parent is c
        self._attach_after(x, child)

    def _attach_after(self, x, anchor):
        n = anchor
        while n is not None:
            if n is x:
                raise NotLocal('moving a container into itself')
            n = n.parent
        old = x.parent
        old.children.remove(x)
        p = anchor.parent
        p.children.insert(p.children.index(anchor) + 1, x)
        x.parent = p
        self._on_remove(old)

    def _on_remove(self, con):
        if con.kind == 'con' and not con.children:
            par = con.parent
            par.children.remove(con)
            del self.index[con.id]
            self._on_remove(par)
            self.renders.append(layout_rects(self.model()))   # i3 redraws here

    def layout(self, child_id, orient):
        """``[con_id=child] layout split*``: on i3 4.22 it changes the PARENT of
        ``child`` (verified; on a workspace it would create a container)."""
        p = self.node(child_id).parent
        assert p.kind == 'con'
        p.orient = orient


def conforms(sim_model, target) -> bool:
    """Structure == target. A single root container under the workspace
    (ws -> K) is allowed; it takes up the same space as the workspace."""
    if target is None:
        return not sim_model.children
    if isinstance(target, Leaf):
        return sim_model.children == (target,)
    if sim_model == target:
        return True
    return (len(sim_model.children) == 1 and isinstance(sim_model.children[0], Split)
            and sim_model.children[0] == target)


class Planner:
    def __init__(self, ws, ws_focused: bool, tag: str):
        if ws.shape is None:
            raise NotLocal('no shape')
        self.ws = ws
        self.sim = Sim(ws.shape)
        self.ws_focused = ws_focused
        self.tag = tag
        self.cmds: List[str] = []
        self.marked = set()
        self.extra_marks: List[str] = []

    def mark(self, wid) -> str:
        m = f'{self.tag}{wid}'
        if wid not in self.marked:
            self.cmds.append(f'[con_id={wid}] mark --add {m}')
            self.marked.add(wid)
        return m

    def move_after(self, x, anchor):
        self.cmds.append(f'[con_id={x}] move container to mark {self.mark(anchor)}')
        self.sim.move_after(x, anchor)

    def relayout(self, k, orient):
        child = next((c for c in k.children if not c.new), None)
        if child is None:
            raise NotLocal('container without a child of known id')
        self.cmds.append(f'[con_id={child.id}] layout split{orient}')
        self.sim.layout(child.id, orient)

    # ------------------------------------------------------------ insertion
    def insert(self, act, target_tree):
        """``act.src`` comes to share ``act.target``'s region."""
        sim = self.sim
        s, t = sim.node(act.src), sim.node(act.target)
        if len(leaves(target_tree)) == 2:
            # Two windows: just reorder inside the root container.
            if s.parent is not t.parent:
                raise NotLocal('two windows in different containers')
            want_first = act.src if act.src_first else act.target
            if s.parent.children[0].id != want_first:
                other = act.target if want_first == act.src else act.src
                self.move_after(other, want_first)
            return
        p = t.parent
        if s.parent is p and len(p.children) == 2:
            # Target = direct sibling: the region is already the pair's; only
            # orientation and order (avoids creating a container of unknown id).
            if p.orient != act.orient:
                if p.kind != 'con':
                    raise NotLocal('rotate the workspace')
                self.relayout(p, act.orient)
            if act.src_first and p.children[0] is not s:
                self.move_after(act.target, act.src)
            elif not act.src_first and p.children[0] is s:
                self.move_after(act.src, act.target)
            return
        if len(p.children) < 2:
            raise NotLocal('target without siblings')
        self.cmds.append(f'[con_id={act.target}] split {SPLIT[act.orient]}')
        sim.split(act.target, act.orient)
        self.move_after(act.src, act.target)
        if act.src_first:
            self.move_after(act.target, act.src)

    # -------------------------------------------------------------- repairs
    def repair(self, target):
        sim = self.sim
        tnodes = {}

        def index(n):
            tnodes[frozenset(leaves(n))] = n
            if isinstance(n, Split):
                for c in n.children:
                    index(c)
        if target is not None:
            index(target)

        # 1. orientation and order of every container with 2+ children
        for _, k in sim.containers():
            if len(k.children) < 2:
                continue
            n = tnodes.get(frozenset(sim.leaves(k)))
            if not isinstance(n, Split) or len(n.children) != len(k.children):
                raise NotLocal(f'container {k.id} has no counterpart')
            if [frozenset(sim.leaves(c)) for c in k.children] != \
                    [frozenset(leaves(c)) for c in n.children]:
                raise NotLocal(f'different order in {k.id}')
            if k.orient != n.orient:
                if k.kind != 'con':
                    raise NotLocal('rotate the workspace')
                self.relayout(k, n.orient)

        # 2. remove single-child containers (innermost first)
        while True:
            redundant = [(d, k) for d, k in sim.containers() if k.kind == 'con' and len(k.children) == 1]
            if not redundant:
                break
            _, p = max(redundant, key=lambda x: x[0])
            x, g = p.children[0], p.parent
            if x.new or g.new:
                raise NotLocal('new container involved')
            if not self.ws_focused:
                raise NotLocal('focus must move on an unfocused workspace')
            leaf = sim.leaves(x)[0]
            gm = f'{self.tag}g{g.id}'
            if gm not in self.extra_marks:
                self.cmds.append(f'[con_id={g.id}] mark --add {gm}')
                self.extra_marks.append(gm)
            # With focus on a window of p, g's focused child is p, so x ends
            # up right after p and p closes once it is empty.
            self.cmds.append(f'[con_id={leaf}] focus')
            self.cmds.append(f'[con_id={x.id}] move container to mark {gm}')
            sim.move_after_child(x.id, g.id, p.id)

        if not conforms(sim.model(), target):
            raise NotLocal(f'simulated result {sim.model()!r} != {target!r}')

    def finish(self, focus_id) -> List[str]:
        if self.ws_focused and focus_id is not None:
            self.cmds.append(f'[con_id={focus_id}] focus')
        self.cmds += [f'unmark {self.tag}{w}' for w in sorted(self.marked, key=str)]
        self.cmds += [f'unmark {m}' for m in self.extra_marks]
        return self.cmds


def plan_local(ws, target, actions, focus_id, ws_focused, tag) -> List[str]:
    """Commands to go from the current structure of ``ws`` to ``target``,
    applying ``actions`` (a list of ``ops.Insert``) and repairing the rest."""
    pl = Planner(ws, ws_focused, tag)
    for act in actions:
        pl.insert(act, target)
    pl.repair(target)
    cmds = pl.finish(focus_id)
    final = layout_rects(pl.sim.model())
    for r in pl.sim.renders:
        if r != final:
            raise NotLocal('an intermediate redraw would show a different geometry')
    return cmds
