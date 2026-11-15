"""qd check's human-session waking line, for a jailed seat, with no service up: as qd setup runs it, before it
starts anything.

A jailed `qd inbox --wait` cannot open its own delivery attempts; it asks a broker to open them and offer them.
Only the service brokered, so under setup the wait was never offered its message and the check failed with
'waiting message has no watch receipt' (a pi desk on a local model, Una's clone of main 5a7c658d). The check now
serves its own wake seat while no service owns the ship, through the service's own broker pass.

Real bubblewrap and the fake program; no model. The fake program takes the waking line and does nothing with it,
so this test plays the agent: once the line is in the seat's record, it runs the real `qd inbox --wait` and then
`qd post report` inside the seat's own jail, as an agent does at its prompt.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from quarterdeck import check, post, sandbox, servicectl, watch_broker
from quarterdeck.seat import temporary_seat
from quarterdeck.ship import SeatSpec

from .test_jailed_wait import EXACT, QD, inputs, ship  # noqa: F401  (ship is the jailed desk's fixture)
from .test_sandbox import namespaces_allowed
from .waiting import wait_for

pytestmark = pytest.mark.skipif(not namespaces_allowed(), reason="bubblewrap is not installed, or cannot make a namespace here (inside a jail)")

NAME = 'check-wake-fake-5e1d'
TOKEN = re.compile(r'wake-answer-[0-9a-f]{8}')


def in_jail(seat, argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    """A command at the seat's own prompt: its own jail, as its launch builds it."""
    jail = sandbox.plan(seat.ship, seat.name, seat.folder, seat.home, Path(seat.spec.cwd), sys.executable,
                        os.environ['PATH'])
    env = {key: value for key, value in os.environ.items() if not key.startswith(('QD_', 'TMUX'))}
    env.update(QD_SEAT=seat.name, QD_SHIP=str(seat.ship.folder))
    return subprocess.run(sandbox.command(jail, argv), env=env, capture_output=True, text=True, timeout=timeout)


def test_a_jailed_check_seat_is_woken_served_and_answers_with_no_service_up(ship, tmux, monkeypatch):  # noqa: F811
    monkeypatch.setattr(check, 'PATIENCE', 45.0)  # bounds the failure on a check that serves nothing
    assert not servicectl.alive(ship.runtime), 'no service: as qd setup checks, before it starts anything'
    spec = SeatSpec(NAME, f'qd-{NAME}', 'worker', 'fake', ('fake',), cwd=ship.seat('desk').cwd, sandbox=True)
    orders: list[post.Message] = []
    seen: dict = {}

    def agent(seat):
        """What the waking line asks: run the wait, read the order, report on it with the body it names."""
        try:
            if not wait_for(lambda: EXACT in inputs(seat), seconds=60):
                seen['error'] = 'the waking line never reached the seat'
                return
            waited = in_jail(seat, [str(QD), 'inbox', '--wait'], timeout=60)
            seen['wait'] = waited
            token = TOKEN.search(waited.stdout)
            if waited.returncode != 0 or token is None or not orders:
                return
            seen['post'] = in_jail(seat, [str(QD), 'post', 'report', '--re', orders[0].id, token.group(0)], timeout=60)
        except Exception as error:
            seen['error'] = repr(error)

    with temporary_seat(ship, tmux, spec, orders, 'qd check human wake', human=True, check_inbox=True) as seat:
        assert seat.name not in ship.seats, 'undeclared: only the check lease admits it'
        player = threading.Thread(target=agent, args=(seat,), daemon=True)
        player.start()
        checks = check.probe_wake(seat, orders)
        player.join(timeout=90)
        assert sandbox.seats_hidden(seat.launched().argv) is True, 'the wake seat runs jailed'

    rows = [(c.name, c.passed, c.detail) for c in checks]
    assert all(passed is True for _, passed, _ in rows), (rows, seen)
    assert 'error' not in seen, seen
    wait = seen.get('wait')
    assert wait is not None and wait.returncode == 0, (rows, wait and wait.stderr)
    assert orders and orders[0].short in wait.stdout, 'the wait printed the waiting order'
    assert [name for name, _, _ in rows] == [
        'human-session waking line: known-empty paste and record receipt',
        'human-session waking line: seat armed its inbox watch',
        'human-session waking line: watch read the waiting message',
        'human-session waking line: seat answered the message']
    attempts = post.Attempts(ship.runtime).for_message(orders[0].id)
    assert [(a.seat, a.result) for a in attempts] == [(NAME, 'watch')], 'one attempt, opened for it and closed by its receipt'
    assert not servicectl.alive(ship.runtime), 'the check served the seat itself; no service was started'


def bare(tmp_path):
    """A runtime with no service and no check: only what serve_check_seat reads."""
    from types import SimpleNamespace
    return SimpleNamespace(runtime=tmp_path / 'runtime', seats={})


def test_with_a_service_up_the_check_stands_aside(tmp_path, monkeypatch):
    """A live service owns the broker: the check's pass does nothing, for any seat."""
    ship = bare(tmp_path)
    monkeypatch.setattr(servicectl, 'alive', lambda runtime: True)
    called = []
    monkeypatch.setattr(watch_broker, 'run', lambda service, seat: called.append(seat.name))
    monkeypatch.setattr(watch_broker, 'check_admitted', lambda runtime, name: True)
    assert watch_broker.serve_check_seat(ship, NAME) is False and called == []


def test_a_seat_no_live_check_owns_is_never_served(tmp_path, monkeypatch):
    """Nor without a service: a seat folder alone admits nothing, only the check's own live lease on a person's seat."""
    ship = bare(tmp_path)
    assert not servicectl.alive(ship.runtime)
    called = []
    monkeypatch.setattr(watch_broker, 'run', lambda service, seat: called.append(seat.name))
    own = watch_broker.seat_folder(ship.runtime, NAME)
    own.mkdir(parents=True)
    (own / watch_broker.REQUEST).write_text('{"format": 1, "generation": "' + '0' * 32 + '", "lease": [1, 2]}\n')
    assert watch_broker.request(ship.runtime, NAME) is not None
    assert watch_broker.serve_check_seat(ship, NAME) is False and called == []
