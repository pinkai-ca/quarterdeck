"""Re-read a service's private process scope after its owner has released the lock."""

from dataclasses import dataclass, field
import os
from pathlib import Path
import re

from .procstat import EXITED

PROC = Path('/proc')
CGROUPS = Path('/sys/fs/cgroup')
UNIT = re.compile(r'qd-service-[A-Za-z0-9_.-]+-[0-9a-f]{8}\.scope')
SERVER = 'tmux: server'  # the name tmux gives its server process


class Uncertain(OSError):
    """The scope was readable but did not supply a complete current process reading."""


def process(pid):
    fields = (PROC / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
    return fields[0], fields[19], int(fields[2]), int(fields[3])


def cgroup(pid):
    """Only the private scope shape created by servicectl.scoped, never a caller's shared cgroup."""
    rows = (PROC / str(pid) / 'cgroup').read_text().splitlines()
    relative = next(row[3:] for row in rows if row.startswith('0::'))
    parts = Path(relative).parts
    if not relative.startswith('/') or '..' in parts:
        raise ValueError('unknown cgroup path')
    for index, part in enumerate(parts):
        if UNIT.fullmatch(part):
            return CGROUPS.joinpath(*parts[1:index + 1])
    raise ValueError('not a private service cgroup')


def server(pid):
    """A tmux server, by the name it gives itself: a service stop never ends one, nor anything under it.

    A seat's relaunch can start the ship's tmux server from the service, and then it is born in the service's
    scope; ending it would end every seat's pane and jail with the service."""
    try:
        return (PROC / str(pid) / 'comm').read_text().rstrip('\n') == SERVER
    except (OSError, UnicodeError):
        return False


def descendants(roots):
    """The roots and every process now under them, by parent pid."""
    children = {}
    for entry in PROC.iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            parent = int((entry / 'stat').read_text().rsplit(')', 1)[1].split()[1])
        except (OSError, IndexError, ValueError, UnicodeError):
            continue
        children.setdefault(parent, []).append(int(entry.name))
    found, stack = set(), list(roots)
    while stack:
        pid = stack.pop()
        if pid not in found:
            found.add(pid)
            stack += children.get(pid, [])
    return found


def mark(path):
    info = path.stat()
    return info.st_dev, info.st_ino


@dataclass
class Scope:
    pid: int
    start: str
    fallback: tuple[str, int] | None
    folder: Path | None
    identity: tuple[int, int] | None
    proof: str = ''
    servers: dict = field(default_factory=dict)  # tmux servers met in the scope, left running with their panes

    def read(self):
        """The scope's live processes, less any tmux server in it and every process under one."""
        found = self.members()
        roots = [pid for pid in found if server(pid)]
        if not roots:
            return found
        for pid in roots:
            self.servers.setdefault(pid, found[pid])
        spared = descendants(roots)
        return {pid: start for pid, start in found.items() if pid not in spared}

    def spared(self):
        """The stop line's words for the tmux servers it left running; empty when the scope held none."""
        if not self.servers:
            return ''
        pids = ', '.join(map(str, sorted(self.servers)))
        return f"a tmux server is in the service's scope (pid {pids}); left running with its panes"

    def members(self):
        if self.folder is not None:
            try:
                found = self.read_cgroup()
            except Uncertain:
                raise
            except (OSError, ValueError, StopIteration):
                pass  # the captured private session/group is the only fallback
            else:
                self.proof = f'processes re-read: cgroup {self.folder.name}'
                return found
        if self.fallback is None:
            raise Uncertain('service cgroup unavailable and no private session or process group')
        kind, number = self.fallback
        # A reused leader pid cannot authorize signalling a new session/group.
        try:
            if process(self.pid)[1] != self.start:
                raise Uncertain('service scope leader identity changed')
        except FileNotFoundError:
            pass
        found = {}
        for entry in PROC.iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                state, start, group, session = process(int(entry.name))
            except FileNotFoundError:
                continue
            if state not in EXITED and (session if kind == 'session' else group) == number:
                found[int(entry.name)] = start
        self.proof = f'processes re-read: {kind} {number} (service cgroup unavailable)'
        return found

    def read_cgroup(self):
        if mark(self.folder) != self.identity:
            raise OSError('service cgroup identity changed')
        pids = set()
        def unreadable(error):
            raise error
        for root, directories, _ in os.walk(self.folder, onerror=unreadable, followlinks=False):
            if any((Path(root) / name).is_symlink() for name in directories):
                raise OSError('unknown service cgroup child')
            pids.update(int(value) for value in (Path(root) / 'cgroup.procs').read_text().split())
        found = {}
        for pid in pids:
            try:
                before = process(pid)
                current = cgroup(pid)
                after = process(pid)
            except FileNotFoundError:
                continue
            if before[1:] != after[1:] or current != self.folder:
                raise Uncertain('service cgroup membership changed during reading')
            if after[0] not in EXITED:
                found[pid] = after[1]
        events = dict(line.split() for line in (self.folder / 'cgroup.events').read_text().splitlines())
        if mark(self.folder) != self.identity:
            raise OSError('service cgroup identity changed')
        if events.get('populated') == '0':
            return {}
        if events.get('populated') != '1' or not found:
            raise Uncertain('service cgroup remains populated without readable live identities')
        return found


def capture(pid, start, *, owners=None):
    state, actual, group, session = process(pid)
    if state in EXITED or actual != start:
        raise Uncertain('service owner changed before its process scope was bound')
    owners = {pid: start} if owners is None else owners
    fallback = ('session', session) if session in owners else ('process group', group) if group in owners else None
    folder, identity = None, None
    try:
        folder = cgroup(pid)
        identity = mark(folder)
    except (OSError, ValueError, StopIteration):
        folder = None
    if folder is None and fallback is None:
        raise Uncertain('service has no provable private process scope')
    anchor = fallback[1] if fallback is not None else pid
    return Scope(anchor, owners.get(anchor, start), fallback, folder, identity)
