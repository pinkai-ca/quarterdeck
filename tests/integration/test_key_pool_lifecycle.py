"""A seat on a key, end to end on the fake program: the key reaches the program and nothing else.

Every test reads a synthetic key from its own scratch file. The search at the end of a lifecycle covers every file qd
wrote and everything it printed, for the key and for its last eight characters."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import time

import pytest

from quarterdeck import doctor, keyfile, seat as seatmod, ship as shipmod, watch
from quarterdeck.harness.fake import FakeHarness
from quarterdeck.harness.types import Launch
from quarterdeck.ship import KeyRefused, ShipError
from quarterdeck.tmux import Tmux

from .waiting import start_with_conversation

KEY = 'sk-synthetic-Q7wM2xC9zT4lP8rV3nH6'
TAIL = KEY[-8:]
PATIENCE = 60.0


@pytest.fixture
def ship(drydock: Path, key_file) -> shipmod.Ship:
    key_file.write(paid=KEY)
    file = drydock / 'ship.toml'
    text = file.read_text() + '\n[pools.paid]\nprovider = "synthetic"\napi_key = "keys:paid"\n'
    text = shipmod.edit(text, 'seats.worker-1', 'pool', 'paid')
    text = shipmod.edit(text, 'seats.captain', 'pool', ['fake', 'paid'])
    file.write_text(text)
    return shipmod.load(drydock)


@pytest.fixture
def typed(tmux: Tmux, monkeypatch) -> list[tuple[str, ...]]:
    """Every argument handed to tmux."""
    seen, real = [], tmux._invoke

    def recording(*args, **options):
        seen.append(tuple(map(str, args)))
        return real(*args, **options)
    monkeypatch.setattr(tmux, '_invoke', recording)
    return seen


def tree(pid: int) -> list[int]:
    found, stack = [], [pid]
    while stack:
        current = stack.pop()
        found.append(current)
        try:
            for task in os.listdir(f'/proc/{current}/task'):
                stack += map(int, Path(f'/proc/{current}/task/{task}/children').read_text().split())
        except OSError:
            continue
    return found


def raw(pid: int, name: str) -> bytes:
    try:
        return Path(f'/proc/{pid}/{name}').read_bytes()
    except OSError:
        return b''


def holding_key(pane_pid: int) -> int:
    """The process of this pane that holds the key in its environment, once the launcher has handed it over."""
    deadline = time.monotonic() + PATIENCE
    while time.monotonic() < deadline:
        for pid in tree(pane_pid):
            if f'{keyfile.HELPER_VARIABLE}={KEY}'.encode() in raw(pid, 'environ').split(b'\0'):
                return pid
        time.sleep(0.02)
    raise AssertionError('no process of the pane holds the key')


def no_command_line_holds_it(pane_pid: int) -> None:
    for pid in tree(pane_pid):
        line = raw(pid, 'cmdline')
        assert KEY.encode() not in line and TAIL.encode() not in line, pid


def nothing_written_holds_it(ship: shipmod.Ship, *printed: str) -> None:
    for text in printed:
        assert KEY not in text and TAIL not in text, text
    holding = []
    for root, _, names in os.walk(ship.folder):
        for name in names:
            path = Path(root, name)
            if path.is_symlink() or not path.is_file():
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if KEY.encode() in data or TAIL.encode() in data:
                holding.append(str(path))
    assert holding == []


def test_the_key_reaches_the_program_and_no_command_line(ship: shipmod.Ship, tmux: Tmux, typed):
    worker = seatmod.Seat(ship, ship.seat('worker-1'), tmux)
    proof = worker.up()
    assert ' · pool paid · ' in proof
    pane = worker.pane()
    holding_key(pane.pid)
    no_command_line_holds_it(pane.pid)
    assert not any(KEY in word or TAIL in word for words in typed for word in words)
    launched = worker.launched()
    assert launched.pool == 'paid' and launched.argv[1:4] == ['-I', '-m', 'quarterdeck.key_launch']
    assert KEY not in ' '.join(launched.argv)
    script = (ship.runtime / 'trusted' / 'worker-1' / 'launch.sh')
    if script.exists():
        assert KEY not in script.read_text()
    worker.down()


def test_a_whole_lifecycle_holds_the_key_nowhere_qd_writes_or_prints(ship: shipmod.Ship, tmux: Tmux):
    """Launch, restart, fresh start, onto a key and off it, with status and doctor read at each step."""
    captain = seatmod.Seat(ship, ship.seat('captain'), tmux)
    start_with_conversation(captain)
    assert captain.launched().pool == 'fake'
    conversation = captain.record()
    printed = []

    def reading() -> None:
        printed.extend(watch.render_table(watch.status_reading(ship, tmux)))
        printed.extend(check.detail for check in (doctor.pool_check(ship.pools['paid']),))
        printed.extend(watch.render_full(watch.seat_reading(seatmod.read_seat(ship, ship.seat('captain'), tmux))))

    printed.append(captain.failover_to('paid'))
    assert captain.pool.name == 'paid' and captain.launched().pool == 'paid'
    assert captain.launched().resume, 'the move keeps its conversation'
    assert captain.record() == conversation
    holding_key(captain.pane().pid)
    no_command_line_holds_it(captain.pane().pid)
    reading()
    printed.append(captain.restart())
    assert captain.launched().pool == 'paid' and captain.launched().resume
    holding_key(captain.pane().pid)
    reading()
    printed.append(captain.failover_to('fake'))  # off the key, its conversation kept
    assert captain.pool.name == 'fake' and captain.launched().pool == 'fake' and captain.launched().resume
    assert captain.record() == conversation
    assert KEY.encode() not in raw(captain.pane().pid, 'environ'), 'off the key, the program holds none'
    reading()
    printed.append(captain.failover_to('paid'))  # and back onto it
    assert captain.launched().pool == 'paid' and captain.launched().resume
    holding_key(captain.pane().pid)
    printed.append(captain.fresh())
    assert captain.launched().pool == 'paid' and not captain.launched().resume
    holding_key(captain.pane().pid)
    reading()
    captain.down()
    nothing_written_holds_it(ship, *printed)


def test_status_says_the_key_is_present_and_names_the_file(ship: shipmod.Ship, tmux: Tmux, key_file):
    rows = watch.render_table(watch.status_reading(ship, tmux))
    assert f'pool paid: key present in {key_file.path}' in rows


def test_a_seat_whose_key_is_missing_stays_down_and_the_rest_of_the_ship_comes_up(ship: shipmod.Ship, tmux: Tmux, key_file):
    key_file.write(other=KEY)
    proofs = seatmod.bring_up(ship, tmux)[2:]
    assert [p.split(' ')[0] for p in proofs] == ['captain', 'desk', 'worker-1:']
    assert proofs[2] == f'worker-1: its pool paid: key paid is not in {key_file.path}; nothing was started'
    assert not (ship.runtime / 'seats' / 'worker-1' / 'home').exists(), 'nothing of the launch was made'


@pytest.mark.parametrize('door', ['up', 'fresh', 'restart'])
def test_each_door_refuses_a_seat_whose_key_is_missing(ship: shipmod.Ship, tmux: Tmux, key_file, door):
    key_file.path.unlink()
    worker = seatmod.Seat(ship, ship.seat('worker-1'), tmux)
    with pytest.raises(KeyRefused, match=f'^worker-1: its pool paid: no key file at {key_file.path}; nothing was started$'):
        getattr(worker, door)()
    assert tmux.sessions() == [] and worker.launched() is None


def test_a_program_that_dies_printing_its_key_is_not_quoted(ship: shipmod.Ship, tmux: Tmux, tmp_path, monkeypatch):
    program = tmp_path / 'prints-its-key'
    program.write_text(f'#!/bin/sh\nprintf "bad key %s ending %s\\n" "${keyfile.HELPER_VARIABLE}" "{TAIL}"\nexit 3\n')
    program.chmod(0o755)
    real = FakeHarness.launch

    def launch(self, *args, **options):
        made = real(self, *args, **options)
        return Launch((str(program),), made.env)
    monkeypatch.setattr(FakeHarness, 'launch', launch)
    worker = seatmod.Seat(ship, ship.seat('worker-1'), tmux)
    with pytest.raises(ShipError) as refused:
        worker.up()
    line = str(refused.value)
    assert 'not quoted on a key pool' in line and KEY not in line and TAIL not in line
    nothing_written_holds_it(ship, line)


def namespaces_allowed() -> bool:
    try:
        return subprocess.run(['bwrap', '--ro-bind', '/', '/', 'true'], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def test_a_jailed_seat_gets_its_key_through_a_pipe_and_on_no_command_line(ship: shipmod.Ship, tmux: Tmux, typed):
    if not namespaces_allowed():
        pytest.skip('bubblewrap cannot make a namespace here')
    file = ship.file
    work = ship.folder.parent.parent / 'jail-work'  # a jailed seat works outside the fleet's folder
    work.mkdir()
    text = shipmod.edit(file.read_text(), 'seats.worker-1', 'sandbox', True)
    file.write_text(shipmod.edit(text, 'seats.worker-1', 'cwd', str(work)))
    jailed = shipmod.load(ship.folder)
    worker = seatmod.Seat(jailed, jailed.seat('worker-1'), tmux)
    assert 'jailed' in worker.up()
    pane = worker.pane()
    holding_key(pane.pid)
    no_command_line_holds_it(pane.pid)
    assert not any(KEY in word for words in typed for word in words)
    from quarterdeck import sandbox
    assert sandbox.seats_hidden(worker.launched().argv) is True, 'a jail behind the key launcher is still read as one'
    worker.down()
    nothing_written_holds_it(jailed)
