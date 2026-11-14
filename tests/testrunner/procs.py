"""Positive, owned process evidence: this boot, this user's processes in a run's own process group, and
this user's processes using a folder. Nothing here asks to see every process on the machine: another user's
process is not ours, and one of ours whose links cannot be read (a privileged helper such as sd-pam or an
ssh agent) is counted as uninspectable, not as a participant."""
from __future__ import annotations

import os
from pathlib import Path


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
        if fields and len(fields) > 2 and fields[0] != 'Z' and fields[2] == str(pgid):
            members.append(pid)
    return members


def session_groups(sid: int) -> list[int]:
    """Live groups in a step's own session, including children which changed their group."""
    groups = set()
    for pid in _ours():
        fields = stat_fields(pid)
        if fields and len(fields) > 3 and fields[0] != 'Z' and fields[3] == str(sid):
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
