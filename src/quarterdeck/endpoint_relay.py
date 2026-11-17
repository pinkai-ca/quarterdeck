"""A jailed seat's one way out: its model endpoint, relayed into a jail that has no network of its own.

Every jail runs in a network namespace of its own, loopback only, so no abstract socket, no outside listener and no
other host is reachable from inside. A seat on an endpoint pool still reaches its endpoint, through two halves:

- Outside, the launcher's relay (this module, run as ``python -m quarterdeck.endpoint_relay``) owns a private folder,
  listens on a unix socket there, starts the jail as its child and serves it: each connection on the socket becomes
  one TCP connection to the endpoint's host and port, resolved outside. It forwards a stop to the jail, and when the
  jail ends it removes its folder and exits with the jail's status. The jail dies with it (bwrap's
  ``--die-with-parent``), so a relay that dies takes the jail's reach with it, and nothing falls open.
- Inside, a forwarder (``INSIDE``, standard library only, so no import of the framework is needed in the jail) listens
  on the endpoint's own port on the jail's loopback, then hands its process to the program. Each connection there is
  carried over the unix socket. When the outside half is gone, a connection is refused, never routed elsewhere.

Bytes pass through unchanged in both directions as they arrive, so a streamed answer streams, and TLS to a named
remote endpoint is end to end: the jail's hosts file maps that name to its loopback, the program checks the real
name's certificate, and only the relay resolves the name. Nothing else resolves inside the jail.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

SOCKET = 's'
HOSTS = 'hosts'
INSIDE_FOLDER = '/run/qd-endpoint'  # where the relay's folder is bound in the jail: the jail's own /run

# The inside half. It binds before the program starts, so the endpoint answers from the program's first request.
INSIDE = r'''
import os, socket, sys, threading
relay, port, argv = sys.argv[1], int(sys.argv[2]), sys.argv[4:]
listeners = []
for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
    try:
        listener = socket.socket(family, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen(64)
        listeners.append(listener)
    except OSError:
        if family == socket.AF_INET:
            raise
def pump(source, target):
    try:
        while True:
            data = source.recv(65536)
            if not data:
                break
            target.sendall(data)
    except OSError:
        pass
    finally:
        try:
            target.shutdown(socket.SHUT_WR)
        except OSError:
            pass
def carry(client):
    outward = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        outward.connect(relay)
    except OSError:
        client.close()
        outward.close()
        return
    back = threading.Thread(target=pump, args=(outward, client), daemon=True)
    back.start()
    pump(client, outward)
    back.join()
    client.close()
    outward.close()
def serve(listener):
    while True:
        client, _ = listener.accept()
        threading.Thread(target=carry, args=(client,), daemon=True).start()
if os.fork() == 0:
    os.setsid()
    for listener in listeners[1:]:
        threading.Thread(target=serve, args=(listener,), daemon=True).start()
    serve(listeners[0])
    os._exit(0)
for listener in listeners:
    listener.close()
os.execvp(argv[0], argv)
'''


RELAY_ROOT = Path('/tmp')  # short: a unix socket's path is limited to about a hundred bytes


def folder_for(seat_folder: Path, launch: str = '') -> Path:
    """A short private folder for one launch's relay: the seat's own name, then the launch's own token (the jail's
    launch, drawn by sandbox.plan and held in the launch record's argv). Each launch has its own, so an older relay
    that exits after a relaunch removes only its own folder, never the new one's."""
    import hashlib

    digest = hashlib.sha256(str(seat_folder).encode()).hexdigest()[:16]
    return RELAY_ROOT / (f'qd-endpoint-{os.getuid()}-{digest}' + (f'-{launch}' if launch else ''))


def of_seat(path, seat_folder: Path) -> bool:
    """Whether ``path`` is a relay folder of this seat's, of any launch."""
    base, path = folder_for(seat_folder), Path(path)
    return path.parent == base.parent and (path.name == base.name or path.name.startswith(base.name + '-'))


def _prepare(folder: Path) -> None:
    """The folder, fresh, this user's own and mode 0700; a link or another owner's folder is refused."""
    try:
        info = folder.lstat()
    except FileNotFoundError:
        info = None
    if info is not None:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise SystemExit(f'endpoint relay: {folder} is not this user\'s own folder; refused')
        shutil.rmtree(folder)
    folder.mkdir(mode=0o700)


def _hosts(folder: Path, name: str | None) -> None:
    """The jail's hosts file: the machine's own, and the endpoint's name on the jail's loopback."""
    try:
        base = Path('/etc/hosts').read_text()
    except OSError:
        base = '127.0.0.1 localhost\n::1 localhost\n'
    extra = f'\n127.0.0.1 {name}\n::1 {name}\n' if name else '\n'
    (folder / HOSTS).write_text(base.rstrip('\n') + extra)


def _pump(source: socket.socket, target: socket.socket) -> None:
    try:
        while True:
            data = source.recv(65536)
            if not data:
                break
            target.sendall(data)
    except OSError:
        pass
    finally:
        try:
            target.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _carry(client: socket.socket, host: str, port: int) -> None:
    try:
        outward = socket.create_connection((host, port), timeout=30)
        outward.settimeout(None)
    except OSError:
        client.close()
        return
    back = threading.Thread(target=_pump, args=(outward, client), daemon=True)
    back.start()
    _pump(client, outward)
    back.join()
    client.close()
    outward.close()


def _serve(listener: socket.socket, host: str, port: int) -> None:
    while True:
        try:
            client, _ = listener.accept()
        except OSError:
            return
        threading.Thread(target=_carry, args=(client, host, port), daemon=True).start()


PR_SET_CHILD_SUBREAPER = 36  # linux/prctl.h
REAP_SECONDS = 5.0  # how long the relay waits to reap what it ended before it exits


def _subreaper() -> None:
    """Orphans of the jail come to the relay, not to the user's manager: a jail's inner bwrap whose outer one died
    before it armed its own death with its parent is then still the relay's to end."""
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), 'prctl(PR_SET_CHILD_SUBREAPER) refused')


def end_descendants() -> list[int]:
    """Before the relay exits: end every process left under it, by the write bound's own kill, confirm none runs,
    and reap them. The pids signalled."""
    from . import write_bound
    own = write_bound._processes().get(os.getpid())
    pids = write_bound.tree(os.getpid(), own[1]) if own else {}
    pids.pop(os.getpid(), None)
    signalled = write_bound.kill(pids) if pids else []
    deadline = time.monotonic() + REAP_SECONDS  # what still runs past this is the seat's stop's to hold
    while time.monotonic() < deadline:
        try:
            if os.waitpid(-1, os.WNOHANG) == (0, 0) and not write_bound.alive(pids):
                break
        except ChildProcessError:
            break
        time.sleep(0.02)  # whether a child was reaped or none was ready: the loop never spins
    return signalled


def run(folder: Path, host: str, port: int, name: str | None, argv: list[str]) -> int:
    """Serve the jail started from ``argv`` until it ends; its exit status is the relay's. The relay is a subreaper,
    and before it exits, by the pane's hangup or its jail's end, it ends and reaps everything left under it."""
    _subreaper()
    _prepare(folder)
    _hosts(folder, name)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(folder / SOCKET))
    listener.listen(64)
    threading.Thread(target=_serve, args=(listener, host, port), daemon=True).start()
    child = subprocess.Popen(argv)

    def forward(signum, frame):
        try:
            child.send_signal(signum)
        except ProcessLookupError:
            pass

    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, forward)
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # the terminal's interrupt is the program's, which shares the pane
    try:
        return child.wait()
    finally:
        end_descendants()
        listener.close()
        shutil.rmtree(folder, ignore_errors=True)


def main(args: list[str] | None = None) -> int:
    args = sys.argv[1:] if args is None else args
    if '--' not in args:
        raise SystemExit('usage: python -m quarterdeck.endpoint_relay FOLDER HOST PORT NAME -- JAIL...')
    head, argv = args[:args.index('--')], args[args.index('--') + 1:]
    folder, host, port, name = head
    return run(Path(folder), host, int(port), name or None, argv)


if __name__ == '__main__':
    raise SystemExit(main())
