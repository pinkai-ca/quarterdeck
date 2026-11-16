"""The drydock with a pool paid by a key: every door that starts a seat on it refuses, and the rest of the ship runs.

A throwaway pool on the fake harness's own provider, with a key variable that is never set: the refusal comes before
any key is read (ship.key_refusal)."""

from __future__ import annotations

from pathlib import Path

import pytest

from quarterdeck import cli, seat as seatmod, ship as shipmod
from quarterdeck.ship import KeyRefused, SeatSpec
from quarterdeck.tmux import Tmux

NEVER_SET = 'QD_SYNTHETIC_NEVER_SET_KEY'
LINE = 'is paid by a key, and no seat runs on a key until the routed key isolation lands; nothing was started'


@pytest.fixture
def ship(drydock: Path, monkeypatch) -> shipmod.Ship:
    monkeypatch.delenv(NEVER_SET, raising=False)
    file = drydock / 'ship.toml'
    text = file.read_text() + f'\n[pools.paid]\nprovider = "synthetic"\napi_key = "env:{NEVER_SET}"\n'
    text = shipmod.edit(text, 'seats.worker-1', 'pool', 'paid')  # declared on a key alone
    text = shipmod.edit(text, 'seats.captain', 'pool', ['fake', 'paid'])  # a subscription first, a key as a choice
    file.write_text(text)
    return shipmod.load(drydock)  # loading never refuses: a running keyed seat can still be seen and stopped


def test_the_whole_ship_comes_up_but_the_seat_on_a_key(ship: shipmod.Ship, tmux: Tmux):
    proofs = seatmod.bring_up(ship, tmux)[2:]
    assert [p.split(' ')[0] for p in proofs] == ['captain', 'desk', 'worker-1:']
    assert ' · pool fake · fresh' in proofs[0] and ' · pool fake · fresh' in proofs[1]
    assert proofs[2] == f'worker-1: its pool paid {LINE}'
    assert 'Crew' not in tmux.sessions() and not (ship.runtime / 'trusted' / 'worker-1' / 'launch.json').exists()
    assert not (ship.runtime / 'seats' / 'worker-1' / 'home').exists(), 'nothing of the launch was made'


@pytest.mark.parametrize('door', ['up', 'fresh', 'restart'])
def test_each_door_refuses_a_seat_declared_on_a_key(ship: shipmod.Ship, tmux: Tmux, door):
    worker = seatmod.Seat(ship, ship.seat('worker-1'), tmux)
    with pytest.raises(KeyRefused, match=f'^worker-1: its pool paid {LINE}$'):
        getattr(worker, door)()
    assert tmux.sessions() == [] and worker.launched() is None


def test_qd_up_names_the_refusal(ship: shipmod.Ship, tmux: Tmux, monkeypatch, capsys):
    monkeypatch.setattr(cli.Tmux, 'for_ship', classmethod(lambda cls, s: tmux))
    monkeypatch.setattr(cli.service, 'start', lambda s: 'service not started by this test')
    code = cli.main(['--ship', str(ship.folder), 'up', 'worker-1'])
    out, err = capsys.readouterr()
    assert code != 0 and f'worker-1: its pool paid {LINE}' in out + err
    assert tmux.sessions() == []


def test_a_running_seat_is_never_moved_onto_its_key_choice(ship: shipmod.Ship, tmux: Tmux):
    captain = seatmod.Seat(ship, ship.seat('captain'), tmux)
    assert ' · pool fake · ' in captain.up()
    pane = captain.pane()
    with pytest.raises(KeyRefused, match=f'^captain: its pool paid {LINE}$'):
        captain.failover_to('paid')
    assert captain.pane() == pane and captain.pool.name == 'fake' and captain.launched().pool == 'fake'
    captain.down()
    with pytest.raises(KeyRefused, match=f'^captain: its pool paid {LINE}$'):
        cli.choose_for_launch(captain, 'paid')  # qd set pool=paid on a down seat
    with pytest.raises(KeyRefused, match=f'^captain: its pool paid {LINE}$'):
        captain.up(pool='paid')  # qd up captain --pool paid
    assert captain.pane() is None and captain.pool.name == 'fake'


def test_a_throwaway_seat_on_a_key_is_refused_inside_its_own_session(ship: shipmod.Ship, tmux: Tmux):
    """qd check's and qd doctor's own seats launch through temporary_seat and Seat.up, as this one does."""
    spec = SeatSpec('check-fake-0000', 'qd-check-fake-0000', 'worker', 'fake', ('paid',), cwd=ship.workspace)
    with seatmod.temporary_seat(ship, tmux, spec, [], 'a key refusal test') as seat:
        with pytest.raises(KeyRefused, match=f'^check-fake-0000: its pool paid {LINE}$'):
            seat.up(resume=False)
        assert [pane.window for pane in tmux.panes('qd-check-fake-0000')] == ['qd'], 'only the placeholder'
    assert 'qd-check-fake-0000' not in tmux.sessions()
