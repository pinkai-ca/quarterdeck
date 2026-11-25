"""The person's quiet time in a human session, from the seat's own record, and one wake per readiness.

The service reads the newest input the person typed into a human seat's owned current record and
keeps it as a small fact bound to the launch: the time, or unknown with a reason from a fixed list.
qd watch and qd status read only that fact, never the conversation. Only what the adapter declares
as its program's own notices, and the ship's own waking line compared whole, are left out; any other
input counts as the person's, and an adapter that declares nothing reads unknown, never quiet.

When a desk's compaction readiness asks for a manual fresh start, the desk is idle and the person
has been quiet for QUIET, the service posts the desk one order, ORDER, once per readiness. Every seat
in a human session is read; only the desk is woken, and the person starts any other seat there. A busy or
unknown reading changes nothing; only a readiness seen to clear, or a new one, arms the next order.
Nothing here types or restarts: the order goes by the post, and the seat's own qd fresh does the rest.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from . import trusted_files
from . import compaction, durable, idle, post
from .ship import ShipError

QUIET = timedelta(minutes=20)  # a framework constant; there is no ship-file key
WAKES = 'desk'  # the one role the wake is posted to; other seats in the person's session wait for the person
ORDER = ('The person has been quiet for twenty minutes and you are ready: '
         'post your handoff if you have not, then run qd fresh now.')
FACT = trusted_files.PERSON_QUIET
WAKE = trusted_files.PERSON_QUIET_WAKE
VERSION = 1
FUTURE = timedelta(seconds=5)  # a recorded time further ahead of the clock than this is not believed

# Every reason a reading may give; anything else becomes the last one, so no record text is shown.
REASONS = (
    'seat not launched',
    'launch unreadable',
    'no record joined',
    'record unreadable',
    'record has unreadable rows',
    'an input row has no time',
    'an input row is malformed',
    'a queued input is malformed',
    'an input row has an unknown history identity',
    'no person input recorded',
    'a recorded time is in the future',
    'not yet read by the service',
    'reading belongs to an earlier launch',
    'reading unreadable',
    'record could not be classified',
)
UNDECLARED = '{} declares no program notices'


def waking_lines() -> tuple[str, ...]:
    """The ship's own fixed waking line, left out of the person's input, compared whole."""
    from .human_wake import LINE
    return (LINE,)


def _known(seat, reason) -> bool:
    """A shared reason, or one the seat's adapter declares for a row it holds by name."""
    return reason in REASONS or reason in getattr(seat.harness, 'person_quiet_reasons', ())


def _unknown(launch, reason: str) -> dict:
    return dict(version=VERSION, launch=launch, at=None, reason=reason)


def read(seat, now: datetime, excluded: tuple[str, ...] | None = None) -> dict:
    """The fact as the record shows it now. This reads the conversation; only the service calls it."""
    try:
        launched = seat.launched()
    except (OSError, ValueError, TypeError, KeyError):
        return _unknown(None, 'launch unreadable')
    if launched is None:
        return _unknown(None, 'seat not launched')
    launch = compaction.key_for(launched)
    record = seat.record()
    if record is None:
        return _unknown(launch, 'no record joined')
    try:
        times = seat.harness.person_inputs(record, waking_lines() if excluded is None else excluded)
    except ValueError as error:
        reason = str(error)
        return _unknown(launch, reason if _known(seat, reason) else 'record could not be classified')
    except OSError:
        return _unknown(launch, 'record unreadable')
    if times is None:
        return _unknown(launch, UNDECLARED.format(seat.harness.name))
    if not times:
        return _unknown(launch, 'no person input recorded')
    latest = max(times)
    if latest > now + FUTURE:
        return _unknown(launch, 'a recorded time is in the future')
    return dict(version=VERSION, launch=launch, at=latest.astimezone(timezone.utc).isoformat(), reason=None)


def observe(seat, now: datetime) -> dict:
    """Read the record and keep the fact; an unchanged fact is not rewritten."""
    fact = read(seat, now)
    path = seat.trusted / FACT
    text = json.dumps(fact, sort_keys=True) + '\n'
    try:
        if path.read_text() == text:
            return fact
    except (FileNotFoundError, UnicodeDecodeError):
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    durable.replace_file(path, text)
    return fact


def fact(seat) -> dict:
    """The kept fact for the current launch, read from its file alone, never from the record."""
    try:
        launched = seat.launched()
    except (OSError, ValueError, TypeError, KeyError):
        return _unknown(None, 'launch unreadable')
    launch = compaction.key_for(launched) if launched else None
    try:
        data = json.loads((seat.trusted / FACT).read_text())
    except FileNotFoundError:
        return _unknown(launch, 'not yet read by the service')
    except (OSError, ValueError, UnicodeDecodeError):
        return _unknown(launch, 'reading unreadable')
    if not isinstance(data, dict) or data.get('version') != VERSION:
        return _unknown(launch, 'reading unreadable')
    if (data.get('at') is None) == (data.get('reason') is None):  # exactly one of a time and a reason
        return _unknown(launch, 'reading unreadable')
    if launch is None or data.get('launch') != launch:
        return _unknown(launch, 'reading belongs to an earlier launch')
    if data['at'] is not None:
        try:
            at = datetime.fromisoformat(data['at'])
        except (TypeError, ValueError):
            return _unknown(launch, 'reading unreadable')
        if at.tzinfo is None:
            return _unknown(launch, 'reading unreadable')
    elif not _known(seat, data['reason']) and not (isinstance(data['reason'], str)
                                                   and data['reason'] == UNDECLARED.format(seat.harness.name)):
        return _unknown(launch, 'reading unreadable')
    return data


def quiet_for(data: dict, now: datetime) -> timedelta | None:
    if data.get('at') is None:
        return None
    return max(timedelta(0), now - datetime.fromisoformat(data['at']))


def _span(quiet: timedelta) -> str:
    minutes = int(quiet.total_seconds() // 60)
    if minutes < 1:
        return 'under a minute'
    hours, minutes = divmod(minutes, 60)
    return f'{hours}h{minutes:02d}m' if hours else f'{minutes}m'


def describe(data: dict, now: datetime) -> str:
    quiet = quiet_for(data, now)
    if quiet is None:
        return f"person's quiet time unknown: {data['reason']}"
    return f'person quiet for {_span(quiet)}'


def summary(seat, now: datetime | None = None) -> dict:
    """What qd watch and qd status show: plain words and the figures behind them, from the fact."""
    now = now or datetime.now(timezone.utc)
    data = fact(seat)
    quiet = quiet_for(data, now)
    return dict(text=describe(data, now), last_input_at=data.get('at'),
                quiet_seconds=int(quiet.total_seconds()) if quiet is not None else None,
                reason=data.get('reason'))


# -- one wake per readiness ---------------------------------------------------------------------

def readiness(seat) -> tuple[dict | None, bool]:
    """The identity of a standing manual readiness, and whether readiness is seen to be clear.

    Both are empty while the journal is unknown, unreadable or mid-transition: nothing then changes.
    """
    try:
        data = compaction.load(seat.ship.runtime, seat.name)
    except ShipError:
        return None, False
    if data is None:
        return None, not seat.spec.fresh_after_compactions
    if (data.get('error') or data.get('reading_unknown') or data.get('count') is None
            or data.get('transition') is not None):
        return None, False
    manual = data.get('manual')
    if isinstance(manual, str) and manual.startswith('ready for manual fresh'):
        try:
            launched = seat.launched()
        except (OSError, ValueError, TypeError, KeyError):
            return None, False
        if launched is None:
            return None, False
        return dict(launch=compaction.key_for(launched), generation=data['generation']), False
    return None, manual is None


def idle_hold(seat, state) -> str | None:
    """Why the seat is not idle, or None: the ship's one idle proof, which lets through only the seat's
    own proved, current inbox watch among its pending tools."""
    hold = idle.check(state, seat)
    if hold is not None and hold.dimension == 'tools-unknown':
        hold = idle.second(seat, idle.execution(seat, state), hold)[1]  # the screen, where the record cannot say
    return hold.reason if hold else None


def _wake(seat) -> dict | None:
    try:
        data = json.loads((seat.trusted / WAKE).read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeDecodeError):
        raise ShipError('person quiet wake journal unreadable; no wake posted') from None
    if not isinstance(data, dict) or data.get('version') != VERSION or not isinstance(data.get('readiness'), dict):
        raise ShipError('person quiet wake journal invalid; no wake posted')
    return data


def _save_wake(seat, data: dict) -> None:
    trusted_files.make(seat.ship.runtime, seat.name)
    durable.replace_file(seat.trusted / WAKE, json.dumps(data, sort_keys=True) + '\n')


def _post(seat, store, wake: dict) -> str:
    """Publish the order the journal names, unless an interrupted pass already published it."""
    try:
        return store.get(wake['order']).id
    except ShipError:
        pass
    message = store.prepare('order', post.SHIP, seat.name, ORDER)
    if message.id != wake['order']:
        wake['order'] = message.id
        _save_wake(seat, wake)  # the name first, so a later pass can tell whether it was published
    store.write(message)
    return message.id


def tick(seat, store, now: datetime | None = None) -> str | None:
    """One service pass for a seat in a human session: keep the fact, then post the wake if due.

    Returns the id of an order posted or adopted in this pass.
    """
    if not seat.session.human:
        return None  # the ship restarts its own seats; this is only for the person's session
    now = now or datetime.now(timezone.utc)
    data = observe(seat, now)
    if seat.spec.role != WAKES:
        return None  # every human seat is read; only the desk takes its own fresh start
    identity, clear = readiness(seat)
    wake = _wake(seat)
    if clear:
        if wake is not None:
            (seat.trusted / WAKE).unlink()
        return None
    if identity is None:
        return None
    if wake is not None and wake['readiness'] == identity:
        if wake.get('published'):
            return None
        posted = _post(seat, store, wake)
        _publish(seat, wake)
        return posted
    quiet = quiet_for(data, now)
    if quiet is None or quiet < QUIET:
        return None
    record = seat.record()
    if record is None:
        return None
    try:
        state = seat.harness.state(record)
    except OSError:
        return None
    if idle_hold(seat, state):
        return None
    wake = dict(version=VERSION, readiness=identity, intended=now.isoformat(), order=None, published=False)
    posted = _post(seat, store, wake)
    _publish(seat, wake)
    return posted


def _publish(seat, wake: dict) -> None:
    wake['published'] = True
    _save_wake(seat, wake)
