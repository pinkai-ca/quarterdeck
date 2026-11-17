"""A seat's successful full-body thread readings: claimed in its own folder, receipted only by an unjailed qd.

qd thread, which runs inside a jailed seat's jail, writes what it printed whole as a claim in the seat's own folder
(``runtime/seats/<seat>/thread-reads.json``), the one place a jail lets it write. A claim settles nothing: the seat can
write that file itself. ``take``, run only by an unjailed qd (the service on every pass, and a launch before its index),
moves a claim into the seat's receipts in its trusted folder (``runtime/trusted/<seat>/thread-reads.json``), which no
jail sees. Each receipt is stamped with the claimed moment, never later than the take: a claim cannot date itself
after the take, so a reading made before a launch, taken at the latest when that launch builds its index, never counts
as made after it. Only receipts are read. They never change an attempt's transport verdict, and never close an order
or a question. Writers merge under a seat-local lock and replace the file whole; readers need no lock. Missing or
unreadable evidence settles no work.
"""

from __future__ import annotations

from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import time

from . import durable, post_dir, trusted_files
from .ship import ShipError

NAME = 'thread-reads.json'
LIMIT = 16 << 20
WAIT = 2.0  # only this seat's CLI writers share the lock; service readers never take it


def path(runtime: Path, seat: str) -> Path:
    """The seat's claim: what its qd thread printed whole, written where its jail lets it write. Never read as proof."""
    if not seat or Path(seat).name != seat or seat in ('.', '..'):
        raise ShipError('invalid thread reader seat')
    return runtime / 'seats' / seat / NAME


def receipts_path(runtime: Path, seat: str) -> Path:
    """The seat's receipts, in its trusted folder: written only by ``take``, in an unjailed qd."""
    path(runtime, seat)  # the same rule for the name
    return trusted_files.path(runtime, seat, NAME)


def _load(target: Path, seat: str) -> dict[str, str]:
    try:
        parent = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            raw = post_dir._read_nofollow(parent, target.name, target, LIMIT)
        finally:
            os.close(parent)
        data = json.loads(raw)
    except FileNotFoundError:
        return {}
    if (not isinstance(data, dict) or data.get('format') != 1 or data.get('seat') != seat
            or not isinstance(data.get('read'), dict)
            or not all(isinstance(key, str) and isinstance(value, str) for key, value in data['read'].items())):
        raise ValueError('invalid thread read receipts')
    return data['read']


def read(runtime: Path, seat: str) -> frozenset[str]:
    """The messages the seat's receipts say it printed whole; its claims count only once taken."""
    return frozenset(read_times(runtime, seat))


def read_times(runtime: Path, seat: str) -> dict[str, datetime]:
    """Each receipted message, with the latest moment the seat printed it whole."""
    try:
        return {identifier: _moment(value) for identifier, value in _load(receipts_path(runtime, seat), seat).items()}
    except (OSError, ValueError, TypeError, ShipError):
        return {}  # unknown reading evidence cannot suppress work


def claimed(runtime: Path, seat: str) -> frozenset[str]:
    """What the seat's claim holds, not yet taken; it proves nothing."""
    try:
        return frozenset(_load(path(runtime, seat), seat))
    except (OSError, ValueError, TypeError, ShipError):
        return frozenset()


def _moment(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError('a read moment without its zone')
    return moment


def _locked(target: Path, wait: float) -> int:
    """The seat-local lock beside its claim, or ShipError when another writer holds it past ``wait``."""
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(target.with_suffix('.lock'),
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return descriptor
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(descriptor)
                raise ShipError('thread read receipt writer busy; body printed, reading not receipted') from None
            time.sleep(0.02)


def _write(target: Path, seat: str, read: dict[str, str]) -> None:
    text = json.dumps(dict(format=1, seat=seat, read=read), indent=2) + '\n'
    if len(text.encode('utf-8')) > LIMIT:
        raise ShipError('thread read receipts exceed their size bound; retained for inspection')
    durable.replace_file(target, text)


def record(runtime: Path, seat: str, identifiers) -> None:
    """The claim, after complete output and flush; a reading already claimed and not yet taken writes nothing."""
    identifiers = frozenset(identifiers)
    if not identifiers or identifiers <= claimed(runtime, seat):
        return
    target = path(runtime, seat)
    descriptor = _locked(target, WAIT)
    try:
        try:
            received = _load(target, seat)
        except (ValueError, TypeError) as error:
            raise ShipError('thread read receipts unreadable; retained for inspection') from error
        missing = identifiers - received.keys()
        if not missing:
            return
        moment = datetime.now().astimezone().isoformat()
        received.update((identifier, moment) for identifier in sorted(missing))
        _write(target, seat, received)
    finally:
        os.close(descriptor)


TAKE_WAIT = 0.5  # the seat's own processes can hold its lock: a take that cannot have it waits for the next pass
REFUSED = 'thread-reads-refused.txt'  # in the trusted folder: a refusal is logged, until a take settles the claim


def refused(runtime: Path, seat: str, reason: str) -> None:
    """Log one seat's claim that could not be taken, once while it stands, from the service or a launch alike.

    The claim lies in a folder the seat's jail writes: what is there is the seat's to mend, and nothing of it stops
    a launch or a pass. The words go to the service's log, as the service writes its own."""
    marker = trusted_files.path(runtime, seat, REFUSED)
    if marker.exists():
        return  # logged already, in the words it first had: a claim's next refusal is the same fault until a take settles
    log = Path(runtime) / 'log' / 'service.jsonl'
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open('a') as stream:
            stream.write(json.dumps({'at': datetime.now().astimezone().isoformat(timespec='seconds'), 'event': 'error',
                                     'step': 'thread-reads', 'seat': seat, 'error': reason}) + '\n')
        durable.replace_file(marker, reason)
    except OSError:
        pass  # an unwritable log or marker costs a repeated line, never the launch or the pass


def _settled(runtime: Path, seat: str) -> None:
    try:
        trusted_files.path(runtime, seat, REFUSED).unlink(missing_ok=True)
    except OSError:
        pass


def take(runtime: Path, seat: str, party) -> int:
    """Move the seat's claim into its receipts; the number of messages receipted. Run only by an unjailed qd.

    ``party()`` gives the messages the seat sent or received, read only when there is a claim to take: a claimed id
    outside them is dropped. Each receipt is stamped with the claimed moment, never later than now; a later reading
    of the same message moves its stamp on. Nothing is taken where the seat's trusted folder is absent: a jail shows
    an empty one in its place, so a take made inside a jail would write where no reader looks.

    A claim that cannot be taken (a link, a FIFO, a folder or an oversize file at its name, unreadable contents, a
    lock held past the wait, or one replaced before it is removed) is left where it is, receipts nothing and returns
    0; it is logged once while it stands (``refused``). The claim is removed before its receipts are written, so a
    claim that cannot be removed receipts nothing."""
    target, receipts = path(runtime, seat), receipts_path(runtime, seat)
    if not receipts.parent.is_dir():
        return 0
    try:
        os.lstat(target)
    except FileNotFoundError:
        _settled(runtime, seat)
        return 0
    except OSError as error:
        refused(runtime, seat, f'its claim cannot be read ({type(error).__name__}); left in place')
        return 0
    try:
        descriptor = _locked(target, TAKE_WAIT)
    except (ShipError, OSError) as error:
        refused(runtime, seat, f"its claim's lock cannot be taken ({type(error).__name__}); left in place")
        return 0
    try:
        try:
            claim = _load(target, seat)
        except (OSError, ValueError, TypeError, ShipError) as error:
            refused(runtime, seat, f'its claim is unreadable ({type(error).__name__}: {error}); left in place')
            return 0
        try:
            kept = _load(receipts, seat)
        except (OSError, ValueError, TypeError, ShipError) as error:
            refused(runtime, seat, f'its receipts are unreadable ({type(error).__name__}); its claim is left in place')
            return 0
        now = datetime.now().astimezone()
        party = frozenset(party())
        taken = 0
        for identifier, value in claim.items():
            if identifier not in party:
                continue
            try:
                moment = min(_moment(value), now)
            except (ValueError, TypeError):
                moment = now  # a claim that names no moment is dated by its take
            if identifier in kept:
                try:
                    moment = max(moment, _moment(kept[identifier]))
                except (ValueError, TypeError):
                    pass
            kept[identifier] = moment.isoformat()
            taken += 1
        try:
            os.unlink(target)  # spent first; the lock beside it stays the seat's
        except (OSError, ShipError) as error:
            refused(runtime, seat, f'its claim cannot be removed ({type(error).__name__}); left in place, nothing receipted')
            return 0
        try:
            _write(receipts, seat, kept)
        except (OSError, ShipError) as error:
            refused(runtime, seat, f'its receipts cannot be written ({type(error).__name__}); that reading is not receipted')
            return 0
        _settled(runtime, seat)
        return taken
    finally:
        os.close(descriptor)
