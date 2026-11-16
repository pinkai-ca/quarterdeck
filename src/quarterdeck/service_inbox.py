"""Reuse a completed inbox pass only while all its inputs and its tail still stand.

Post, import, addressed and forwarding records are immutable: their directory
stamps cover publication/removal. Mutable seat journals and rendered files need
their own stamps. Recent timestamps, links and unreadable inputs certify nothing.
"""

from datetime import timedelta
import stat
import time

from . import trusted_files
from . import forward_post, post, post_dir
from .ship import ShipError


def fingerprint(runtime, paths):
    """A cheap inventory, including ancestor identity and missing-name races."""
    inventory, newest = [], 0
    root = runtime.parent
    for path in paths:
        entries, changed = [], 0
        relative = path.relative_to(root)
        walked = root
        previous = None
        for part in (None, *relative.parts):
            if part is not None:
                walked /= part
            try:
                info = walked.lstat()
            except FileNotFoundError:
                # Absence in a recently changed parent is just as racy as presence.
                if previous is not None:
                    changed = max(previous.st_mtime_ns, previous.st_ctime_ns)
                    entries.append((previous.st_mtime_ns, previous.st_ctime_ns))
                entries.append((str(walked), None))
                break
            if stat.S_ISLNK(info.st_mode):
                return None
            entries.append((str(walked), info.st_dev, info.st_ino, info.st_mode))
            if walked == path:
                entries.append((info.st_size, info.st_mtime_ns, info.st_ctime_ns))
                changed = max(info.st_mtime_ns, info.st_ctime_ns)
            previous = info
        inventory.append((tuple(entries), changed))
        newest = max(newest, changed)
    return tuple(inventory), newest


def inputs(service):
    runtime = service.runtime
    names = tuple(service.ship.seats)
    # Forward declarations can reserve an undeclared old address's handoff too.
    journals = sorted(set(names) | set(forward_post.declared(runtime)))
    sources = [runtime.parent / 'ship.toml', service.store.dir,
               *(runtime / 'post' / name for name in ('imports', 'addressed', 'forwarded')),
               *(trusted_files.path(runtime, name, file) for name in journals
                 for file in (trusted_files.REFUSAL, trusted_files.COMPACTION))]
    outputs = [runtime / 'post' / 'inbox',
               *(runtime / 'post' / 'inbox' / f'{name}.md' for name in names)]
    return (names, fingerprint(runtime, sources)), fingerprint(runtime, outputs)


def trusted(key, started):
    sources, outputs = key
    return (sources[1] is not None and outputs is not None
            and max(sources[1][1], outputs[1]) + post.POST_TIMESTAMP_TICK_NS < started)


def projection(key):
    """The same inventory, omitting only the immutable message-store folder."""
    sources = key[0]
    if sources[1] is None:
        return None
    paths = sources[1][0]
    return sources[0], paths[:1] + paths[2:]


def projection_trusted(projected, started):
    return (projected is not None
            and max((item[1] for item in projected[1]), default=0)
            + post.POST_TIMESTAMP_TICK_NS < started)


def refresh(service):
    started, clock = time.time_ns(), post.now()
    certificate = getattr(service, 'inbox_certificate', None)
    service.inbox_certificate = None  # exceptions and partial passes certify nothing
    try:
        before = inputs(service)
    except (OSError, ValueError, ShipError):
        before = None
    if (before is not None and certificate is not None
            and before == certificate[0] and trusted(before, started)
            and certificate[1] <= started and certificate[2] <= clock
            and (certificate[3] is None or clock < certificate[3])):
        service.inbox_certificate = certificate
        return

    projected = projection(before) if before is not None else None
    prior_projection = getattr(service, 'inbox_projection', None)
    service.inbox_projection = None
    if (projected != prior_projection or not projection_trusted(projected, started)):
        # Ordinary arrivals keep the service's incremental Store warm. Changed
        # projections need a new reader; retain it for the following duties too.
        service.store = post.Store(service.runtime)
    succeeded, deadline = render(service)
    if succeeded and before is not None:
        try:
            after = inputs(service)
        except (OSError, ValueError, ShipError):
            return
        finished, end_clock = time.time_ns(), post.now()
        if (projected is not None and projected == projection(after)
                and projection_trusted(projected, started) and started <= finished):
            service.inbox_projection = projected
        if (after == before and trusted(before, started) and started <= finished
                and clock <= end_clock and (deadline is None or end_clock < deadline)):
            service.inbox_certificate = before, started, clock, deadline


def render(service):
    store = service.store
    with post.reading_pass(store=store):
        return render_pass(service, store)


def render_pass(service, store):
    succeeded, deadline = True, None
    for name in service.ship.seats:
        path = service.runtime / 'post' / 'inbox' / f'{name}.md'
        try:
            hand = getattr(service.ship.seats[name], 'role', None) == 'hand'
            data = (post.inbox_reading(store, name, recent=True, open_only=True) if hand
                    else post.inbox_reading(store, name, recent=True))
            text = post.render_inbox_reading(data)
            for message in data['received']:
                # The cutoff excludes strictly older entries, not equal ones.
                expires = message.at + post.INBOX_TAIL_AGE + timedelta(microseconds=1)
                deadline = expires if deadline is None else min(deadline, expires)
            try:
                current = post_dir.read_text(path)
            except FileNotFoundError:
                current = None
            if current != text:
                post.render_inbox(service.runtime, store, name, open_only=hand)
        except ShipError as error:
            succeeded = False
            service.log('error', step='inbox', seat=name, error=str(error))

    return succeeded, deadline
