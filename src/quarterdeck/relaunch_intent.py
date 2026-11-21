"""A relaunch the service makes is one section a stop waits out, and its intent outlives a service that dies in it.

A relaunch closes a seat's pane and then opens a new one. A stop that ends the service between the two would leave
the seat down, with nothing said. Every relaunch the service makes runs inside ``section``, which:

- writes the intent, ``runtime/post/relaunch/<seat>.json``, before the down, naming the service that owns it;
- logs a ``relaunch`` line with phase ``begin`` before the down, and another with phase ``done`` (or ``failed``)
  after the up, so a stop in that window is never silent;
- holds a stop as a delivery does. The service's SIGTERM handler waits for the section to end, then exits, and
  ``qd``'s stop waits up to ``GRACE`` while an intent of that service is open (``held_by``).

The relaunches the service makes, and where they enter a section: a fresh start after an order
(Service.fresh_after_order), a relaunch at a request, a handoff or a renewed sign-in (Service.relaunch, kind
'relaunch'), a fresh
start after a provider refusal (refusal.relaunch), a move at a wall and a move by qd set (Service.failover and
Service.select, through Seat.failover_to), a fresh start after compactions (compaction.fresh), and the
bring-up of each seat after the ship's tmux server was lost (server_recovery).

A service that is killed inside a section leaves its intent. When ``GRACE`` runs out, the stop marks it released
and logs that (``release``). The next service finishes it once (``finish``), through Seat._up like any other
launch. It acts only on a real file, read without following a link, that names a declared seat. It acts only when
that seat is down and has no launch since the intent, and only when the intent's service is gone. It marks the
intent as finishing before the up, so a finish that is itself cut short is never tried again. That one waits for a
person, and the status line says so (``status``). law: nothing-waits-silently

An intent never overrides a later word that the seat stays as it is. A person's ``qd down``, ``qd stop`` or
``qd fresh``, and a service hold that keeps a seat down, remove the seat's intent and log who cleared it
(``clear``). The finish also leaves an intent alone while ``hold`` names a reason: the seat declared
``up = false``, its recovery or refusal hold, a relaunch refused while its program was ending, a program still
ending, a stopped ship-server recovery, or a hold registered in ``HOLDS``. The status line names the reason.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
from pathlib import Path
import stat

from . import post_dir, ship_locks, shutdown
from .harness import resume_target
from .ship import ShipError

# The bound a stop waits for a relaunch, from the stop's signal. Measured on the crew (15 Nov), a fresh start's up
# took 6 to 15 s after its down had closed the pane, and the down itself ended at once for an idle seat. 30 s is
# twice the slowest of those ups, with room for a down that ends within a few seconds. It is also the bound a stop
# already waits for a delivery, so a sync's stop is never held longer than it could already be held. A section
# still open at the bound is taken as hung. The stop then kills the service, and its intent is left for the next.
GRACE = 30.0
LIMIT = 4096  # an intent is a few hundred bytes; a larger file is not one
RELAUNCHING, RELEASED, FINISHING, FAILED = 'relaunching', 'released', 'finishing', 'failed'
KINDS = frozenset({'after-order', 'relaunch', 'refusal', 'failover', 'select', 'compaction', 'server-recovery', 'fresh-handoff'})
ROUTING = frozenset({'after-order', 'relaunch', 'refusal', 'fresh-handoff'})
_active = ContextVar('relaunch_intent', default=None)
_selection = ContextVar('relaunch_pool_selection', default=None)


# A hold that keeps a seat down, added later, registers a reader here: seat -> reason or None. Its writer calls
# clear() when it places the hold, so an intent never brings the seat up against it.
HOLDS: list = []


def folder(runtime) -> Path:
    return Path(runtime) / 'post' / 'relaunch'


def path(runtime, seat: str) -> Path:
    return folder(runtime) / f'{seat}.json'


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(runtime, event: str, **data) -> None:
    """A line in the service's log, written as the service writes its own: for the stop, which is not the service."""
    log = Path(runtime) / 'log' / 'service.jsonl'
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open('a') as stream:
        stream.write(json.dumps({'at': datetime.now().astimezone().isoformat(timespec='seconds'),
                                 'event': event, **data}) + '\n')


def _controller(runtime):
    """The running service's stop controller for this ship, or None: a person's qd, a check or a test."""
    controller = shutdown._controller
    if controller is None or Path(controller.runtime) != Path(runtime):
        return None
    return controller


def _write(runtime, data: dict) -> None:
    post_dir.ensure_directory(folder(runtime))
    post_dir.replace_bytes(path(runtime, data['seat']), (json.dumps(data) + '\n').encode())


def _remove(runtime, seat: str) -> None:
    try:
        post_dir.remove(path(runtime, seat))
    except FileNotFoundError:
        pass


def _owner_valid(owner) -> bool:
    return (isinstance(owner, dict) and type(owner.get('pid')) is int and owner['pid'] > 0
            and isinstance(owner.get('start'), str) and bool(owner['start']))


def _destination_valid(destination) -> bool:
    return (isinstance(destination, dict) and destination.keys() == {'pool', 'harness'}
            and all(isinstance(value, str) and bool(value) for value in destination.values()))


def _target_valid(target) -> bool:
    return (isinstance(target, dict) and target.keys() == {'id', 'record'}
            and all(isinstance(value, str) and bool(value) for value in target.values())
            and Path(target['record']).is_absolute())


def read(runtime, name: str, seats) -> dict:
    """One intent, by its file name, read without following a link and checked whole; ShipError says why not.

    ``seats`` are the ship's declared seat names: an intent for any other name is refused and never acted on.
    """
    if not name.endswith('.json') or name[:-5] not in seats:
        raise ShipError(f'relaunch intent {name}: not a declared seat; refused, never launched')
    try:
        raw = post_dir.read_bytes(folder(runtime) / name, limit=LIMIT)
        data = json.loads(raw)
    except ShipError as error:
        raise ShipError(f'relaunch intent {name}: {error}; refused') from None
    except (OSError, ValueError) as error:
        raise ShipError(f'relaunch intent {name}: unreadable ({type(error).__name__}); refused') from None
    if (not isinstance(data, dict) or data.get('version') != 1 or data.get('seat') != name[:-5]
            or data.get('kind') not in KINDS or type(data.get('resume')) is not bool
            or type(data.get('needs_destination', False)) is not bool
            or ('destination' in data and not _destination_valid(data['destination']))
            or ('target' in data and not (data['resume'] and _target_valid(data['target'])))
            or not isinstance(data.get('at'), str) or not _owner_valid(data.get('owner'))
            or data.get('phase') not in (RELAUNCHING, RELEASED, FINISHING, FAILED)
            or not (data.get('before') is None or isinstance(data.get('before'), str))):
        raise ShipError(f'relaunch intent {name}: not an intent this service wrote; refused')
    try:
        datetime.fromisoformat(data['at'])
    except ValueError:
        raise ShipError(f'relaunch intent {name}: its time is unreadable; refused') from None
    return data


def _entries(runtime, declared):
    """(name, intent or None, refusal or None) for each *.json at the intents' place; [] when there is none.

    ``declared`` gives the ship's declared seat names, read only once an intent is there.
    """
    try:
        children = post_dir.list_children(folder(runtime))
    except FileNotFoundError:
        return []
    except ShipError as error:
        return [('relaunch', None, f'relaunch intents: {error}')]
    if not any(name.endswith('.json') for name, _ in children):
        return []
    seats = set(declared())
    found = []
    for name, mode in sorted(children):
        if not name.endswith('.json'):
            continue
        if stat.S_ISLNK(mode):
            found.append((name, None, f'relaunch intent {name}: a link at its place; refused and left where it is'))
            continue
        try:
            found.append((name, read(runtime, name, seats), None))
        except ShipError as error:
            found.append((name, None, str(error)))
    return found


def clear(runtime, seat: str, who: str) -> bool:
    """A later word that the seat stays as it is: its intent goes, logged as cleared by ``who``. A link is left."""
    target = path(runtime, seat)
    try:
        mode = post_dir.mode(target)
    except (OSError, ShipError):
        return False
    if mode is None or not stat.S_ISREG(mode):
        return False  # nothing to clear, or a link or other file finish() already refuses and the line names
    with _intent_lock(runtime, seat):
        try:
            kind = json.loads(post_dir.read_bytes(target, limit=LIMIT)).get('kind')
        except (OSError, ShipError, ValueError, AttributeError):
            kind = None
        try:
            post_dir.remove(target)
        except FileNotFoundError:
            return False
    _log(runtime, 'relaunch-intent', seat=seat, kind=kind, outcome='cleared', reason=f'cleared by {who}')
    return True


def clear_all(ship, who: str, *, keep_server_choices: bool = False) -> list[str]:
    """Clear for a word about the whole ship, retaining unfinished choices at a server recovery stop."""
    return [name for name in sorted(ship.seats) if not (keep_server_choices and pending_server_choice(ship, name))
            and clear(ship.runtime, name, who)]


def held(seat) -> str | None:
    """hold(), with a reading that fails taken as a hold: an unknown is never read as nothing holding the seat."""
    try:
        return hold(seat)
    except Exception as error:
        return f'hold unknown ({type(error).__name__})'


def hold(seat) -> str | None:
    """Why the seat must stay as it is, whatever an intent says; None when nothing holds it."""
    if getattr(seat.spec, 'up', True) is False:
        return 'declared up = false'
    from . import recovery, refusal, server_recovery
    from .seat import REFUSED_RELAUNCH
    runtime = seat.ship.runtime
    try:
        if reason := recovery.held(seat):
            return reason
    except (OSError, ShipError) as error:
        return f'recovery hold unreadable ({type(error).__name__})'
    try:
        if any(event['stage'] == 'held' for event in refusal.load(runtime, seat.name)):
            return 'its refusal hold'
    except (OSError, ShipError, KeyError, TypeError) as error:
        return f'refusal journal unreadable ({type(error).__name__})'
    if (seat.trusted / REFUSED_RELAUNCH).exists():
        return 'a relaunch was refused while its program was ending'
    if seat.retiring() is not None:
        return 'its last program is still ending'
    try:
        if server_recovery.read(runtime)['stopped']:
            return 'ship server recovery stopped'
    except (OSError, ShipError, ValueError, KeyError):
        return 'ship server recovery state unreadable'
    from . import write_bound  # noqa: F401  registers its reader, wherever this is read
    for reader in HOLDS:
        if reason := reader(seat):
            return reason
    return None


def _needs_destination(spec, kind: str) -> bool:
    """A move may have chosen a pool not preserved by the resume flag or old selection."""
    if kind == 'server-recovery':
        return len(getattr(spec, 'pools', ())) > 1  # qd up chooses from tables and plain lists
    return kind in ('select', 'failover') or (kind in ROUTING and len(getattr(spec, 'pools', ())) > 1)


def _destination_required(spec, intent) -> bool:
    return intent.get('needs_destination', False) or _needs_destination(spec, intent['kind'])


def pending_server_choice(ship, name: str) -> bool:
    """A new server pass must leave an interrupted pool choice to its intent's recovery."""
    target = path(ship.runtime, name)
    try:
        if post_dir.mode(target) is None:
            return False
        intent = read(ship.runtime, target.name, ship.seats)
    except (OSError, ShipError, TypeError):
        return True  # an unreadable intent cannot prove a new choice is safe
    return intent['kind'] == 'server-recovery' and (
        'destination' in intent or _destination_required(ship.seats[name], intent))


def _intent_lock(runtime, seat):
    return ship_locks.held(ship_locks.path(runtime, 'relaunch', f'{seat}.lock'), f'{seat}: relaunch intent')


def record_destination(seat, resume: bool, *, pool: str | None = None, harness: str | None = None,
                       target: resume_target.Target | None = None) -> None:
    """Record the chosen pool and actual resume mode under the caller's lifecycle claim, before retirement."""
    active = _active.get()
    if active is None or active[0] is not seat:
        return  # an ordinary launch, outside this seat's service transaction
    _, intent = active
    destination = {'pool': pool if pool is not None else seat.pool.name,
                   'harness': harness if harness is not None else seat.spec.harness}
    runtime = seat.ship.runtime
    name = path(runtime, seat.name).name
    with _intent_lock(runtime, seat.name):
        if post_dir.mode(path(runtime, seat.name)) is None:
            return  # a person's later word cleared it
        current = read(runtime, name, {seat.name})
        if current != intent:
            raise ShipError(f'{seat.name}: relaunch intent changed before its destination was recorded')
        current.update(destination=destination, resume=resume)
        if target is not None:
            current['target'] = dict(id=target.id, record=str(target.record))
        _write(runtime, current)


@contextmanager
def selecting_pool(seat, pool):
    token = _selection.set((seat, pool))
    try:
        yield
    finally:
        _selection.reset(token)


def explicit_pool(seat, pool: str) -> str | None:
    """The operator's choice applies only to the launch being built for this seat and pool."""
    return pool if _selection.get() == (seat, pool) else None


@contextmanager
def section(service, seat, kind: str, *, resume, selected_pool=None):
    with selecting_pool(seat, selected_pool), _section(service, seat, kind, resume=resume):
        yield


@contextmanager
def _section(service, seat, kind: str, *, resume):
    """One relaunch of ``seat``: intent and log line before the down, log line after the up, a stop held between.

    Outside a running service (a person's qd, a check, a test) it does nothing and reads nothing. ``resume`` is
    the initial resume mode: a bool, or a callable read only when the intent is written. A seat that selects a
    destination records its effective mode and destination before retirement. ``service`` logs the lines; without
    one they go to the service's log directly.
    """
    if shutdown._controller is None:
        yield
        return
    runtime = seat.ship.runtime
    controller = _controller(runtime)
    if controller is None:
        yield
        return
    log = service.log if service is not None else (lambda event, **data: _log(runtime, event, **data))
    if controller.requested and not (controller.delivering or controller.relaunching):
        raise SystemExit(0)  # a stop already asked: nothing new is started
    previous = controller.relaunching
    controller.relaunching = True  # from here the handler holds a stop until the section ends
    try:
        launched = seat.launched()
        intent = dict(version=1, seat=seat.name, kind=kind, resume=bool(resume() if callable(resume) else resume),
                      at=_now(), phase=RELAUNCHING, owner=controller.owner,
                      before=launched.at if launched is not None else None)
        if _needs_destination(getattr(seat, 'spec', None), kind):
            intent['needs_destination'] = True  # even if its choices change before recovery
        _write(runtime, intent)
        log('relaunch', seat=seat.name, kind=kind, phase='begin')
        outcome = 'failed'
        active = _active.set((seat, intent))
        try:
            yield
            outcome = 'done'
        except BaseException as error:
            outcome = f'failed: {type(error).__name__}'
            raise
        finally:
            _active.reset(active)
            _remove(runtime, seat.name)
            log('relaunch', seat=seat.name, kind=kind, phase=outcome)
    finally:
        controller.relaunching = previous
        if controller.requested and not previous and not controller.delivering:
            raise SystemExit(0)  # the stop asked during the relaunch takes effect now that the seat is up


def held_by(runtime, owner) -> list[str]:
    """The seats whose relaunch the service ``owner`` has open: the stop waits for these, up to GRACE."""
    if owner is None:
        return []
    try:
        children = post_dir.list_children(folder(runtime))
    except (OSError, ShipError):
        return []
    held = []
    for name, mode in sorted(children):
        if not name.endswith('.json') or not stat.S_ISREG(mode):
            continue
        try:
            data = json.loads(post_dir.read_bytes(folder(runtime) / name, limit=LIMIT))
        except (OSError, ShipError, ValueError):
            continue
        if isinstance(data, dict) and data.get('owner') == owner and data.get('phase') == RELAUNCHING:
            held.append(name[:-5])
    return held


def release(runtime, owner) -> list[str]:
    """After the stop killed ``owner`` at GRACE: its open intents are marked released and logged, for the next."""
    released = []
    for seat in held_by(runtime, owner):
        try:
            data = json.loads(post_dir.read_bytes(path(runtime, seat), limit=LIMIT))
            data.update(phase=RELEASED, released_at=_now())
            _write(runtime, data)
        except (OSError, ShipError, ValueError):
            continue
        _log(runtime, 'relaunch-released', seat=seat, kind=data.get('kind'), pid=owner.get('pid'),
             detail=f'the stop waited {GRACE:g}s for the relaunch and killed the service; the next service '
                    'finishes its up')
        released.append(seat)
    return released


def _gone(owner) -> bool:
    return shutdown.process_gone(owner)


def _had_up(seat, data) -> bool:
    """Whether the seat has a launch made after the intent: the up it named has happened."""
    launched = seat.launched()
    if launched is None or launched.at == data['before']:
        return False
    try:
        return datetime.fromisoformat(launched.at) >= datetime.fromisoformat(data['at'])
    except (TypeError, ValueError):
        return False


def finish(service) -> None:
    """Each intent a gone service left: finished once through Seat._up, cleared, or held for a person; all logged."""
    from . import lifecycle

    runtime = service.ship.runtime
    refused = getattr(service, 'relaunch_refused', None)
    if refused is None:
        refused = service.relaunch_refused = set()
    own = getattr(_controller(runtime), 'owner', None)
    for name, data, refusal in _entries(runtime, lambda: service.ship.seats):
        if refusal is not None:
            if refusal not in refused:  # once per service; the status line keeps saying it
                refused.add(refusal)
                service.log('relaunch-intent', file=name, outcome='refused', reason=refusal)
            continue
        if data['owner'] == own or not _gone(data['owner']):
            continue  # its own service is still running: its section, not this one's
        seat = service.seat(data['seat'])
        if held(seat) is not None:
            continue  # a later word or a hold keeps the seat as it is; the status line names it
        try:
            with seat._lifecycle(automatic=True):
                try:
                    current = read(runtime, name, set(service.ship.seats))
                except ShipError:
                    continue  # changed under the claim: the next pass reads it again
                if current != data:
                    continue
                if data['kind'] == 'fresh-handoff':
                    _remove(runtime, seat.name)
                    service.log('relaunch-intent', seat=seat.name, kind=data['kind'], outcome='cleared',
                                reason='the durable handoff records whether its fresh start completed')
                    continue
                if seat.is_up or _had_up(seat, data):
                    _remove(runtime, seat.name)
                    service.log('relaunch-intent', seat=seat.name, kind=data['kind'], outcome='cleared',
                                reason='the seat is up' if seat.is_up else 'an up already followed it')
                    continue
                if data['phase'] in (FINISHING, FAILED) or held(seat) is not None:
                    continue  # tried once already, or held: a person decides, and the status line says so
                destination = data.get('destination')
                if destination is None and _destination_required(getattr(seat, 'spec', None), data):
                    reason = 'its destination pool was not recorded'
                    _write(runtime, {**data, 'phase': FAILED, 'error': reason})
                    service.log('relaunch-intent', seat=seat.name, kind=data['kind'], outcome='refused', reason=reason)
                    continue
                left_by = data['owner']['pid']
                data.update(phase=FINISHING, finisher=own, finishing_at=_now())
                _write(runtime, data)  # before the up: a finish cut short is never tried twice
                service.log('relaunch', seat=seat.name, kind=data['kind'], phase='finish-begin', left_by=left_by)
                try:
                    saved = data.get('target')
                    expected = resume_target.Target(saved['id'], Path(saved['record'])) if saved else None
                    if destination is None:
                        resume = data['resume'] and seat.resumable()
                        target = seat._require_resume(expected=expected) if resume else None
                        proof = seat._up(resume=resume, target=target)
                    else:
                        selected = destination['pool'] if data['kind'] == 'select' else None
                        with selecting_pool(seat, selected):
                            proof = seat._up_on_recorded_pool(**destination, resume=data['resume'], expected=expected)
                    if destination is not None and not data['resume'] and data['kind'] in ('failover', 'select', 'server-recovery'):
                        service.store.new('report', 'ship', seat.name,
                                          f"Moved to pool {destination['pool']} and {resume_target.NEW_CONVERSATION}.")
                except Exception as error:
                    data.update(phase=FAILED, error=str(error)[:200])
                    _write(runtime, data)
                    service.log('relaunch', seat=seat.name, kind=data['kind'], phase='finish-failed',
                                error=str(error)[:200])
                    continue
                _remove(runtime, seat.name)
                service.log('relaunch', seat=seat.name, kind=data['kind'], phase='finished', left_by=left_by,
                            proof=proof)
        except lifecycle.Busy:
            continue  # another owns the seat's transition: the next pass looks again


def _clock(stamp) -> str:
    try:
        return datetime.fromisoformat(stamp).astimezone().strftime('%H:%M')
    except (TypeError, ValueError):
        return '?'


def status(ship, seats=None) -> list[str]:
    """The status line's words for every intent not being carried by a running service.

    ``seats``: the ship's Seat objects by name, to read a hold that keeps one down.
    """
    lines = []
    for name, data, refusal in _entries(ship.runtime, lambda: ship.seats):
        if refusal is not None:
            lines.append(refusal)
            continue
        seat = data['seat']
        if data['phase'] == RELAUNCHING and not _gone(data['owner']):
            continue  # a relaunch in progress
        reason = None
        if seats and seat in seats:
            reason = held(seats[seat])
        elif getattr(ship.seats.get(seat), 'up', True) is False:
            reason = 'declared up = false'
        if reason is not None:
            lines.append(f"{seat}: relaunch intent left as it is while held: {reason}")
            continue
        if data['phase'] in (RELAUNCHING, RELEASED):
            how = ('released by a stop that waited for it' if data['phase'] == RELEASED
                   else 'left by a service that ended')
            lines.append(f"{seat}: relaunch at {_clock(data.get('released_at') or data['at'])} {how}, "
                         "unfinished; the next service finishes its up")
        elif data['phase'] == FINISHING and _owner_valid(data.get('finisher')) and not _gone(data['finisher']):
            continue  # being finished now
        else:
            reason = data.get('error') or 'its finish was cut short'
            if data['resume'] and f'Use qd fresh {seat} to start a new conversation' in reason:
                lines.append(reason)
            else:
                lines.append(f"{seat}: relaunch not finished from its intent ({reason}); needs a human: qd up {seat}")
    return lines
