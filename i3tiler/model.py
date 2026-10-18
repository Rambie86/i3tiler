"""Logical layout representation: a binary tree of alternating splits.

A node is either ``Leaf(id)`` (a window) or ``Split(orient, (a, b))``.

Invariants of a valid tree (see ``validate``):

* Every ``Split`` has exactly two children (no empty ones, no redundant
  ones, no three siblings).
* A ``Split`` child never has the same orientation as its parent. So
  ``H[H[A,B],C]`` (three visual columns) is impossible: each region is
  split in two, and each half, if split, is split along the other axis
  (recursive 2x2 grid).
* With exactly two windows the root is horizontal (side by side).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Hashable, Optional, Union

H = 'h'
V = 'v'


def perp(orient: str) -> str:
    return V if orient == H else H


@dataclass(frozen=True)
class Leaf:
    id: Hashable

    def __repr__(self) -> str:
        return str(self.id)


@dataclass(frozen=True)
class Split:
    orient: str
    children: tuple

    def __repr__(self) -> str:
        return f"{self.orient.upper()}[{','.join(map(repr, self.children))}]"


Node = Union[Leaf, Split]


# ------------------------------------------------------------------ queries

def leaves(node: Optional[Node]) -> list:
    if node is None:
        return []
    if isinstance(node, Leaf):
        return [node.id]
    out = []
    for child in node.children:
        out.extend(leaves(child))
    return out


def first_leaf(node: Node) -> Hashable:
    while isinstance(node, Split):
        node = node.children[0]
    return node.id


def parent_of(node: Optional[Node], wid: Hashable) -> Optional[Split]:
    """Split that directly contains leaf ``wid`` (None if it is the root)."""
    if not isinstance(node, Split):
        return None
    for child in node.children:
        if isinstance(child, Leaf) and child.id == wid:
            return node
        found = parent_of(child, wid)
        if found is not None:
            return found
    return None


def replace_leaf(node: Node, wid: Hashable, new: Node) -> Node:
    if isinstance(node, Leaf):
        return new if node.id == wid else node
    return Split(node.orient, tuple(replace_leaf(c, wid, new) for c in node.children))


def without(node: Optional[Node], ids) -> Optional[Node]:
    """Remove leaves WITHOUT normalizing (may leave empty or single-child Splits)."""
    ids = set(ids)
    if node is None:
        return None
    if isinstance(node, Leaf):
        return None if node.id in ids else node
    kids = tuple(k for k in (without(c, ids) for c in node.children) if k is not None)
    return Split(node.orient, kids)


def transpose(node: Node) -> Node:
    if isinstance(node, Leaf):
        return node
    return Split(perp(node.orient), tuple(transpose(c) for c in node.children))


# --------------------------------------------------------------- compaction

def compact(node: Optional[Node]) -> Optional[Node]:
    """``normalize`` without the two-window root rule (intermediate state of
    a move: the extracted window is about to be reinserted)."""
    return _norm(node)


def normalize(node: Optional[Node]) -> Optional[Node]:
    """Compact any tree (even an arbitrary one read from i3).

    * Removes empty containers.
    * Replaces single-child containers with that child (the surviving
      sibling takes over the free region).
    * If a node moved up ends with the same orientation as its new parent,
      its subtree is transposed: its children are redistributed inside the
      region along the other axis instead of forming three sibling
      columns/rows.
    * Splits with 3+ children (they only come from outside the manager) are
      folded into two halves.
    * With two windows the root becomes horizontal.
    """
    node = _norm(node)
    if isinstance(node, Split) and node.orient == V and len(leaves(node)) == 2:
        node = Split(H, node.children)
    return node


def _norm(node: Optional[Node]) -> Optional[Node]:
    if node is None or isinstance(node, Leaf):
        return node
    kids = [k for k in (_norm(c) for c in node.children) if k is not None]
    if not kids:
        return None
    if len(kids) == 1:
        return kids[0]
    if len(kids) > 2:
        return _norm(_fold(node.orient, kids))
    return Split(node.orient, tuple(_align(k, node.orient) for k in kids))


def _fold(orient: str, kids: list) -> Node:
    if len(kids) == 1:
        return kids[0]
    if len(kids) == 2:
        return Split(orient, tuple(kids))
    mid = len(kids) // 2
    return Split(orient, (_fold(perp(orient), kids[:mid]), _fold(perp(orient), kids[mid:])))


def _align(child: Node, parent_orient: str) -> Node:
    if isinstance(child, Split) and child.orient == parent_orient:
        return transpose(child)
    return child


# --------------------------------------------------------------- validation

def validate(node: Optional[Node]) -> list:
    """List of layout rule violations ([] if valid)."""
    errors: list = []
    ids = leaves(node)
    if len(ids) != len(set(ids)):
        errors.append(f'duplicate windows: {ids}')
    if isinstance(node, Split) and len(ids) == 2 and node.orient != H:
        errors.append('two windows are not side by side')

    def walk(n, parent_orient):
        if n is None or isinstance(n, Leaf):
            return
        if n.orient not in (H, V):
            errors.append(f'invalid orientation {n.orient!r}')
        if len(n.children) == 0:
            errors.append('empty container')
        elif len(n.children) == 1:
            errors.append(f'redundant container {n!r}')
        elif len(n.children) > 2:
            errors.append(f'{len(n.children)} siblings in {n!r}')
        if parent_orient is not None and n.orient == parent_orient:
            errors.append(f'same orientation nested in {n!r} (three columns/rows)')
        for c in n.children:
            walk(c, n.orient)

    walk(node, None)
    return errors


# ------------------------------------------------------- parsing helpers

def parse(text: str) -> Optional[Node]:
    """``'H[A,V[B,C]]'`` -> tree. Leaf names: alphanumeric sequences."""
    text = text.replace(' ', '')
    if not text:
        return None
    pos = 0

    def node():
        nonlocal pos
        if text[pos] in 'HV' and pos + 1 < len(text) and text[pos + 1] == '[':
            orient = text[pos].lower()
            pos += 2
            kids = []
            while text[pos] != ']':
                kids.append(node())
                if text[pos] == ',':
                    pos += 1
            pos += 1
            return Split(orient, tuple(kids))
        start = pos
        while pos < len(text) and text[pos] not in ',[]':
            pos += 1
        return Leaf(text[start:pos])

    result = node()
    if pos != len(text):
        raise ValueError(f'trailing text in {text!r} from {pos}')
    return result
