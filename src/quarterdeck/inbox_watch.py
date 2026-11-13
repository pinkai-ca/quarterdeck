"""Current own-watch registration; native command classification stays in the adapter.

The first two registration fields retain the existing pid/time reading. Old or
incomplete registrations still show an armed watch, but grant no idle exception.
"""

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat

from . import durable, post


def process(pid):
    """A live process's parent and kernel start marker, without command or environment reads."""
    try:
        if type(pid) is not int or pid <= 0:
            return None
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] in ('Z', 'X', 'x'):
            return None
        return int(fields[1]), fields[19]
    except (OSError, ValueError, IndexError, UnicodeError):
        return None


def launch(runtime, name):
    try:
        raw = (runtime / 'seats' / name / 'launch.json').read_bytes()
        data = json.loads(raw)
        if type(data['pid']) is not int or data['pid'] <= 0:
            return None
        at = datetime.fromisoformat(data['at'])
        if at.tzinfo is None:
            return None
        return data['pid'], at, hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        return None


def stamp(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('watch registration is not a regular file')
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def register(runtime, name):
    path = post.watch_file(runtime, name)
    with post.admission(runtime, name):
        own = process(os.getpid())
        source = launch(runtime, name)
        text = (f'{os.getpid()} {post.now().isoformat(timespec="microseconds")} '
                f'{own[1] if own else "-"} {source[2] if source else "-"} '
                f'{secrets.token_hex(16)}\n')
        path.parent.mkdir(parents=True, exist_ok=True)
        durable.replace_file(path, text)
        return text, stamp(path)


def retire(runtime, name, registration):
    path = post.watch_file(runtime, name)
    with post.admission(runtime, name):
        try:
            if stamp(path) == registration[1] and path.read_text() == registration[0]:
                path.unlink()
                durable.sync_folder(path.parent)
        except FileNotFoundError:
            pass


def current(seat, called_at):
    """Both native candidate and this seat's unchanged live generation must be proved."""
    if called_at is None or called_at.tzinfo is None:
        return False
    path = post.watch_file(seat.ship.runtime, seat.name)
    try:
        before = stamp(path)
        text = path.read_text()
        fields = text.split()
        if len(fields) != 5 or fields[2] == '-' or len(fields[4]) != 32:
            return False
        pid = int(fields[0])
        registered = datetime.fromisoformat(fields[1])
        source = launch(seat.ship.runtime, seat.name)
        observed = process(pid)
        if (source is None or observed is None or registered.tzinfo is None
                or observed[1] != fields[2] or source[2] != fields[3]
                or not source[1] <= called_at <= registered <= post.now()):
            return False
        root, parent, seen = source[0], observed[0], {pid}
        for _ in range(64):
            if parent in seen or parent <= 0:
                return False
            ancestor = process(parent)
            if ancestor is None:
                return False
            if parent == root:
                break
            seen.add(parent)
            parent = ancestor[0]
        else:
            return False
        return (process(pid) == observed and launch(seat.ship.runtime, seat.name) == source
                and stamp(path) == before and path.read_text() == text)
    except (OSError, ValueError, TypeError, IndexError, UnicodeError):
        return False
