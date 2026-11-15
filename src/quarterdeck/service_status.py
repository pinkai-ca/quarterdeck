"""Cheap hints to schedule presentation; never evidence for delivery or lifecycle.

Directory/file changes request a new reading. The bounded periodic reading also
covers native screens, expiring grants, disk space and changes outside these hints.
No Facts, record state or screen is reused by this scheduler.
"""


def fingerprint(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns


# What the service writes itself, on every pass or with each collection: a change there is the service's own
# doing, never a reason to collect again. move-hold.json is refreshed by its idle reading on its passes, and
# compaction.json by its observation as a working seat's record grows. The 30 s refresh still reads both.
SERVICE_WRITTEN = frozenset({'move-hold.json', 'compaction.json'})


def inputs(ship):
    """Bounded metadata: fixed directories and one shallow listing per declared seat.

    Immutable post/atomic attempt writes change their parent. Journals live one level below pending,
    and seat metadata is checked by file, so an in-place update requests a reading too. A seat's folder
    is listed, not stamped: a file added or removed changes the listing, while a rename of a file the
    service rewrites leaves it as it was. Never an input: what the service writes itself (SERVICE_WRITTEN,
    its heartbeat and its accounting snapshot) and a working seat's native record, which grows on every
    turn. Those, native screens, expiring grants, disk space and changes outside these hints are read by
    the periodic refresh (the ship's status_refresh, 30 s by default); every alert the line carries is
    either in a file kept here or as slow as a screen.
    """
    runtime = ship.runtime
    paths = [ship.file]
    paths += [runtime / name for name in (
        'post', 'post/store', 'post/attempts', 'post/pending', 'post/imports',
        'post/forwarded', 'post/outbound', 'post/drop', 'post/group-holds',
        'post/cascade-holds', 'post/structure-waits', 'requests', 'request-state',
        'hwa', 'pool-usage', 'proven/check-outcomes', 'server-recovery.json',
        'retirement-status.json', 'pool-forecast-alerts.json', 'seat-holds.json', 'pr-screen/screening', 'post/relaunch',
    )]
    from . import trusted_files
    for name in sorted(ship.seats):
        paths += [runtime / 'post/pending' / name, runtime / 'post/drop' / name]
        # Its own folder (what its program and its watch write) and its trusted one (what the service writes).
        for folder in (runtime / 'seats' / name, trusted_files.folder(runtime, name)):
            try:
                paths += sorted(path for path in folder.iterdir()
                                if path.name != 'home' and path.name not in SERVICE_WRITTEN)
            except FileNotFoundError:
                pass
            except OSError:
                return None
    try:
        return tuple((str(path), fingerprint(path)) for path in paths)
    except OSError:
        return None  # an unreadable hint cannot certify that nothing changed


def split(ship, files):
    """The inputs the post's traffic changes (everything under runtime/post), and the rest. A change to the post's
    alone needs only a post refresh; any other needs a full collection."""
    post = str(ship.runtime / 'post')
    ours = tuple(item for item in files if item[0] == post or item[0].startswith(post + '/'))
    return ours, tuple(item for item in files if not (item[0] == post or item[0].startswith(post + '/')))


def door_seats(ship, before, after):
    """Seats whose own pending or drop folder changed between two post readings: their door is read again."""
    old, changed = dict(before or ()), set()
    for path, stamp in after:
        if old.get(path) == stamp:
            continue
        for kind in ('pending', 'drop'):
            prefix = str(ship.runtime / 'post' / kind) + '/'
            if path.startswith(prefix) and '/' not in path[len(prefix):]:
                changed.add(path[len(prefix):])
    return frozenset(changed)
