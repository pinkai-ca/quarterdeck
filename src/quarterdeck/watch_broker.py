"""A jailed seat's inbox watch, served by the service: nothing under runtime/post becomes writable.

A seat in a person's session is reached by its watch, ``qd inbox --wait``. Inside a jail ``runtime/post`` is
read-only, so the wait cannot take its admission lock or open and close its delivery attempts. It does what its
own seat folder allows instead, and the service does the rest on its behalf:

- The wait registers its watch in its seat folder as every watch does, holds a lease there (an exclusive
  ``flock`` on ``watch-lease`` for its whole life), and asks for the service with ``watch-broker.json``, which
  names its watch generation and the lease's identity. A process id from inside the jail means nothing outside
  it, so the lease, not the process id, says the watch is armed.
- The service, under the seat's admission, opens the attempts for what waits (the same law record as a door,
  before anything is printed) and publishes one offer, ``runtime/post/watch-offers/<seat>.json``: service
  written, read-only in the jail, naming the generation, the messages and their attempts.
- The wait reads the offer for its own generation and returns those messages and attempts; ``qd inbox`` prints
  them and then writes one receipt per attempt in ``watch-receipts/`` in the seat folder.
- The service closes an attempt as a watch delivery only when it is still open, is this seat's, and is named by
  this seat's offer. A receipt that is not a regular file is never read.

While no service owns the ship, ``qd check`` serves its own waking-line seat with the same pass (serve_check_seat):
``qd setup`` checks before it starts anything, and a jailed wait with no broker is never offered its message.

Locks: the waiting seat holds only its own lease, in a folder only its own jail sees, and the service only tests
that lease, never waits on it. The service's locks here (the seat's admission, the attempts lock) are visible in
every jail, so the service waits on each for at most ``WAIT`` and skips the seat for that pass.

law: one-door: an armed watch holds the waking line, an attempt is opened before anything is printed, a receipt
is a delivery, and an attempt offered but never receipted stays open to be judged, as for a wait that dies
between opening and printing.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import time
from pathlib import Path

from . import durable, post, post_dir
from .ship import ShipError

WAIT = 2.0  # the service's bound on a post lock a jail can see; past it the pass skips this seat and retries
OFFER_SECONDS = 60.0  # an offered attempt is the broker's to settle this long; past it reconcile judges it as any watch's
LEASE = 'watch-lease'
REQUEST = 'watch-broker.json'
RECEIPTS = 'watch-receipts'


def needed(runtime: Path) -> bool:
    """Whether this process cannot write delivery attempts: a read-only post folder, as inside a jail."""
    folder = runtime / 'post' / 'attempts'
    target = folder if folder.exists() else runtime / 'post'
    return target.exists() and not os.access(target, os.W_OK)


def seat_folder(runtime: Path, seat: str) -> Path:
    return post.watch_file(runtime, seat).parent


def offer_path(runtime: Path, seat: str) -> Path:
    if not seat or '/' in seat or seat in ('.', '..'):
        raise ShipError(f'no watch offer for seat {seat!r}')
    return runtime / 'post' / 'watch-offers' / f'{seat}.json'


LIMIT = 1 << 16  # the bound on any one file the service reads from a seat's folder


def _folder_fd(folder: Path, parent: int | None = None) -> int | None:
    """A descriptor for a real directory, never through a link; None when it is absent or anything else."""
    try:
        return os.open(folder if parent is None else folder.name,
                       os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                       **({} if parent is None else dict(dir_fd=parent)))
    except OSError:
        return None


def _claim(folder_fd: int, name: str, path: Path) -> bytes | None:
    """The seat's own claim: one regular file read through post_dir's guard (no link followed, regular, bounded,
    and refused if it changes in place during the read); None for anything else."""
    try:
        return post_dir._read_nofollow(folder_fd, name, path, LIMIT)
    except (OSError, ShipError):
        return None


def _seat_file(runtime: Path, seat: str, name: str) -> bytes | None:
    folder = seat_folder(runtime, seat)
    descriptor = _folder_fd(folder)
    if descriptor is None:
        return None
    try:
        return _claim(descriptor, name, folder / name)
    finally:
        os.close(descriptor)


def request(runtime: Path, seat: str) -> dict | None:
    raw = _seat_file(runtime, seat, REQUEST)
    try:
        data = json.loads(raw) if raw is not None else None
        if (not isinstance(data, dict) or data.get('format') != 1 or not isinstance(data.get('generation'), str)
                or len(data['generation']) != 32 or not isinstance(data.get('lease'), list) or len(data['lease']) != 2
                or not all(type(value) is int for value in data['lease'])):
            return None
        return data
    except (ValueError, UnicodeError):
        return None


def lease_held(runtime: Path, seat: str, generation: str | None) -> bool | None:
    """True while the brokered watch of this generation holds its lease, False once it has let go.

    None when this generation asked for no broker: the caller reads the watch as it always has.
    """
    data = request(runtime, seat)
    if data is None or generation is None or data['generation'] != generation:
        return None
    folder = _folder_fd(seat_folder(runtime, seat))
    if folder is None:
        return None
    try:
        descriptor = os.open(LEASE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=folder)
    except FileNotFoundError:
        return False
    except OSError:
        return None
    finally:
        os.close(folder)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or [info.st_dev, info.st_ino] != data['lease']:
            return False
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False  # nobody holds it: the shared lock taken here is released by the close
    finally:
        os.close(descriptor)


def read_offer(runtime: Path, seat: str) -> dict | None:
    try:
        raw = post_dir.read_bytes(offer_path(runtime, seat), limit=1 << 20)
    except (FileNotFoundError, ShipError, OSError):
        return None
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        return None
    if (not isinstance(data, dict) or data.get('format') != 1 or data.get('seat') != seat
            or not isinstance(data.get('generation'), str) or not isinstance(data.get('attempts'), list)
            or not isinstance(data.get('messages'), list)
            or not all(isinstance(item, str) for item in data['attempts'] + data['messages'])):
        return None
    return data


def offered(runtime: Path) -> set[str]:
    """The attempt names every live offer holds: the broker settles these from receipts, on a pass of its own.

    The offer is the service's own file; nothing the seat writes can add a name here.
    """
    names: set[str] = set()
    try:
        paths = post_dir.json_paths(runtime / 'post' / 'watch-offers')
    except (OSError, ShipError):
        return names
    for path in paths:
        data = read_offer(runtime, path.stem)
        if data is not None:
            names.update(data['attempts'])
    return names


def _attempt(runtime: Path, name: str) -> post.Attempt | None:
    try:
        path = post.attempt_path(runtime, name)
        return post.Attempt(**json.loads(post_dir.read_text(path)), path=path)
    except (OSError, ShipError, ValueError, TypeError):
        return None


# -- the wait's side -----------------------------------------------------------------------------------------------


class Lease:
    """The brokered watch's lease: an exclusive flock in its own seat folder for the wait's whole life."""

    def __init__(self, runtime: Path, seat: str):
        folder = seat_folder(runtime, seat)
        folder.mkdir(parents=True, exist_ok=True)
        self.descriptor = os.open(folder / LEASE, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.descriptor)
            raise ShipError(f'{seat}: another inbox watch is already armed') from None
        info = os.fstat(self.descriptor)
        self.identity = [info.st_dev, info.st_ino]

    def close(self) -> None:
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None


def wait(runtime: Path, seat: str, interval: float, stop_after: float | None, validate=None):
    """The jailed wait: register, hold the lease, ask for the service, and take the offer for this generation."""
    from . import inbox_watch

    store = post.Store(runtime)
    if validate is not None:
        validate()
    lease = Lease(runtime, seat)
    registration = None

    def retain(value):
        nonlocal registration
        registration = value

    try:
        registration = inbox_watch.register(runtime, seat, validate=validate, on_registered=retain, admitted=False)
        durable.replace_file(seat_folder(runtime, seat) / REQUEST,
                             json.dumps(dict(format=1, generation=registration['generation'],
                                             lease=lease.identity)) + '\n')
        deadline = None if stop_after is None else time.monotonic() + stop_after
        while True:
            if validate is not None:
                validate()
            offer = read_offer(runtime, seat)
            if offer is not None and offer['generation'] == registration['generation'] and offer['attempts']:
                attempts = [_attempt(runtime, name) for name in offer['attempts']]
                if all(a is not None and a.seat == seat and a.result is None for a in attempts):
                    return [store.get(identifier) for identifier in offer['messages']], attempts
            if deadline is not None and time.monotonic() > deadline:
                return None
            time.sleep(interval)
    finally:
        try:
            if registration is not None:
                try:
                    inbox_watch.retire(runtime, seat, registration, admitted=False)
                except Exception:
                    post.watch_retirement_warning(post.watch_file(runtime, seat), registration['generation'])
        finally:
            lease.close()


def receipt(runtime: Path, attempts: list[post.Attempt]) -> None:
    """After the print: one receipt per attempt, in the seat's own folder, for the service to close."""
    for attempt in attempts:
        folder = seat_folder(runtime, attempt.seat) / RECEIPTS
        folder.mkdir(parents=True, exist_ok=True)
        durable.replace_file(folder / attempt.path.name,
                             json.dumps(dict(format=1, attempt=attempt.path.name, message=attempt.message)) + '\n')


# -- the service's side --------------------------------------------------------------------------------------------


def check_seats(ship) -> list:
    """Undeclared seats that asked for brokering and that a live check owns as a person's seat.

    A seat folder alone admits nothing: only the check's own live lease (pending.check_inbox_owner, the admission
    qd inbox uses for such a seat) does, and only a human one. Each is a stand-in with the name and the ship.
    """
    from types import SimpleNamespace

    folder = ship.runtime / 'seats'
    try:
        names = sorted(entry.name for entry in os.scandir(folder)
                       if entry.is_dir(follow_symlinks=False) and entry.name not in ship.seats)
    except OSError:
        return []
    return [SimpleNamespace(name=name, ship=ship) for name in names if check_admitted(ship.runtime, name)]


def check_admitted(runtime: Path, name: str) -> bool:
    """A seat that asked for brokering and that a live check owns, by its lease, as a person's seat."""
    from .pending import check_inbox_owner

    if request(runtime, name) is None:
        return False
    try:
        owner = check_inbox_owner(runtime, name)
    except ShipError:
        return False
    return owner.get('human') is True


class CheckHost:
    """What the broker needs of a service, for a check that serves its own seat: the store, the attempts, a log."""

    def __init__(self, runtime: Path, log=None):
        self.store, self.attempts = post.Store(runtime), post.Attempts(runtime)
        self.log = log or (lambda event, **data: None)


def serve_check_seat(ship, name: str, log=None) -> bool:
    """One broker pass for a live check's own human seat, while no service owns the ship.

    qd setup runs qd check before any service is up, so nothing else would open the attempts and publish the offer
    a jailed wait asks for, and the check's wake proof would wait for a receipt that cannot come. The admission is
    the service's own (check_admitted: the check's live lease on a person's seat, as check_seats has it; the check's
    temporary seat is undeclared by construction), and so is the pass
    (run, under the seat's admission, every lock bounded). A live service owns the broker: the check stands aside,
    and two brokers in turn meet the same offer under the same admission. True when a pass ran.
    """
    from types import SimpleNamespace
    from . import servicectl

    if servicectl.alive(ship.runtime) or not check_admitted(ship.runtime, name):
        return False
    run(CheckHost(ship.runtime, log), SimpleNamespace(name=name, ship=ship))
    return True


def run(service, seat) -> None:
    """Close receipted attempts, then offer what waits to a brokered watch that is armed. Under the seat's admission.

    Every lock taken here is one a jail can see, so each wait is bounded: past it this seat is skipped this pass,
    logged, and tried again on the next. The waiting seat itself locks nothing the service waits on.
    """
    runtime = seat.ship.runtime
    if (request(runtime, seat.name) is None and not offer_path(runtime, seat.name).exists()
            and not (seat_folder(runtime, seat.name) / RECEIPTS).exists()):
        return  # no brokered watch, no offer, no receipt: nothing to serve, and no lock taken
    try:
        _run(service, seat)
    except post.LockBusy as busy:
        service.log('watch-broker-skipped', seat=seat.name, reason=str(busy))
    from . import post_view
    if post_view.folder(runtime, seat.name).is_dir():
        # A jailed wait reads its offer, and the attempts it names, from its view of the post: show them now,
        # not at the next pass.
        try:
            post_view.refresh(runtime, seat.name)
        except (OSError, ValueError, ShipError) as error:
            service.log('error', step='jailed_watches', seat=seat.name, error=repr(error))


def _run(service, seat) -> None:
    runtime, name = seat.ship.runtime, seat.name
    with post.admission(runtime, name, wait=WAIT):
        offer = read_offer(runtime, name)
        _settle(service, name, offer)
        reading = post.watch_reading(runtime, name)
        data = request(runtime, name)
        if offer is not None:
            pending = [a for a in (_attempt(runtime, item) for item in offer['attempts']) if a is not None and a.result is None]
            if pending and offer['generation'] == reading.generation:
                return  # offered to this watch and not yet receipted
            post_dir.remove(offer_path(runtime, name))  # settled, or its watch is gone: open attempts stay to be judged
            service.log('watch-offer-retired', seat=name, open=len(pending))
        if data is None or reading.armed is not True or reading.generation != data['generation']:
            return
        waiting = service.attempts.undelivered(service.store, name)
        if not waiting:
            return
        opened = [service.attempts.open(message, name, None, None, None, wait=WAIT) for message in waiting]  # law: one-door
        durable.replace_file(offer_path(runtime, name), json.dumps(dict(
            format=1, seat=name, generation=data['generation'], messages=[m.id for m in waiting],
            attempts=[a.path.name for a in opened])) + '\n')
        service.log('watch-offered', seat=name, messages=[m.short for m in waiting])


def _settle(service, name: str, offer: dict | None) -> None:
    """Close what the seat's receipts claim. The folder and each receipt are walked without following a link: a
    receipts folder that is a link is never entered, and nothing outside the seat's own folder is read or removed."""
    seat = _folder_fd(seat_folder(service.store.dir.parent.parent, name))
    if seat is None:
        return
    try:
        folder = _folder_fd(seat_folder(service.store.dir.parent.parent, name) / RECEIPTS, seat)
    finally:
        os.close(seat)
    if folder is None:
        return
    try:
        _settle_in(service, name, offer, folder)
    finally:
        os.close(folder)


def _settle_in(service, name: str, offer: dict | None, folder: int) -> None:
    where = seat_folder(service.store.dir.parent.parent, name) / RECEIPTS
    offered = set(offer['attempts']) if offer is not None else set()
    with os.scandir(folder) as entries:
        names = [(entry.name, entry.is_file(follow_symlinks=False)) for entry in entries]
    for entry, regular in names:
        if not regular:
            continue  # never read through a link or a special file; the seat's own folder keeps it
        raw = _claim(folder, entry, where / entry)
        try:
            data = json.loads(raw) if raw is not None else None
        except (ValueError, UnicodeError):
            data = None
        attempt = None
        if (isinstance(data, dict) and data.get('format') == 1 and data.get('attempt') == entry
                and entry in offered):
            attempt = _attempt(service.store.dir.parent.parent, entry)
        if attempt is not None and attempt.seat == name and attempt.result is None:
            service.attempts.close(attempt, 'watch', 'printed by qd inbox --wait', wait=WAIT)
            service.log('watch-receipt', seat=name, message=attempt.message)
        elif attempt is None or attempt.result is not None:
            service.log('watch-receipt-ignored', seat=name, receipt=entry)
        try:
            os.unlink(entry, dir_fd=folder)
        except FileNotFoundError:
            pass
