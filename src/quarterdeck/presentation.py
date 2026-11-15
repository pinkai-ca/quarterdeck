"""Published status text, never evidence for a lifecycle or receipt decision."""

import json
import hashlib
import os
from pathlib import Path
import sys
import time

from . import copy_state, servicectl, ship as shipmod
from .procstat import EXITED

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
    """The start time of a process that has not exited: an exited one (Z, X, x) proves no identity, so None."""
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return fields[19] if fields[0] not in EXITED else None
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
        from . import accounting_health, ledger_recovery, post_retirement
        if data.get('ledger_recovery') != ledger_recovery.alarms(runtime):
            return None
        if data.get('retirement') != post_retirement.reading(runtime):
            return None
        current = accounting_health.reading(runtime)
        if ('accounting' not in data or current == accounting_health.UNKNOWN
                or data['accounting'] != current):
            return None  # derived text cannot outrank changed or unavailable accounting evidence
        return line if fresh(data['wall'], data['monotonic']) else None
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        return None


def published_line(folder):
    """Read a running service's rendering; None asks for independent collection."""
    return published_reading(folder)[0]


def published_reading(folder, *, status_refresh=None):
    """Return text and, when a publication expired, its age for the collector's lead.

    The lock/heartbeat are read independently even when the publication is old.
    Stale alerts remain visible, but their old service heading never claims liveness.
    """
    folder = Path(folder).expanduser().resolve()
    line = cached_line(folder)
    if line is not None:
        return line, None
    lead = service_lead(folder / 'runtime')
    if lead != 'service ok' and not lead.startswith('service busy'):
        return None, None  # a stopped service cannot keep the named waits up to date
    if status_refresh is None:
        try:
            status_refresh = shipmod.load(folder).status_refresh
        except (shipmod.ShipError, OSError):
            status_refresh = shipmod.STATUS_REFRESH
    plain_ns = round((status_refresh + servicectl.STALE_SECONDS) * 1_000_000_000)
    collect_ns = round(3 * status_refresh * 1_000_000_000)
    try:
        path = folder / 'runtime' / NAME
        if path.stat().st_size > 65536:
            raise ValueError('publication too large')
        data = json.loads(path.read_text())
        line = data['line']
        if (type(data['format']) is not int or data['format'] != 1
                or not isinstance(line, str) or not line.startswith('service ok · ')
                or '\n' in line or '\r' in line
                or type(data['wall']) is not int or type(data['monotonic']) is not int
                or ('written' in data and type(data['written']) is not int)
                or type(data['pid']) is not int or data['pid'] <= 0
                or not isinstance(data['process_start'], str) or not data['process_start']):
            raise ValueError('invalid publication')
        written = data.get('written')
        age = time.time_ns() - written if written is not None else -1
        if age < 0:
            return None, None  # an undated/reversed clock cannot bound reuse
        if age > collect_ns:
            return None, age // 1_000_000_000
        if lead == 'service ok' and 0 <= age <= plain_ns:
            beat = servicectl.read_heartbeat(folder / 'runtime')
            if (beat is not None and beat[0] == data['pid'] and 0 <= beat[1] <= servicectl.STALE_SECONDS
                    and process_start(beat[0]) == data['process_start']
                    and data['ship'] == fingerprint(folder / 'ship.toml')
                    and data['framework'] == framework_stamp()):
                return line, None
        dated = f'published {age // 1_000_000_000}s ago'
        return f'{lead} · status stale: {dated}; inspect qd status · {line.partition(" · ")[2]}', None
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        return None, None


def collected_line(line, publication_age):
    """Keep fresh alerts, explicitly saying why the independent collector ran."""
    if publication_age is None:
        return line
    lead, separator, alerts = line.partition(' · ')
    return f'{lead} · publication {publication_age}s old, collected here' + separator + alerts


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
    """Only the service writes this rendering, dating collection and publication separately."""
    from . import ship as shipmod, watch

    wall, monotonic = time.time_ns(), time.monotonic_ns()
    stamp = fingerprint(ship.file)
    current = shipmod.load(ship.folder)
    facts = watch.facts(current, tmux, store=store, attempts=attempts)
    line = facts.line()
    beat = servicectl.read_heartbeat(ship.runtime)
    start = process_start(os.getpid())
    if (facts.service != 'ok' or beat is None or beat[0] != os.getpid() or start is None
            or not servicectl.alive(ship.runtime) or fingerprint(ship.file) != stamp):
        return False
    data = dict(format=1, wall=wall, written=time.time_ns(), monotonic=monotonic, pid=os.getpid(), process_start=start,
                ship=stamp, framework=framework, line=line, accounting=getattr(facts, 'accounting', None),
                ledger_recovery=getattr(facts, 'ledger_recovery', []),
                retirement=getattr(facts, 'retirement', None))
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
    from . import ship as shipmod

    try:
        ship = shipmod.load(shipmod.find(folder))
    except shipmod.ShipError:
        return None  # keep the CLI's declaration errors and numeric exit
    selected = copy_state.select(ship.folder, mode=ship.code, mode_known=True)
    if selected.warning or (selected.qd is not None
                            and Path(sys.executable).parent.resolve() != selected.qd.parent.resolve()):
        return None
    line, publication_age = published_reading(ship.folder, status_refresh=ship.status_refresh)
    if line is not None:
        return line
    from . import watch
    from .tmux import Tmux, TmuxError

    try:
        return collected_line(watch.facts(ship, Tmux.for_ship(ship)).line(), publication_age)
    except (TmuxError, shipmod.ShipError):
        return None  # preserve the CLI's native failure exit and independent alarm
    except (OSError, ValueError, KeyError, TypeError):
        return unavailable_line(argv)


def service_lead(runtime):
    """Only the live lock and heartbeat establish the service heading."""
    try:
        if not servicectl.alive(runtime):
            return 'SERVICE DOWN'
        beat = servicectl.read_heartbeat(runtime)
        if beat is None or beat[1] < 0:
            return 'service busy (heartbeat unknown)'
        if beat[1] > servicectl.STALE_SECONDS:
            return f'service busy {int(beat[1])}s'
        return 'service ok'
    except OSError:
        return 'service unknown'


def unavailable_line(argv):
    """Even a declaration/reader refusal must not leave tmux showing cached health."""
    folder = command_folder(argv)
    if folder is None:
        return None
    try:
        folder = Path(folder).expanduser().resolve()
        if folder.is_file():
            folder = folder.parent
        lead = service_lead(folder / 'runtime')
        publication_age = published_reading(folder)[1]
    except OSError:
        lead = 'service unknown'
        publication_age = None
    return collected_line(lead + ' · status unknown: reading unavailable; inspect qd status', publication_age)
