"""Applies a logical tree to i3 without closing or re-mapping windows.

i3 does not allow editing its tree directly over IPC, and ``append_layout``
only works for *new* windows (placeholders that "swallow" windows when they
are mapped), so the structure is rebuilt with primitives whose semantics
were verified empirically on i3 4.22:

* ``[con_id=W] move container to mark M``: if M marks a window, W ends up
  right after it in the same parent. If M marks a container or the
  workspace, i3 descends into its focused child (if that child is floating,
  i3 4.22 "loses" the moved window: avoided by design).
* ``[con_id=W] split h|v``: if W has siblings, creates a new container
  holding only W, with that orientation. If it is an only child, it just
  changes the parent's orientation.
* Containers that become empty are closed by i3 automatically.
* ``layout splith|splitv`` on a workspace with children creates a redundant
  container; to rotate the workspace, ``split`` is used on its only child.

Algorithm (a single command batch, hence a single render, no flicker):

1. Flatten: every tiling window becomes a direct child of the workspace
   (the old containers are emptied and disappear).
2. If the target root has a different orientation than the workspace,
   rotate it.
3. Build top-down: for ``Split(o, (L, R))`` with first leaf ``a`` of L and
   ``b`` of R, ``split o`` on ``a`` (except at the root, which is the
   workspace itself) and ``move b to mark a``. Recurse into L and R.
4. Restore focus and remove the temporary marks.
"""
from __future__ import annotations

import os
from typing import List, Optional

from .i3state import Workspace
from .model import H, Leaf, Node, Split, first_leaf, leaves

SPLIT = {H: 'horizontal', 'v': 'vertical'}


class Deferred(Exception):
    """The change needs to move focus on an unfocused workspace; it is
    deferred until that workspace gets focus."""


def conforming(ws: Workspace, target: Optional[Node]) -> bool:
    """Is the real i3 structure exactly ``target``? (allows a single root
    container under the workspace, see ``local.conforms``)"""
    from .local import conforms
    return conforms(ws.raw, target)


def plan(ws: Workspace, target: Node, focus_id: Optional[int], ws_focused: bool,
         tag: Optional[str] = None) -> List[str]:
    tag = tag or f'_i3tiler{os.getpid()}_'
    windows = leaves(target)
    if sorted(windows) != sorted(ws.tiling):
        raise ValueError('the target tree does not contain exactly the tiling windows of the workspace')

    def mk(w):
        return f'{tag}{w}'

    cmds: List[str] = [f'[con_id={w}] mark --add {mk(w)}' for w in windows]
    marks = [mk(w) for w in windows]

    # 1. Flatten onto a window that is already a direct child of the workspace.
    direct = [w for w in ws.tiling if ws.parent[w] == ws.id]
    if direct:
        pivot = direct[0]
    else:
        pivot = windows[0]
        if not ws.focus_first_tiling:
            if not ws_focused:
                raise Deferred('unfocused workspace with a floating window on top of the focus stack')
            cmds.append(f'[con_id={pivot}] focus')
        cmds.append(f'[con_id={ws.id}] mark --add {tag}ws')
        marks.append(f'{tag}ws')
        cmds.append(f'[con_id={pivot}] move container to mark {tag}ws')
    for w in ws.tiling:
        if w != pivot and ws.parent[w] != ws.id:
            cmds.append(f'[con_id={w}] move container to mark {mk(pivot)}')

    # 2. Rotate the workspace if needed (without leaving a redundant container).
    if isinstance(target, Split) and target.orient != ws.orient:
        if not ws_focused:
            raise Deferred('rotating the workspace orientation requires focus')
        o = SPLIT[target.orient]
        others = [w for w in windows if w != pivot]
        cmds.append(f'[con_id={pivot}] split {o}')           # W1 = [pivot]
        cmds += [f'[con_id={w}] move container to mark {mk(pivot)}' for w in others]
        cmds += [f'[con_id={pivot}] focus', 'focus parent', f'split {o}']   # ws = o[W1]
        if f'{tag}ws' not in marks:
            cmds.append(f'[con_id={ws.id}] mark --add {tag}ws')
            marks.append(f'{tag}ws')
        cmds.append(f'[con_id={pivot}] move container to mark {tag}ws')
        cmds += [f'[con_id={w}] move container to mark {mk(pivot)}' for w in others]

    # 3. Build.
    def build(node, root):
        if isinstance(node, Leaf):
            return
        left, right = node.children
        a, b = first_leaf(left), first_leaf(right)
        if not root:
            cmds.append(f'[con_id={a}] split {SPLIT[node.orient]}')
        cmds.append(f'[con_id={b}] move container to mark {mk(a)}')
        build(left, False)
        build(right, False)

    build(target, True)

    # 4. Focus and cleanup.
    if ws_focused and focus_id is not None:
        cmds.append(f'[con_id={focus_id}] focus')
    cmds += [f'unmark {m}' for m in marks]
    return cmds
