"""Service-produced JSON status, bounded by the reading's age and owner."""

from dataclasses import fields
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import time

from . import durable, lock_holders, output, servicectl

NAME = 'status-reading.json'
DEMAND = 'status-demand.json'
MAX_AGE_NS = 60_000_000_000  # two ordinary 30 s whole-reading periods
DEMAND_NS = 300_000_000_000
MAX_BYTES = 32 * 1024 * 1024
TOP = frozenset(('service', 'seats', 'programs', 'programs_error', 'check_outcomes', 'check_outcome_errors',
                 'open_orders', 'requests', 'facts', 'usage', 'pools', 'harnesses'))


def path(runtime, name=NAME):
    return Path(runtime) / 'status' / name


def _write(runtime, name, value):
    folder = path(runtime).parent
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    durable.replace_file(folder / name, json.dumps(value, allow_nan=False) + '\n')


def _read(runtime, name, limit=MAX_BYTES):
    descriptor = os.open(path(runtime, name), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        with os.fdopen(os.dup(descriptor), 'rb') as stream:
            raw = stream.read(limit + 1)
        return json.loads(raw) if len(raw) <= limit else None
    finally:
        os.close(descriptor)


def _fresh(data, bound):
    if not isinstance(data, dict) or type(data.get('wall')) is not int or type(data.get('monotonic')) is not int:
        return False
    age, elapsed = time.time_ns() - data['wall'], time.monotonic_ns() - data['monotonic']
    return 0 <= age < bound and 0 <= elapsed < bound and abs(age - elapsed) <= 50_000_000


def request(ship):
    """A JSON caller asks for five minutes of headless periodic readings.

    The real status folder is absent inside a jail: a private stamp cannot
    reach the service, and its served reading is absent, so the command reads
    cold. An unwritable destination also keeps the cold reader.
    """
    try:
        _write(ship.runtime, DEMAND, dict(wall=time.time_ns(), monotonic=time.monotonic_ns()))
    except OSError:
        return False
    return True


def requested(ship):
    runtime = getattr(ship, 'runtime', None)
    if runtime is None:
        return False
    try:
        return _fresh(_read(runtime, DEMAND, 4096), DEMAND_NS)
    except (OSError, ValueError, TypeError, UnicodeError):
        return False


def invalidate(runtime):
    path(runtime).unlink(missing_ok=True)


def _complete(reading):
    from .watch import Facts, SeatReading
    if not isinstance(reading, dict) or not TOP <= reading.keys():
        return False
    if not isinstance(reading['facts'], dict) or not {field.name for field in fields(Facts)} <= reading['facts'].keys():
        return False
    if any(not isinstance(reading[name], list) for name in
           ('seats', 'programs', 'check_outcomes', 'check_outcome_errors', 'open_orders', 'requests', 'usage', 'pools', 'harnesses')):
        return False
    expected = {field.name for field in fields(SeatReading)}
    return all(isinstance(seat, dict) and expected <= seat.keys() for seat in reading['seats'])


def publish(ship, reading, *, wall, monotonic, framework, stamp):
    from . import presentation
    value = output.value(reading)
    beat = servicectl.read_heartbeat(ship.runtime)
    start = servicectl.process_start(os.getpid())
    if (not _complete(value) or value['service'] != 'ok' or beat is None or beat[0] != os.getpid()
            or not servicectl.alive(ship.runtime) or start is None
            or presentation.fingerprint(ship.file) != stamp):
        invalidate(ship.runtime)
        return
    _write(ship.runtime, NAME, dict(format=1, wall=wall, monotonic=monotonic,
                                  pid=os.getpid(), process_start=start,
                                  ship=stamp, framework=framework, reading=value))


def labelled(reading, mode, wall):
    at = datetime.fromtimestamp(wall / 1_000_000_000, timezone.utc).isoformat()
    label = f'served: service reading at {at}' if mode == 'served' else f'cold: independent reading at {at}'
    return {**output.value(reading), 'reading': dict(mode=mode, at=at, label=label)}


def served(ship):
    from . import presentation
    try:
        data = _read(ship.runtime, NAME)
        if (not isinstance(data, dict) or type(data.get('format')) is not int or data['format'] != 1
                or not _fresh(data, MAX_AGE_NS) or not _complete(data.get('reading'))
                or data['reading']['service'] != 'ok' or data['reading']['facts']['service'] != 'ok'):
            return None
        beat = servicectl.read_heartbeat(ship.runtime)
        if (beat is None or type(data.get('pid')) is not int or data['pid'] <= 0 or beat[0] != data['pid']
                or not 0 <= beat[1] <= servicectl.STALE_SECONDS or not servicectl.alive(ship.runtime)
                or lock_holders.reading(servicectl.lock_path(ship.runtime)) != (True, data['pid'])
                or data.get('process_start') is None or servicectl.process_start(data['pid']) != data['process_start']
                or data.get('ship') != presentation.fingerprint(ship.file)
                or data.get('framework') != presentation.framework_stamp()):
            return None
        return labelled(data['reading'], 'served', data['wall']) if _fresh(data, MAX_AGE_NS) else None
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, OverflowError):
        return None
