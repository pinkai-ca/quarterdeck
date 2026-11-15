"""Positive, owned process evidence: this boot, this user's processes in a run's own process group, and
this user's processes using a folder. Nothing here asks to see every process on the machine: another user's
process is not ours, and one of ours whose links cannot be read (a privileged helper such as sd-pam or an
ssh agent) is counted as uninspectable, not as a participant."""
from __future__ import annotations

import os
from pathlib import Path

# The states of a process that has exited: Z, a zombie until it is reaped, and X (x before Linux 4.14) for the
# instant of its reaping, after which /proc no longer shows it. Each is read as ended, never as live; a process caught
# between Z and its disappearance is still one that has exited.
EXITED = frozenset({'Z', 'X', 'x'})


def boot_id() -> str:
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def stat_fields(pid: int) -> list[str] | None:
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    except (FileNotFoundError, ProcessLookupError, PermissionError, IndexError):
        return None


def start_time(pid: int) -> str | None:
    fields = stat_fields(pid)
    return fields[19] if fields and len(fields) > 19 else None


def _ours() -> list[int]:
    uid = os.getuid()
    pids = []
    for name in os.listdir('/proc'):
        if name.isdigit():
            try:
                if os.stat(f'/proc/{name}').st_uid == uid:
                    pids.append(int(name))
            except FileNotFoundError:
                pass
    return pids


def group_members(pgid: int) -> list[int]:
    """This user's live processes in the process group ``pgid`` (zombies excluded)."""
    members = []
    for pid in _ours():
        fields = stat_fields(pid)
        if fields and len(fields) > 2 and fields[0] not in EXITED and fields[2] == str(pgid):
            members.append(pid)
    return members


def session_groups(sid: int) -> list[int]:
    """Live groups in a step's own session, including children which changed their group."""
    groups = set()
    for pid in _ours():
        fields = stat_fields(pid)
        if fields and len(fields) > 3 and fields[0] not in EXITED and fields[3] == str(sid):
            groups.add(int(fields[2]))
    return sorted(groups)


def users_of(folder: Path) -> tuple[list[dict], int]:
    """This user's processes whose working directory, program or root lies in ``folder``."""
    folder = folder.resolve()
    users, uninspectable = [], 0
    for pid in _ours():
        if pid == os.getpid():
            continue
        seen = False
        for link in ('cwd', 'exe', 'root'):
            try:
                target = Path(os.readlink(f'/proc/{pid}/{link}'))
            except (FileNotFoundError, ProcessLookupError):
                break
            except PermissionError:
                uninspectable += not seen
                seen = True
                continue
            if target == folder or folder in target.parents:
                users.append({'pid': pid, 'link': link, 'path': str(target)})
                break
    return users, uninspectable


def tree(pid: int, start: str) -> dict[int, str]:
    """A process of this user's and its descendants now, each by pid and start; empty unless ``pid`` still has
    ``start`` and has not exited."""
    rows = {}
    for found in _ours():
        fields = stat_fields(found)
        if fields and len(fields) > 19 and fields[0] not in EXITED:
            rows[found] = (int(fields[1]), fields[19])
    if pid not in rows or rows[pid][1] != start:
        return {}
    taken, stack = {}, [pid]
    while stack:
        at = stack.pop()
        if at in taken:
            continue
        taken[at] = rows[at][1]
        stack += [child for child, (parent, _) in rows.items() if parent == at]
    return taken


def alive(pids: dict[int, str]) -> dict[int, str]:
    """Which of these still run, by pid and start; an exited process (a zombie included) has ended."""
    left = {}
    for pid, start in pids.items():
        fields = stat_fields(pid)
        if fields and len(fields) > 19 and fields[19] == start and fields[0] not in EXITED:
            left[pid] = start
    return left


def end_tree(pid: int, start: str, *, rounds: int = 20, pause: float = 0.05) -> dict[int, str]:
    """Stop, then kill, a process and every descendant, by pid and start, taking in new children each round, and
    confirm. Returns what still runs; empty once all of it has ended. A pid is signalled only while it still has the
    start it was found with, so a pid given to another process since is never struck."""
    import signal
    import time
    known = tree(pid, start)
    for _ in range(rounds):
        live = alive(known)
        for parent, parent_start in list(live.items()):
            known.update(tree(parent, parent_start))
        live = alive(known)
        if not live:
            return {}
        for sig in (signal.SIGSTOP, signal.SIGKILL):
            for target, target_start in live.items():
                if start_time(target) == target_start:
                    try:
                        os.kill(target, sig)
                    except (ProcessLookupError, PermissionError):
                        pass
        time.sleep(pause)
    return alive(known)
