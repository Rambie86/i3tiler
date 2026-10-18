"""Event handling: the single source of layout changes.

Everything goes through one process and one thread, so there are never two
simultaneous rearrangements. After any event the manager "reconciles": it
reads the real tree, computes the tree there should be, and applies it only
if they differ.

* New windows (not in the last applied state) are placed with the opening
  policy; the rest is kept and compacted.
* Closing, sending to another workspace, making floating: the gap is
  compacted.
* Changes made with native i3 commands (split, layout toggle...) are
  normalized to the rules on the next event.
"""
from __future__ import annotations

import logging
import os

import i3ipc

from . import i3state, ops
from .geometry import DIRECTIONS, layout_rects
from .model import normalize, validate, without
from .local import NotLocal, plan_local
from .realize import Deferred, conforming, plan

log = logging.getLogger('i3tiler')
PREFIX = 'i3tiler'
ALIASES = {'h': 'left', 'j': 'down', 'k': 'up', 'l': 'right'}


class Manager:
    def __init__(self, i3, exclude=()):
        self.i3 = i3
        self.exclude = set(exclude)
        self.known = {}          # ws_id -> tiling windows already placed
        self.pending = set()     # workspaces deferred until they get focus
        self.failed = {}         # ws_id -> structure after a failure (prevents loops)
        self.paused = False

    # -------------------------------------------------------------- reading
    def snapshot(self):
        return i3state.read(self.i3.get_tree(), self.exclude)

    def desired(self, ws):
        return self.desired_ex(ws)[0]

    def desired_ex(self, ws):
        """Tree ``ws`` should have right now, and the insertions that produce
        it from the current structure."""
        prev = self.known.get(ws.id)
        new = [] if prev is None else [w for w in ws.tiling if w not in prev]
        tree = normalize(without(ws.raw, new))
        # Split the window that had focus before the new one opened; if
        # several arrive at once, each one follows the previous one.
        recent = [w for w in ws.recency if w not in new]
        hint = recent[0] if recent else None
        actions = []
        for w in sorted(new):                       # con_id grows with creation order
            tree, act = ops.insert_new_ex(tree, w, focused=hint, area=ws.area)
            if act is not None:
                actions.append(act)
            hint = w
        return tree, actions

    # ------------------------------------------------------------ reconcile
    def reconcile(self, only=None):
        if self.paused:
            return
        snap = self.snapshot()
        for ws in list(snap.workspaces.values()):
            if only is not None and ws.id not in only:
                continue
            self._reconcile_ws(snap, ws)
        for gone in set(self.known) - set(snap.workspaces):
            self.known.pop(gone, None)
            self.pending.discard(gone)
            self.failed.pop(gone, None)

    def _reconcile_ws(self, snap, ws):
        if not ws.managed:
            log.debug('ws %s unmanaged: %s', ws.name, ws.reason)
            return
        target, actions = self.desired_ex(ws)
        errors = validate(target)
        if errors:
            log.error('ws %s: computed tree is invalid %r: %s', ws.name, target, errors)
            return
        if conforming(ws, target):
            self.known[ws.id] = set(ws.tiling)
            self.pending.discard(ws.id)
            self.failed.pop(ws.id, None)
            return
        if self.failed.get(ws.id) == repr(ws.raw):
            return
        focused = snap.focused_ws == ws.id
        self.apply(snap, ws, target, snap.focused_window if focused else None, focused, actions)

    # ---------------------------------------------------------------- apply
    def apply(self, snap, ws, target, focus_id, ws_focused, actions=()):
        tag = f'_i3tiler{os.getpid()}_'
        try:
            cmds = plan_local(ws, target, list(actions), focus_id, ws_focused, tag)
            mode = 'local'
        except NotLocal as e:
            log.debug('ws %s: no local plan (%s); full rebuild', ws.name, e)
            try:
                cmds = plan(ws, target, focus_id, ws_focused, tag)
                mode = 'full'
            except Deferred as e:
                log.info('ws %s deferred: %s', ws.name, e)
                self.pending.add(ws.id)
                return False
        log.info('ws %s: %r -> %r (%s, %d commands)', ws.name, ws.raw, target, mode, len(cmds))
        replies = self.i3.command('; '.join(cmds))
        bad = [(c, r.error) for c, r in zip(cmds, replies) if not r.success]
        after = self.snapshot()
        lost = snap.all_windows - after.all_windows
        if lost:
            # Should never happen: i3 does not close windows when moving
            # containers. If a window disappears from the tree, warn.
            log.critical('windows disappeared from the tree after applying! %s', lost)
        ws2 = after.workspaces.get(ws.id)
        ok = ws2 is not None and conforming(ws2, target) and not bad
        if ws2 is not None and sorted(ws2.floating) != sorted(ws.floating):
            log.error('ws %s: the set of floating windows changed %s -> %s', ws.name, ws.floating, ws2.floating)
            ok = False
        if ok:
            self.known[ws.id] = set(ws2.tiling)
            self.pending.discard(ws.id)
            self.failed.pop(ws.id, None)
        else:
            log.error('ws %s: result does not match. expected %r, got %r, failures %s\n%s',
                      ws.name, target, ws2.raw if ws2 else None, bad, '\n'.join(cmds))
            if ws2 is not None:
                self.failed[ws.id] = repr(ws2.raw)
        return ok

    # ----------------------------------------------------------------- move
    def move(self, direction):
        snap = self.snapshot()
        wid = snap.focused_window
        ws = snap.workspaces.get(snap.focused_ws)
        if wid is None or ws is None or wid not in ws.tiling or not ws.managed:
            log.debug('move %s ignored (floating, no window or unmanaged ws)', direction)
            return
        current, pending_actions = self.desired_ex(ws)
        exact = conforming(ws, current)
        rects = ws.rects if exact else layout_rects(current, ws.area)
        result = ops.move_ex(current, wid, direction, rects)
        if result is None:
            log.debug('move %s: no valid target for %s in %r', direction, wid, current)
            if not exact:
                self.apply(snap, ws, current, wid, True, pending_actions)
            return
        new, act = result
        # A local plan only exists if i3 already matches the starting tree.
        self.apply(snap, ws, new, wid, True, [act] if exact else ())

    # ------------------------------------------------------------- commands
    def command(self, text):
        parts = text.split()
        if not parts or parts[0] != PREFIX:
            return False
        args = parts[1:]
        if args[:1] == ['move'] and len(args) == 2:
            d = ALIASES.get(args[1], args[1])
            if d in DIRECTIONS and not self.paused:
                self.move(d)
        elif args == ['reconcile']:
            self.failed.clear()
            self.reconcile()
        elif args == ['pause']:
            self.paused = True
            log.info('paused')
        elif args == ['resume']:
            self.paused = False
            self.failed.clear()
            log.info('resumed')
            self.reconcile()
        elif args == ['toggle']:
            self.command(f'{PREFIX} {"resume" if self.paused else "pause"}')
        else:
            log.warning('unknown command: %s', text)
        return True

    # --------------------------------------------------------------- events
    def _guard(self, fn, *a):
        try:
            fn(*a)
        except Exception:                            # the daemon must never die
            log.exception('error handling event')

    def on_window(self, _i3, e):
        if e.change == 'focus':
            if self.pending:
                self._guard(self.reconcile, set(self.pending))
            return
        if e.change in ('new', 'close', 'move', 'floating', 'fullscreen_mode'):
            self._guard(self.reconcile)

    def on_workspace(self, _i3, e):
        if e.change == 'focus' and self.pending:
            self._guard(self.reconcile, set(self.pending))
        elif e.change in ('move', 'reload', 'restored'):
            self._guard(self.reconcile)

    def on_binding(self, _i3, e):
        cmd = e.binding.command.strip()
        if cmd.startswith('nop ' + PREFIX):
            self._guard(self.command, cmd[4:].strip())
        else:
            self._guard(self.reconcile)

    def on_tick(self, _i3, e):
        if not getattr(e, 'first', False) and e.payload:
            self._guard(self.command, e.payload)

    @staticmethod
    def _warn_competitors():
        """Another rearranger (autotiling) would fight with this manager."""
        for pid in filter(str.isdigit, os.listdir('/proc')):
            try:
                with open(f'/proc/{pid}/cmdline', 'rb') as f:
                    cmd = f.read().replace(b'\0', b' ').decode(errors='replace')
            except OSError:
                continue
            if 'autotiling' in cmd and 'python' in cmd:
                log.warning('autotiling is running (pid %s): it changes split h/v on every '
                            'focus change and conflicts with i3tiler. Stop it: pkill -f autotiling', pid)

    def run(self):
        self._warn_competitors()
        self.i3.on(i3ipc.Event.WINDOW, self.on_window)
        self.i3.on(i3ipc.Event.WORKSPACE, self.on_workspace)
        self.i3.on(i3ipc.Event.BINDING, self.on_binding)
        self.i3.on(i3ipc.Event.TICK, self.on_tick)
        self._guard(self.reconcile)
        log.info('i3tiler running')
        self.i3.main()
        log.info('connection to i3 closed; exiting')
