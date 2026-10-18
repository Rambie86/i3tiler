"""CLI: ``python3 -m i3tiler daemon|send|dump``."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import logging
import logging.handlers
import os
import signal
import sys
import time

import i3ipc

from . import i3state
from .model import normalize, validate


def _socket(args):
    return args.socket or os.environ.get('I3SOCK') or None


def _lock(socket_path):
    """One instance per i3 session: the new one replaces the old one (so
    ``exec_always`` on every reload does not pile up processes)."""
    key = hashlib.sha1((socket_path or i3ipc.Connection().socket_path).encode()).hexdigest()[:12]
    base = os.environ.get('XDG_RUNTIME_DIR') or '/tmp'
    path = os.path.join(base, f'i3tiler-{key}.lock')
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            old = int(os.pread(fd, 32, 0).decode().strip() or 0)
            if old:
                os.kill(old, signal.SIGTERM)
        except (ValueError, ProcessLookupError):
            pass
        deadline = time.time() + 3
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() > deadline:
                    sys.exit('i3tiler: another instance does not release the lock')
                time.sleep(0.05)
    os.ftruncate(fd, 0)
    os.pwrite(fd, str(os.getpid()).encode(), 0)
    return fd


def _logging(path, verbose):
    handlers = [logging.StreamHandler(sys.stderr)]
    if path:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(path, maxBytes=512_000, backupCount=1))
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s', handlers=handlers)


def main(argv=None):
    p = argparse.ArgumentParser(prog='i3tiler')
    p.add_argument('--socket', help='i3 IPC socket (defaults to the current session)')
    sub = p.add_subparsers(dest='cmd', required=True)
    d = sub.add_parser('daemon', help='layout manager (start it with exec_always)')
    d.add_argument('--exclude', action='append', default=[], help='workspace to leave unmanaged (repeatable)')
    d.add_argument('--log', default=os.path.expanduser('~/.cache/i3tiler/i3tiler.log'))
    d.add_argument('-v', '--verbose', action='store_true')
    s = sub.add_parser('send', help='send a command to the daemon: move left|right|up|down, reconcile, pause, resume, toggle')
    s.add_argument('words', nargs='+')
    sub.add_parser('dump', help='show the logical tree of each workspace (read-only)')
    args = p.parse_args(argv)
    sock = _socket(args)

    if args.cmd == 'daemon':
        _logging(args.log, args.verbose)
        _lock(sock)
        signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
        from .daemon import Manager
        Manager(i3ipc.Connection(socket_path=sock), exclude=args.exclude).run()
    elif args.cmd == 'send':
        i3ipc.Connection(socket_path=sock).send_tick('i3tiler ' + ' '.join(args.words))
    elif args.cmd == 'dump':
        snap = i3state.read(i3ipc.Connection(socket_path=sock).get_tree())
        for ws in snap.workspaces.values():
            tree = normalize(ws.raw)
            print(f'{ws.name:>6}: i3={ws.raw!r}  logical={tree!r}  '
                  f'{"OK" if not validate(tree) else validate(tree)}  '
                  f'{"" if ws.managed else "UNMANAGED: " + ws.reason}'
                  f'{"  floating=" + str(ws.floating) if ws.floating else ""}')


if __name__ == '__main__':
    main()
