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

from quarterdeck import relaunch_intent, seat as seatmod, ship as shipmod

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
    assert seat.is_up and Path(seat.launched().argv[0]).name == 'bwrap'
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
