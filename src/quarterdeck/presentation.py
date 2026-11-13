"""Optional bounded status text, never evidence for a lifecycle or receipt decision."""

import json
import hashlib
import os
from pathlib import Path
import sys
import time

from . import copy_state, servicectl

NAME = 'status-line.json'
MAX_AGE_NS = 3_000_000_000  # covers long actor duties; independent owner checks still run on every job
CLOCK_SKEW_NS = 50_000_000


def fingerprint(path):
    info = path.stat()
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def framework_stamp():
    root = Path(__file__).parent.resolve()
    digest = hashlib.sha256()
    for path in sorted(root.rglob('*.py')):
        digest.update(str(path.relative_to(root)).encode() + b'\0')
        digest.update(path.read_bytes() + b'\0')
    return [str(root), list(sys.version_info[:3]), digest.hexdigest()]


def process_start(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError, UnicodeError):
        return None


def fresh(wall, monotonic):
    if type(wall) is not int or type(monotonic) is not int:
        return False
    wall_age, age = time.time_ns() - wall, time.monotonic_ns() - monotonic
    return 0 <= wall_age <= MAX_AGE_NS and 0 <= age <= MAX_AGE_NS and abs(wall_age - age) <= CLOCK_SKEW_NS


def cached_line(folder):
    """A fast presentation may never hide death, a busy owner or unverified freshness."""
    folder = Path(folder).expanduser().resolve()
    runtime = folder / 'runtime'
    selected = copy_state.select(folder)
    if selected.warning or (selected.qd is not None
                            and Path(sys.executable).parent.resolve() != selected.qd.parent.resolve()):
        return None  # the ordinary CLI performs the existing installed-copy handoff
    try:
        path = runtime / NAME
        if path.stat().st_size > 65536:
            return None
        data = json.loads(path.read_text())
        line = data['line']
        if (type(data['format']) is not int or data['format'] != 1
                or not isinstance(line, str) or not line.startswith('service ok · ')
                or '\n' in line or '\r' in line or not fresh(data['wall'], data['monotonic'])):
            return None
        if (data['ship'] != fingerprint(folder / 'ship.toml') or data['framework'] != framework_stamp()
                or type(data['pid']) is not int or data['pid'] <= 0 or data['process_start'] is None):
            return None
        beat = servicectl.read_heartbeat(runtime)
        if (beat is None or beat[0] != data['pid'] or not 0 <= beat[1] <= servicectl.STALE_SECONDS
                or not servicectl.alive(runtime)  # law: nothing-waits-silently
                or process_start(beat[0]) != data['process_start']):
            return None
        from . import accounting_health
        current = accounting_health.reading(runtime)
        if ('accounting' not in data or current == accounting_health.UNKNOWN
                or data['accounting'] != current):
            return None  # derived text cannot outrank changed or unavailable accounting evidence
        return line if fresh(data['wall'], data['monotonic']) else None
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        return None


def publish(ship, tmux, framework, store, attempts):
    """A failed/refused collection cannot leave its prior healthy rendering available."""
    written = False
    try:
        written = _publish(ship, tmux, framework, store, attempts)
        return written
    finally:
        if not written:
            (ship.runtime / NAME).unlink(missing_ok=True)


def _publish(ship, tmux, framework, store, attempts):
    """Only the service writes this disposable rendering, dated at collection start."""
    from . import ship as shipmod, watch

    wall, monotonic = time.time_ns(), time.monotonic_ns()
    stamp = fingerprint(ship.file)
    current = shipmod.load(ship.folder)
    facts = watch.facts(current, tmux, store=store, attempts=attempts)
    line = facts.line()
    beat = servicectl.read_heartbeat(ship.runtime)
    start = process_start(os.getpid())
    if (facts.service != 'ok' or beat is None or beat[0] != os.getpid() or start is None
            or not servicectl.alive(ship.runtime) or fingerprint(ship.file) != stamp
            or not fresh(wall, monotonic)):
        return False
    data = dict(format=1, wall=wall, monotonic=monotonic, pid=os.getpid(), process_start=start,
                ship=stamp, framework=framework, line=line, accounting=getattr(facts, 'accounting', None))
    path = ship.runtime / NAME
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, allow_nan=False) + '\n')
    os.replace(temporary, path)
    return True


def command_folder(argv):
    if len(argv) == 4 and argv[0] == '--ship' and argv[2:] == ['status', '--line']:
        folder = argv[1]
    elif len(argv) == 3 and argv[0].startswith('--ship=') and argv[1:] == ['status', '--line']:
        folder = argv[0][7:]
    elif argv == ['status', '--line'] and os.environ.get('QD_SHIP'):
        folder = os.environ['QD_SHIP']
    else:
        return None
    if not folder or folder.startswith('-'):
        return None
    return folder


def command_line(argv):
    """Only the plain canonical text command bypasses the normal parser/handoff."""
    folder = command_folder(argv)
    if folder is None:
        return None
    line = cached_line(folder)
    if line is not None:
        return line
    from . import ship as shipmod, watch
    from .tmux import Tmux, TmuxError

    try:
        ship = shipmod.load(shipmod.find(folder))
    except shipmod.ShipError:
        return None  # keep the CLI's declaration errors and numeric exit
    selected = copy_state.select(ship.folder, mode=ship.code, mode_known=True)
    if selected.warning or (selected.qd is not None
                            and Path(sys.executable).parent.resolve() != selected.qd.parent.resolve()):
        return None
    try:
        return watch.facts(ship, Tmux.for_ship(ship)).line()
    except (TmuxError, shipmod.ShipError):
        return None  # preserve the CLI's native failure exit and independent alarm
    except (OSError, ValueError, KeyError, TypeError):
        return unavailable_line(argv)


def unavailable_line(argv):
    """Even a declaration/reader refusal must not leave tmux showing cached health."""
    folder = command_folder(argv)
    if folder is None:
        return None
    try:
        folder = Path(folder).expanduser().resolve()
        if folder.is_file():
            folder = folder.parent
        runtime = folder / 'runtime'
        if not servicectl.alive(runtime):
            lead = 'SERVICE DOWN'
        else:
            beat = servicectl.read_heartbeat(runtime)
            if beat is None or beat[1] < 0:
                lead = 'service busy (heartbeat unknown)'
            elif beat[1] > servicectl.STALE_SECONDS:
                lead = f'service busy {int(beat[1])}s'
            else:
                lead = 'service ok'
    except OSError:
        lead = 'service unknown'
    return lead + ' · status unknown: reading unavailable; inspect qd status'
