"""A relaunch left between its down and its up by a killed service: the next service finishes its up once.

Real launches on the drydock, on a private tmux server, through Seat._up as every launch goes.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import relaunch_intent, sandbox, seat as seatmod, ship as shipmod

DEAD = dict(version=1, pid=999999999, start='synthetic-start-zzzz', token='synthetic-owner-zzzz', device=1, inode=1)


@pytest.fixture
def ship(drydock: Path) -> shipmod.Ship:
    return shipmod.load(drydock)


def service(ship, make):
    logs = []
    return SimpleNamespace(runtime=ship.runtime, ship=ship, logs=logs, seat=make,
                           log=lambda event, **data: logs.append((event, data)))


def leave(ship, seat='worker-1', at=None, **extra):
    """The intent a service killed after its down, before its up, leaves behind."""
    folder = relaunch_intent.folder(ship.runtime)
    folder.mkdir(parents=True, exist_ok=True)
    data = dict(version=1, seat=seat, kind='after-order', resume=False,
                at=(at or datetime.now(timezone.utc)).isoformat(), phase=relaunch_intent.RELAUNCHING,
                owner=DEAD, before=None, **extra)
    relaunch_intent.path(ship.runtime, seat).write_text(json.dumps(data) + '\n')


def phases(svc):
    return [data.get('phase') or data.get('outcome') for event, data in svc.logs]


def test_an_intent_left_by_a_killed_service_is_finished_once_and_never_twice(ship, tmux):
    worker = lambda name: seatmod.Seat(ship, ship.seat(name), tmux)
    leave(ship)
    first = service(ship, worker)
    relaunch_intent.finish(first)
    assert phases(first) == ['finish-begin', 'finished'] and first.logs[1][1]['left_by'] == DEAD['pid']
    seat = worker('worker-1')
    assert seat.is_up, 'the next service finished the up'
    launched = seat.launched()
    assert not relaunch_intent.path(ship.runtime, 'worker-1').exists()
    second = service(ship, worker)
    relaunch_intent.finish(second)
    assert second.logs == [] and seat.launched() == launched, 'a second next service does nothing'
    # A finish cut short after its up and before its intent went: the seat is up, so the intent is only cleared.
    leave(ship, at=datetime.now(timezone.utc) - timedelta(minutes=1))
    third = service(ship, worker)
    relaunch_intent.finish(third)
    assert phases(third) == ['cleared'] and seat.launched() == launched, 'never a second up'
    seat.down()


def test_a_finish_that_was_itself_cut_short_waits_for_a_person(ship, tmux):
    worker = lambda name: seatmod.Seat(ship, ship.seat(name), tmux)
    leave(ship, finisher=DEAD)
    data = json.loads(relaunch_intent.path(ship.runtime, 'worker-1').read_text())
    relaunch_intent.path(ship.runtime, 'worker-1').write_text(json.dumps({**data, 'phase': 'finishing'}) + '\n')
    svc = service(ship, worker)
    relaunch_intent.finish(svc)
    assert svc.logs == [] and not worker('worker-1').is_up
    assert relaunch_intent.status(ship) == [
        'worker-1: relaunch not finished from its intent (its finish was cut short); needs a human: qd up worker-1']


def test_a_jailed_seat_finished_from_an_intent_passes_its_own_folder_check(ship, tmux, tmp_path):
    from .test_sandbox import namespaces_allowed

    if not namespaces_allowed():
        pytest.skip('bubblewrap namespace unavailable')
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    jailed = lambda name: seatmod.Seat(ship, replace(ship.seat(name), cwd=workspace, sandbox=True), tmux)
    leave(ship)
    svc = service(ship, jailed)
    relaunch_intent.finish(svc)
    assert phases(svc) == ['finish-begin', 'finished'] and 'jailed' in svc.logs[1][1]['proof']
    seat = jailed('worker-1')
    assert seat.is_up and sandbox.seats_hidden(seat.launched().argv) is True
    seat.down()
    # The same finish for a jail its folder check refuses: the check ran at the finish, nothing started.
    in_ship = lambda name: seatmod.Seat(ship, replace(ship.seat(name), cwd=ship.folder, sandbox=True), tmux)
    leave(ship)
    refused = service(ship, in_ship)
    relaunch_intent.finish(refused)
    assert phases(refused) == ['finish-begin', 'finish-failed']
    assert 'ship folder as its working directory' in refused.logs[1][1]['error']
    assert not in_ship('worker-1').is_up
    assert 'needs a human: qd up worker-1' in relaunch_intent.status(ship)[0]


# -- an intent never overrides a later word that the seat stays as it is ----------------------

def cleared(ship):
    log = ship.runtime / 'log' / 'service.jsonl'
    entries = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return [entry['reason'] for entry in entries if entry.get('outcome') == 'cleared']


@pytest.fixture
def person(ship, tmux, monkeypatch):
    """qd run by Sentry on this ship, on the test's private server."""
    import argparse
    from quarterdeck import cli

    monkeypatch.setenv('QD_TMUX_SOCKET', tmux.socket)
    monkeypatch.setenv('QD_SEAT', 'sentry')
    monkeypatch.setenv('QD_SHIP', str(ship.folder))
    monkeypatch.setattr(cli, 'seat_of', lambda found, name: seatmod.Seat(found, found.seat(name), tmux))
    return lambda verb, **extra: getattr(cli, f'cmd_{verb}')(argparse.Namespace(ship=str(ship.folder), **extra))


def test_an_intent_then_qd_down_then_a_new_service_leaves_the_seat_down(ship, tmux, person):
    worker = lambda name: seatmod.Seat(ship, ship.seat(name), tmux)
    leave(ship)
    person('down', seat='worker-1')
    assert cleared(ship) == ['cleared by qd down by sentry']
    assert not relaunch_intent.path(ship.runtime, 'worker-1').exists()
    svc = service(ship, worker)
    relaunch_intent.finish(svc)
    assert svc.logs == [] and not worker('worker-1').is_up, 'the person kept it down'


def test_qd_down_of_the_whole_ship_clears_every_intent(ship, tmux, person):
    leave(ship, seat='worker-1')
    leave(ship, seat='captain')
    person('down', seat=None)
    assert sorted(cleared(ship)) == ['cleared by qd down by sentry'] * 2
    assert not any(relaunch_intent.folder(ship.runtime).iterdir())


def test_qd_stop_clears_the_intent_even_with_no_service_to_ask(ship, tmux, person):
    from quarterdeck.ship import ShipError

    leave(ship)
    with pytest.raises(ShipError, match='service down'):
        person('stop', seat='worker-1')
    assert cleared(ship) == ['cleared by qd stop by sentry']


def test_qd_fresh_clears_the_intent_and_starts_the_seat_once(ship, tmux, person):
    worker = lambda name: seatmod.Seat(ship, ship.seat(name), tmux)
    leave(ship)
    person('fresh', seat='worker-1', cancel=None)
    assert cleared(ship) == ['cleared by qd fresh by sentry']
    seat = worker('worker-1')
    launched = seat.launched()
    svc = service(ship, worker)
    relaunch_intent.finish(svc)
    assert svc.logs == [] and seat.launched() == launched, 'one up, the person\'s'
    seat.down()


def test_an_intent_for_a_seat_declared_up_false_is_never_finished(ship, tmux, drydock):
    text = (drydock / 'ship.toml').read_text()
    (drydock / 'ship.toml').write_text(shipmod.edit(text, 'seats.worker-1', 'up', False))
    declared = shipmod.load(drydock)
    assert declared.seat('worker-1').up is False
    worker = lambda name: seatmod.Seat(declared, declared.seat(name), tmux)
    leave(declared)
    svc = service(declared, worker)
    relaunch_intent.finish(svc)
    assert svc.logs == [] and not worker('worker-1').is_up
    assert relaunch_intent.path(declared.runtime, 'worker-1').exists()
    assert relaunch_intent.status(declared) == [
        'worker-1: relaunch intent left as it is while held: declared up = false']


@pytest.mark.parametrize('how', ['recovery', 'refused-relaunch', 'registered'])
def test_an_intent_for_a_held_seat_is_never_finished_and_shows_on_the_line(ship, tmux, monkeypatch, how):
    from quarterdeck import recovery, watch
    from quarterdeck.seat import REFUSED_RELAUNCH

    worker = lambda name: seatmod.Seat(ship, ship.seat(name), tmux)
    reason = {'recovery': 'synthetic recovery hold',
              'refused-relaunch': 'a relaunch was refused while its program was ending',
              'registered': 'synthetic write-bound hold'}[how]
    if how == 'recovery':
        monkeypatch.setattr(recovery, 'held', lambda seat: reason if seat.name == 'worker-1' else None)
    elif how == 'refused-relaunch':
        folder = worker('worker-1').trusted
        folder.mkdir(parents=True, exist_ok=True)
        (folder / REFUSED_RELAUNCH).write_text('{}\n')
    else:
        monkeypatch.setattr(relaunch_intent, 'HOLDS', [lambda seat: reason if seat.name == 'worker-1' else None])
    leave(ship)
    svc = service(ship, worker)
    relaunch_intent.finish(svc)
    assert svc.logs == [] and not worker('worker-1').is_up
    line = watch.Facts('ok', 0, 0, 0, relaunches=relaunch_intent.status(ship, {'worker-1': worker('worker-1')})).line()
    assert f'worker-1: relaunch intent left as it is while held: {reason}' in line


def test_a_hold_the_service_places_clears_the_intent_and_says_who(ship):
    leave(ship)
    assert relaunch_intent.clear(ship.runtime, 'worker-1', 'its refusal hold')
    assert cleared(ship) == ['cleared by its refusal hold']
    assert not relaunch_intent.clear(ship.runtime, 'worker-1', 'its refusal hold'), 'nothing left to clear'
    link = relaunch_intent.path(ship.runtime, 'worker-1')
    link.symlink_to(ship.folder / 'ship.toml')
    assert not relaunch_intent.clear(ship.runtime, 'worker-1', 'its refusal hold') and link.is_symlink()
