"""Bring a ship back when its own tmux server has gone: proved, bounded, and through qd up's own path.

A seat counts as formerly up while its launch.json exists; qd down deletes it, so a deliberate down is never
resurrected. The server counts as gone only when its explicit socket is absent or refuses a connection and every
formerly-up launch's process is dead, so no second owner can exist. Anything else is unknown and starts nothing.
At most two attempts in an hour, each written durably before any effect: a crash counts, a clock that runs
backwards counts as exhausted, and a third attempt or any bring-up that is not whole stops until a person runs
qd up (law: nothing-waits-silently; law: context-is-cache).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import stat

from . import durable, relaunch_intent, resting_failover, trusted_files
from .ship import Ship, ShipError

FORMAT = 1
LIMIT = 2  # attempts within WINDOW
WINDOW = timedelta(hours=1)
KEEP = 8  # attempts kept on file
CLEARED = 'cleared by qd up'


def state_path(runtime: Path) -> Path:
    return runtime / 'recovery' / 'ship-server.json'


def read(runtime: Path) -> dict:
    """The saved state; absent is empty. Malformed raises: it is never read as empty."""
    try:
        data = json.loads(state_path(runtime).read_text())
    except FileNotFoundError:
        return dict(format=FORMAT, attempts=[], stopped=None, unknown=None)
    except (OSError, ValueError, UnicodeError):
        raise ShipError('ship server recovery state unreadable; inspect runtime/recovery/ship-server.json') from None
    if (not isinstance(data, dict) or data.get('format') != FORMAT or not isinstance(data.get('attempts'), list)
            or any(not isinstance(item, dict) or not isinstance(item.get('at'), str) for item in data['attempts'])):
        raise ShipError('ship server recovery state unreadable; inspect runtime/recovery/ship-server.json')
    data.setdefault('stopped', None)
    data.setdefault('unknown', None)
    return data


def save(runtime: Path, data: dict) -> None:
    target = state_path(runtime)
    target.parent.mkdir(parents=True, exist_ok=True)
    data['attempts'] = data['attempts'][-KEEP:]
    durable.replace_file(target, json.dumps(data, indent=2, sort_keys=True) + '\n')


def now() -> datetime:
    return datetime.now(timezone.utc)


def server_gone(tmux) -> tuple[bool | None, str]:
    """True only when the explicit socket is absent or refuses; False while a server answers; None is unknown."""
    address = getattr(tmux, 'socket', None)
    if not address or not os.path.isabs(address):
        try:  # no explicit socket: a server that answers is fine, and absence cannot be proved
            return (False, 'the ship server answers') if tmux.running() else (None, 'the ship server does not answer and its socket is not explicit')
        except Exception as error:
            return None, f'the ship server is unreadable: {type(error).__name__}'
    try:
        info = os.lstat(address)
    except FileNotFoundError:
        return True, 'the ship server socket is gone'
    except OSError as error:
        return None, f'the ship server socket is unreadable: {error.strerror}'
    if not stat.S_ISSOCK(info.st_mode):
        return None, 'the ship server socket path is not a socket'
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(2)
    try:
        probe.connect(address)
    except ConnectionRefusedError:
        return True, 'the ship server socket refuses connections'
    except OSError as error:
        return None, f'the ship server socket did not answer: {error.strerror or type(error).__name__}'
    finally:
        probe.close()
    return False, 'the ship server answers'


def pid_dead(pid) -> bool | None:
    """True when no such process exists; None when it exists or cannot be judged."""
    if type(pid) is not int or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return None
    return None  # alive, or reused: either way not proved gone


def formerly_up(ship: Ship, tmux) -> tuple[list, list[str]]:
    """Declared seats whose launch.json exists, and the names of undeclared ones that have one."""
    from .seat import Seat
    members = [Seat(ship, spec, tmux) for spec in ship.seats.values()
               if trusted_files.path(ship.runtime, spec.name, trusted_files.LAUNCH).exists()]
    folder = trusted_files.root(ship.runtime)
    stray = sorted(path.parent.name for path in folder.glob(f'*/{trusted_files.LAUNCH}') if path.parent.name not in ship.seats) \
        if folder.is_dir() else []
    return members, stray


def window(data: dict, moment: datetime) -> tuple[int, bool]:
    """Attempts within the last hour, and whether the clock is unknown (it reads earlier than an attempt)."""
    count, backwards = 0, False
    for item in data['attempts']:
        if item.get('outcome') == CLEARED:
            continue  # a person's clear lifts STOPPED; the automatic attempts of the hour still count
        try:
            at = datetime.fromisoformat(item['at'])
        except ValueError:
            return LIMIT, True
        if at.tzinfo is None or at > moment:
            backwards = True
        elif moment - at < WINDOW:
            count += 1
    return count, backwards


@resting_failover.reading_pass()
def pass_once(service) -> str | None:
    """One service pass: recover the ship if, and only if, its server is proved gone. Returns what it did."""
    ship, tmux, runtime = service.ship, service.tmux, service.ship.runtime
    data = read(runtime)
    if data['stopped']:
        return None  # a person's qd up clears it
    members, stray = formerly_up(ship, tmux)
    if not members:
        return _settle_unknown(runtime, data, None)
    for seat in members:  # one live formerly-up seat proves the server lives; a missing pane is the per-seat path
        try:
            if seat.is_up:
                return _settle_unknown(runtime, data, None)
        except Exception:
            continue
    gone, reason = server_gone(tmux)
    if gone is False:
        return _settle_unknown(runtime, data, None)
    if gone is None:
        return _settle_unknown(runtime, data, reason)
    launches = []
    for seat in members:
        try:
            launched = seat.launched()
        except (OSError, ValueError, TypeError, ShipError):
            return _settle_unknown(runtime, data, f'{seat.name} launch unreadable')
        if launched is None:
            continue  # taken down between the reads: a deliberate down wins
        if not pid_dead(launched.pid):
            return _settle_unknown(runtime, data, f'{seat.name} old launch process is not proved gone')
        launches.append((seat, launched))
    if not launches:
        return _settle_unknown(runtime, data, None)
    if getattr(tmux, 'legacy', False):
        # qd never starts tmux's default server. The service follows a ship whose default server holds none of
        # its sessions to its own, and the next pass recovers it there.
        return _settle_unknown(runtime, data, "the ship ran on tmux's default server; it is recovered on its own "
                                              "server once the service has followed it there")
    eligible, choosing = [], []
    for seat, proved in launches:
        try:
            with seat._lifecycle(automatic=True):
                if (relaunch_intent.pending_server_choice(ship, seat.name)
                        or resting_failover.hold_state(seat) is not resting_failover.Hold.CLEAR):
                    choosing.append(seat.name)
                else:
                    eligible.append((seat, proved))
        except ShipError:
            return _settle_unknown(runtime, data, f'{seat.name} lifecycle busy; waiting, no attempt used')
    if not eligible:
        return _settle_unknown(runtime, data, None)  # the intent's own status names what holds these seats
    moment = now()
    count, backwards = window(data, moment)
    if backwards or count >= LIMIT:
        data['stopped'] = ('the clock reads earlier than the last attempt' if backwards
                           else f'{count} attempts in the last hour')
        data['unknown'] = None
        save(runtime, data)
        relaunch_intent.clear_all(ship, 'ship server recovery stopped', keep_server_choices=True)
        _report(service, f'ship tmux server gone; recovery stopped ({data["stopped"]}); needs a human: qd up')
        return 'stopped'
    attempt = dict(at=moment.isoformat(), outcome='started', seats=[seat.name for seat, _ in eligible])
    data['attempts'].append(attempt)
    data['unknown'] = None
    save(runtime, data)  # consumed before any effect: a crash after this line still counts
    from . import fresh_handoff, ship as shipmod
    from .harness import resume_target
    failures = []
    try:
        shipmod.fence(ship)
    except (OSError, ShipError) as error:
        failures.append(f'fence: {error}')
    brought, downed, new_conversations = [], [], []
    for seat, proved in eligible:
        try:
            # Membership is re-read under the same lifecycle lock as the effect, which a qd down also takes:
            # a deliberate down that finished first wins, and one that comes later waits or is refused.
            try:
                with seat._lifecycle(automatic=True):
                    if seat.launched() != proved:
                        downed.append(seat.name)
                        continue
                    if (relaunch_intent.pending_server_choice(ship, seat.name)
                            or resting_failover.hold_state(seat) is not resting_failover.Hold.CLEAR):
                        choosing.append(seat.name)
                        continue
                    seat.defer_opening = True  # observe startup on a later service pass
                    with relaunch_intent.section(service, seat, 'server-recovery', resume=True):
                        seat._up_with_pool_choice(True, require_resume_target=True)
            except resume_target.Unproved as error:
                if seat.session.human:
                    raise
                result = fresh_handoff.request(seat, reason=error.reason, expected_launch=proved,
                                               service=service, restart=True)
                if result.disposition != 'started':
                    raise ShipError(result.proof) from error
                new_conversations.append(seat.name)  # the restart already reported to the seat
            else:
                launch = seat.launched()
                if launch is not None and launch.resume is None:
                    new_conversations.append(seat.name)
                    service.store.new('report', 'ship', seat.name,
                                      f'Tmux server recovery {resume_target.NEW_CONVERSATION}.')
            brought.append(seat)
        except Exception as error:  # every seat gets its one try; the outcome names each failure
            failures.append(f'{seat.name}: {str(error)[:200]}')
    for seat in brought:
        try:
            if not seat.is_up:
                failures.append(f'{seat.name}: not up after bring-up')
        except Exception as error:
            failures.append(f'{seat.name}: up state unknown: {str(error)[:120]}')
    attempt['outcome'] = 'recovered' if not failures else 'failed: ' + '; '.join(failures)[:800]
    if failures:
        data['stopped'] = 'recovery failed: ' + '; '.join(failures)[:400]
    save(runtime, data)
    if failures:
        relaunch_intent.clear_all(ship, 'ship server recovery stopped', keep_server_choices=True)
    k = count + 1
    if failures:
        _report(service, f'ship tmux server was gone; recovery attempt {k} of {LIMIT} this hour failed '
                         f'({"; ".join(failures)[:300]}); needs a human: qd up')
        return 'failed'
    note = f'; started a new conversation for {", ".join(new_conversations)}' if new_conversations else ''
    note += f'; not declared, left alone: {", ".join(stray)}' if stray else ''
    note += f'; taken down meanwhile, left down: {", ".join(downed)}' if downed else ''
    note += f'; unfinished pool choice, left down: {", ".join(choosing)}' if choosing else ''
    _report(service, f'ship tmux server was gone; recovered {len(brought)} seat(s) '
                     f'({", ".join(seat.name for seat in brought)}), attempt {k} of {LIMIT} this hour{note}')
    return 'recovered'


def _settle_unknown(runtime: Path, data: dict, reason: str | None) -> str | None:
    if data.get('unknown') != reason:
        data['unknown'] = reason
        save(runtime, data)
    return f'unknown: {reason}' if reason else None


def _report(service, text: str) -> None:
    ship = service.ship
    systems = [spec.name for spec in ship.seats.values() if spec.role == 'systems' and service._up(spec.name)]
    desks = [spec.name for spec in ship.seats.values() if spec.role == 'desk']
    for name in systems or desks:
        service.store.new('report', 'ship', name, text)


def status(runtime: Path) -> list[str]:
    """Status-line facts: STOPPED needs a human; an unknown server is said, never silent."""
    try:
        data = read(runtime)
    except ShipError as error:
        return [str(error)]
    lines = []
    if data['stopped']:
        lines.append(f'ship server gone: recovery stopped ({data["stopped"]}); needs a human: qd up')
    elif data['unknown']:
        lines.append(f'ship server unknown: {data["unknown"]}')
    return lines


def clear_by_person(runtime: Path) -> str | None:
    """A person's qd up of the whole ship clears a stopped recovery and says so."""
    try:
        data = read(runtime)
    except ShipError:
        return None
    if not data['stopped']:
        return None
    data['attempts'].append(dict(at=now().isoformat(), outcome=CLEARED, seats=[]))
    data['stopped'], data['unknown'] = None, None
    save(runtime, data)
    return 'ship server recovery cleared by qd up'
