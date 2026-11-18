"""Metadata-only warnings for a current record growing toward its read bound."""

from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import time

from . import compaction_alert, durable, human_wake, post, seat_files
from .harness import record_file
from .ship import ShipError

SAMPLE_SECONDS = 300
MIN_SPAN = 15 * 60
MAX_SPAN = 2 * 60 * 60
LEAD_HOURS = 12
FOLDER = 'record-growth'


def _stat(path: Path):
    """Stat only the named file through the seat-folder walk. Never open the record."""
    parent = seat_files.parent(path, create=False)
    if parent is None:
        return path.lstat()
    folder, name = parent
    try:
        return os.stat(name, dir_fd=folder, follow_symlinks=False)
    finally:
        os.close(folder)


def _identity(info):
    if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1:
        return info.st_dev, info.st_ino
    return None


def _sample(seat):
    """Only an existing owned pointer can name a record for the forecast."""
    human = seat.session.human
    owned = (lambda: human_wake.owned_record(seat)) if human else seat.lookup_record
    record = owned()
    if record is None or not record.is_absolute():
        return None
    info = _stat(record)
    identity = _identity(info)
    if identity is None:
        return None
    size = record_file.size_of(record) if record.suffix == '.db' else info.st_size
    if size is None or owned() != record or _identity(_stat(record)) != identity:
        return None
    # The path is only an input to the key. A report or journal never names a conversation file.
    key = sha256(os.fsencode(record) + b'\0' + f'{identity[0]}:{identity[1]}'.encode()).hexdigest()
    return key, size, record_file.bound_of(record)


def _point(value):
    return isinstance(value, list) and len(value) == 2 and all(type(n) is int and n >= 0 for n in value)


def _read(path):
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise ShipError('record growth journal unreadable') from None
    if (not isinstance(data, dict) or data.get('version') != 1
            or not _point(data.get('first')) or not _point(data.get('last'))
            or type(data.get('published')) is not bool or type(data.get('crew_accepted')) is not bool
            or data.get('report') is not None and not isinstance(data['report'], str)):
        raise ShipError('record growth journal unsupported')
    return data


def _save(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    durable.replace_file(path, json.dumps(data, sort_keys=True) + '\n')


def _publish(service, path, data, recipient):
    message = post.Message.from_json(data['report'])
    if (message.kind != 'report' or message.sender != post.SHIP or message.re is not None
            or message.to != recipient):
        raise ShipError('record growth report intent unsupported')
    with post.admission(service.runtime, recipient), post.fixed_report(service.store, message):
        compaction_alert.publish(service.runtime, data['report'])
    data['published'] = True
    _save(path, data)


def _finish(service, seat, path, data, recipient, launch):
    if not data['published']:
        _publish(service, path, data, recipient)
    if launch is None or data['crew_accepted']:
        return
    message = post.Message.from_json(data['report'])
    reason = message.body.split('\n', 1)[0]
    result = service.request_fresh_handoff(seat, reason=reason, expected_launch=launch)
    if result.disposition in ('staged', 'already-staged'):
        data['crew_accepted'] = True
        _save(path, data)


def _forecast(size, bound, first, last, now):
    """Return observed MiB/hour and projected hours until the bound, before it is reached."""
    elapsed, grew = now - first[0], size - first[1]
    if (not MIN_SPAN <= elapsed <= MAX_SPAN or now <= last[0] or now - last[0] > MAX_SPAN
            or size < last[1] or grew <= 0 or size >= bound):
        return None
    hours = (bound - size) * elapsed / grew / 3600
    if hours <= LEAD_HOURS:
        return grew / record_file.MiB * 3600 / elapsed, hours
    return None


def observe(service, seat, now):
    human = seat.session.human
    role = 'desk' if human else 'systems'
    recipient = next((spec.name for spec in service.ship.seats.values() if spec.role == role), None)
    if recipient is None:
        return
    launch = None if human else seat.launched()
    if (sample := _sample(seat)) is None:
        return
    key, size, bound = sample
    if launch is not None and (size >= bound or seat.launched() != launch):
        launch = None  # a retry must prove its own current launch and pre-bound record
    path = seat.trusted / FOLDER / f'{key}.json'
    data = _read(path)
    point = [now, size]
    if data is None:
        _save(path, dict(version=1, first=point, last=point, report=None, published=False, crew_accepted=False))
        return
    if data['report'] is not None:  # the same immutable report survives a service restart or interrupted post
        _finish(service, seat, path, data, recipient, launch)
        return
    first, last = data['first'], data['last']
    forecast = _forecast(size, bound, first, last, now)
    if now < last[0] or now - first[0] > MAX_SPAN or size < last[1]:
        data['first'] = point
    data['last'] = point
    if forecast is None:
        if point != last:
            _save(path, data)
        return
    rate, hours = forecast
    body = (f'{seat.name}: record {size / record_file.MiB:.1f} MiB of its {bound / record_file.MiB:.1f} MiB read bound. '
            f'It grew at {rate:.1f} MiB/h over {(now - first[0]) / 3600:.2f} h. '
            f'At that rate it could reach the bound in {hours:.1f} h.')
    if human:
        body += '\nAsk the Admiral for his yes before starting it fresh at a natural break.'
    else:
        body += '\nAllow time for the crew handoff and a fresh start at a natural break.'
    data['report'] = service.store.prepare('report', post.SHIP, recipient, body).to_json()
    _save(path, data)
    _finish(service, seat, path, data, recipient, launch)


def notify(service):
    """Take a size sample on the service tick, independently of status rendering."""
    clock = time.monotonic()
    if clock < getattr(service, 'next_record_growth', 0):
        return
    service.next_record_growth = clock + SAMPLE_SECONDS
    now = int(time.time())
    for seat in service.resolved_seats():
        try:
            observe(service, seat, now)
        except FileNotFoundError:
            continue  # a pointer or file that went away during sampling supplies no evidence
        except (OSError, ValueError, ShipError) as error:
            service.log('error', step='record_growth', seat=seat.name, error=type(error).__name__)
