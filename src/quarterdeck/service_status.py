"""Cheap hints to schedule presentation; never evidence for delivery or lifecycle.

Directory/file changes request a new reading. The bounded periodic reading also
covers native screens, expiring grants, disk space and changes outside these hints.
No Facts, record state or screen is reused by this scheduler.
"""

from pathlib import Path

from . import accounting_health, watch


def fingerprint(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def inputs(ship):
    """Bounded metadata: fixed directories and one shallow folder per declared seat.

    Immutable post/atomic attempt writes change their parent. Journals live one
    level below pending, and seat metadata is checked by file as well as folder
    so an in-place update requests a reading too. Native record pointers name files to stat, never bytes to parse; the periodic
    reading also observes native screens and evidence outside these hints.
    """
    runtime = ship.runtime
    paths = [ship.file]
    paths += [runtime / name for name in (
        'post', 'post/store', 'post/attempts', 'post/pending', 'post/imports',
        'post/forwarded', 'post/outbound', 'post/drop', 'post/group-holds',
        'post/cascade-holds', 'post/structure-waits', 'requests', 'request-state',
        'hwa', 'pool-usage', 'proven/check-outcomes', 'server-recovery.json',
        'retirement-status.json', 'pool-forecast-alerts.json',
    )]
    for name in sorted(ship.seats):
        folder = runtime / 'seats' / name
        paths += [folder, runtime / 'post/pending' / name, runtime / 'post/drop' / name]
        try:
            paths += sorted(path for path in folder.iterdir() if path.name != 'home')
        except FileNotFoundError:
            pass
        except OSError:
            return None
        try:
            pointer = folder / 'record'
            if pointer.stat().st_size <= 4096:
                record = pointer.read_text().strip()
                if record:
                    paths += [Path(record), Path(record + '-wal')]
        except FileNotFoundError:
            pass
        except (OSError, UnicodeError):
            return None
    try:
        return (tuple((str(path), fingerprint(path)) for path in paths),
                watch.service_state(runtime), accounting_health.reading(runtime))
    except OSError:
        return None  # an unreadable hint cannot certify that nothing changed
