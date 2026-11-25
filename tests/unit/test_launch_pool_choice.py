"""At launch, the ship file is the Operator's order: a down seat takes its first declared choice with room.

A saved choice later in the list than a choice with room gives way, and so does one the ship file no longer
declares; a person's explicit choice for the next launch, by ``qd set pool=`` on a down seat or ``qd up
--pool``, is taken as it is; with no choice that has room the saved choice stays.
"""

import json
from types import SimpleNamespace

import pytest

from quarterdeck import cli, install, ledger, seat as seatmod
from quarterdeck.seat import EXPLICIT, Seat, SelectionError, read_seat
from quarterdeck.ship import ShipError
from .test_own_copy_modes import fact, inert_copy, state, vessel  # noqa: F401 (fixtures)
from .test_saved_pool_selection import migrated  # noqa: F401 (fixture)
from .test_cross_program_failover import running, usage  # noqa: F401 (fixture)


def down_on(r, pool):
    """The worker down, its saved choice ``pool``, as failover or an older ship file left it."""
    r.worker.down()
    assert r.worker.launched() is None
    (r.worker.trusted / 'pool').write_text(pool + '\n')
    return Seat(r.ship, r.ship.seat('worker'), r.tmux, launching=True)


def walls(monkeypatch, *walled):
    """The ledger shows these pools walled, and room known on every other: the reading the service routes by."""
    class Book:
        quota = SimpleNamespace(room_known=lambda name, *, scope=None: True)

        def walled(self, name, ship=None, *, scope=None):
            return name in walled

        def usage_known(self, name, *, scope=None):
            return True

        def catching_up(self):
            return False

        def close(self):
            pass

    monkeypatch.setattr(ledger.Ledger, 'reading', classmethod(lambda cls, path: Book()))


def test_a_saved_later_choice_gives_way_to_the_first_with_room_and_starts_fresh_on_another_program(migrated,
                                                                                                    monkeypatch):
    r = migrated
    walls(monkeypatch)  # room known on both
    seat = down_on(r, 'second')
    assert seat.pool.name == 'second'
    proof = seat.up()
    assert proof.startswith('worker: launched on first (the first declared choice with room, before its saved '
                            'second); its saved choice was second')
    after = Seat(r.ship, r.ship.seat('worker'), r.tmux)
    assert (after.trusted / 'pool').read_text() == 'first\n' and after.pool.name == 'first'
    assert after.launched().pool == 'first' and after.launched().harness == 'codex'
    assert r.launches[-1][:2] == ('codex', 'first')


def test_the_saved_choice_stays_when_every_earlier_choice_is_walled(migrated, monkeypatch):
    r = migrated
    walls(monkeypatch, 'first')
    seat = down_on(r, 'second')
    assert seat.launch_choice() == ('second', 'its saved choice')
    proof = seat.up(resume=False)
    assert proof.startswith('worker up in'), 'a launch on its saved choice is as it always was'
    after = Seat(r.ship, r.ship.seat('worker'), r.tmux)
    assert after.pool.name == 'second' and after.launched().harness == 'claude'


def test_with_every_choice_walled_the_saved_choice_stays(migrated, monkeypatch):
    r = migrated
    walls(monkeypatch, 'first', 'second')
    seat = down_on(r, 'second')
    assert seat.launch_choice() == ('second', 'every declared choice is walled; its saved choice stays')


def test_a_saved_choice_the_ship_file_no_longer_declares_gives_way_only_at_a_launch(migrated):
    r = migrated
    r.worker.down()
    (r.worker.trusted / 'pool').write_text('withdrawn\n')
    with pytest.raises(SelectionError, match="saved pool 'withdrawn' has no declared choice"):
        Seat(r.ship, r.ship.seat('worker'), r.tmux)
    reading = read_seat(r.ship, r.ship.seat('worker'), r.tmux)
    assert 'selection unknown' in str(reading.error), 'every reading still names it'
    seat = Seat(r.ship, r.ship.seat('worker'), r.tmux, launching=True)
    proof = seat.up()
    assert proof.startswith('worker: launched on first')
    assert Seat(r.ship, r.ship.seat('worker'), r.tmux).pool.name == 'first'


def test_an_undeclared_saved_choice_with_a_launch_on_file_is_still_refused(migrated):
    r = migrated
    (r.worker.trusted / 'pool').write_text('withdrawn\n')
    assert r.worker.launched() is not None
    with pytest.raises(SelectionError, match='has no declared choice'):
        Seat(r.ship, r.ship.seat('worker'), r.tmux, launching=True)


def test_qd_set_on_a_down_seat_chooses_the_next_launch_and_it_is_taken_once(migrated, monkeypatch):
    r = migrated
    walls(monkeypatch)  # room known on both
    r.worker.down()
    seat = Seat(r.ship, r.ship.seat('worker'), r.tmux, launching=True)
    answer = cli.select(r.ship, seat, ['pool=second'])
    assert answer == 'worker: pool second chosen for its next launch (it is down); qd up worker launches it there'
    assert (seat.trusted / 'pool').read_text() == 'second\n' and (seat.trusted / EXPLICIT).read_text() == 'second\n'
    seat = Seat(r.ship, r.ship.seat('worker'), r.tmux, launching=True)
    proof = seat.up()
    assert proof.startswith('worker: launched on second (chosen for this launch by qd set)')
    assert not (seat.trusted / EXPLICIT).exists(), 'the choice is taken by one launch'
    assert Seat(r.ship, r.ship.seat('worker'), r.tmux).launched().pool == 'second'
    r.live.clear()
    again = Seat(r.ship, r.ship.seat('worker'), r.tmux, launching=True)
    again.down()
    proof = Seat(r.ship, r.ship.seat('worker'), r.tmux, launching=True).up()
    assert proof.startswith('worker: launched on first'), 'the next launch reads the ship file again'


def test_a_refused_launch_keeps_the_qd_set_choice_and_the_next_up_takes_it(migrated, monkeypatch):
    """A person's choice is used up only by a launch that starts: one refused at a door of _up leaves it as set."""
    from quarterdeck import write_bound
    r = migrated
    walls(monkeypatch)  # room known on both: without the choice the next up would take first
    r.worker.down()
    seat = Seat(r.ship, r.ship.seat('worker'), r.tmux, launching=True)
    cli.select(r.ship, seat, ['pool=second'])
    real = write_bound.require_clear

    def stopped_at_its_bound(seat):
        raise ShipError(f'{seat.name}: stopped at its write bound')
    monkeypatch.setattr(write_bound, 'require_clear', stopped_at_its_bound)
    seat = Seat(r.ship, r.ship.seat('worker'), r.tmux, launching=True)
    launches = len(r.launches)
    with pytest.raises(ShipError, match='stopped at its write bound'):
        seat.up()
    assert seat.launched() is None and len(r.launches) == launches, 'refused before anything was started'
    assert (seat.trusted / EXPLICIT).read_text() == 'second\n', 'a refused launch keeps the choice as the person set it'
    assert (seat.trusted / 'pool').read_text() == 'second\n'
    monkeypatch.setattr(write_bound, 'require_clear', real)
    seat = Seat(r.ship, r.ship.seat('worker'), r.tmux, launching=True)
    assert seat.up().startswith('worker: launched on second (chosen for this launch by qd set)')
    assert not (seat.trusted / EXPLICIT).exists(), 'the launch that started takes the choice'
    assert Seat(r.ship, r.ship.seat('worker'), r.tmux).launched().pool == 'second'


def test_qd_set_never_writes_a_choice_while_a_launch_is_on_file(migrated):
    r = migrated
    r.live.clear()  # the pane is gone, its launch still on file
    seat = Seat(r.ship, r.ship.seat('worker'), r.tmux)
    assert not seat.is_up and seat.launched() is not None
    with pytest.raises(ShipError, match='its last launch is still on file'):
        cli.select(r.ship, seat, ['pool=second'])
    assert not (seat.trusted / EXPLICIT).exists() and not (seat.trusted / 'pool').exists()


def test_qd_set_on_an_up_seat_still_asks_the_service(migrated, monkeypatch):
    r = migrated
    asked = []
    monkeypatch.setattr(cli.service, 'request', lambda runtime, kind, **data: asked.append((kind, data)) or 'asked')
    assert cli.select(r.ship, r.worker, ['pool=second']) == 'asked'
    from quarterdeck.request_queue import launch_binding
    assert asked == [('select', {'seat': 'worker', 'pool': 'second', 'origin': launch_binding(r.worker)})]
    assert not (r.worker.trusted / EXPLICIT).exists()


def test_qd_up_pool_takes_a_declared_choice_for_one_launch_and_refuses_another(migrated):
    r = migrated
    seat = down_on(r, 'first')
    with pytest.raises(ShipError, match="--pool 'elsewhere' is not one of its declared choices"):
        seat.up(pool='elsewhere')
    assert seat.launched() is None
    proof = seat.up(pool='second')
    assert proof.startswith('worker: launched on second (chosen for this launch by qd up --pool); its saved '
                            'choice was first')


def test_qd_up_pool_names_a_seat(migrated):
    r = migrated
    with pytest.raises(ShipError, match='--pool names one launch: give the seat'):
        cli.cmd_up(SimpleNamespace(ship=str(r.ship.folder), seat=None, pool='second', fresh=False))


def test_a_seat_that_is_up_is_never_moved_by_its_launch_choice(migrated):
    r = migrated
    assert r.worker.is_up
    with pytest.raises(ShipError, match='is up in %1'):
        r.worker.up()
    with pytest.raises(ShipError, match='is up in %1'):
        r.worker.up(pool='second')
    assert not (r.worker.trusted / 'pool').exists() and r.worker.launched().pool == 'first'


def test_bring_up_launches_down_seats_by_the_ship_file(migrated, monkeypatch):
    r = migrated
    walls(monkeypatch)  # room known on both
    down_on(r, 'second')
    monkeypatch.setattr(seatmod.shipmod, 'fence', lambda ship: 'fenced')
    proofs = seatmod.bring_up(r.ship, r.tmux)
    assert any(line.startswith('worker: launched on first') for line in proofs), proofs


def test_the_delegated_up_carries_the_pool_and_never_drops_it(vessel, monkeypatch):  # noqa: F811
    inert_copy(vessel.runtime / 'venv', 'installed')
    state(vessel, fact())
    monkeypatch.setattr(install, 'running_from', lambda path: True)
    calls = []
    monkeypatch.setattr(install.subprocess, 'run', lambda argv, **kwargs: calls.append(argv) or SimpleNamespace(
        returncode=0, stdout=json.dumps(['synthetic launched'])))
    assert install.delegate_up(vessel, seat='synthetic-worker', fresh=False, reload_required=True,
                               pool='synthetic-choice') == ['synthetic launched']
    assert calls[0][-3:] == ['synthetic-worker', '--pool', 'synthetic-choice']
    install.delegate_up(vessel, seat='synthetic-worker', fresh=True, reload_required=True)
    assert '--pool' not in calls[1]


def test_qd_set_on_a_down_seat_through_the_cli_keeps_the_ship_file(migrated, capsys, monkeypatch):
    r = migrated
    r.worker.down()
    r.ship.file.write_text(r.ship.file.read_text().replace('[ship]', '[ship]\ncode="checkout"', 1))
    before = r.ship.file.read_bytes()
    monkeypatch.setattr(cli.service, 'request', lambda *a, **k: pytest.fail('a down seat needs no service request'))
    assert cli.main(['--ship', str(r.ship.folder), 'set', 'worker', 'pool=second']) == 0
    out, err = capsys.readouterr()
    assert not err and 'pool second chosen for its next launch' in out
    assert r.ship.file.read_bytes() == before
    seat = Seat(r.ship, r.ship.seat('worker'), r.tmux, launching=True)
    assert seat.up().startswith('worker: launched on second (chosen for this launch by qd set)')


def test_a_launch_after_the_pane_exits_reseeds_its_old_pool_before_selection(running):
    r = running
    r.svc.account()
    r.svc.failover()
    assert r.seat.pool.name == 'second'
    r.seat.tmux.kill_window(r.seat.launched().pane)
    usage(r.svc, 'first', 0.2)
    r.reseeds.clear()
    proof = r.seat.up()
    assert proof.startswith('worker: launched on first')
    assert r.reseeds and all(pool == launched for pool, harness, launched in r.reseeds)
    assert r.reseeds[0] == ('second', 'claude', 'second')
    assert r.seat.launched().pool == 'first' and r.seat.launched().harness == 'codex'


def walled_now(monkeypatch, *walled):
    """The ledger shows these pools walled for the running seat."""
    from quarterdeck import quota
    monkeypatch.setattr(quota, 'current_walled', lambda seat, book, **kwargs: seat.pool.name in walled)
    walls(monkeypatch, *walled)


@pytest.mark.parametrize('how', ['restart', 'fresh'])
def test_a_relaunch_off_a_walled_pool_takes_the_first_declared_choice_with_room(migrated, monkeypatch, how):
    r = migrated
    walled_now(monkeypatch, 'first')
    assert r.worker.is_up and r.worker.launched().pool == 'first'
    proof = getattr(r.worker, how)()
    assert 'worker: relaunched on second, not on its walled first' in proof
    after = Seat(r.ship, r.ship.seat('worker'), r.tmux)
    assert after.pool.name == 'second' and after.launched().pool == 'second'


def test_a_relaunch_on_a_pool_with_room_stays_where_it_is(migrated, monkeypatch):
    r = migrated
    walled_now(monkeypatch)  # nothing walled
    proof = r.worker.restart()
    assert 'relaunched on' not in proof
    assert Seat(r.ship, r.ship.seat('worker'), r.tmux).launched().pool == 'first'


def test_a_relaunch_with_every_choice_walled_stays_where_it_is(migrated, monkeypatch):
    r = migrated
    walled_now(monkeypatch, 'first', 'second')
    proof = r.worker.restart()
    assert 'relaunched on' not in proof
    assert Seat(r.ship, r.ship.seat('worker'), r.tmux).launched().pool == 'first'


@pytest.mark.parametrize('walled', [('first',), ()], ids=['first-spent', 'first-open'])
@pytest.mark.parametrize('switching', [False, True], ids=['order', 'with-harness'])
def test_a_dropped_running_pool_names_the_launch_choice_not_always_the_first(tmp_path, monkeypatch, walled, switching):
    from quarterdeck.ship import load

    def declaration(_, pools):
        (tmp_path / 'ship.toml').write_text(
            '[ship]\nname="synthetic"\n' + ''.join(f'[pools.{name}]\nprovider="openai"\n'
                                                   for name in ('first', 'second', 'third'))
            + '[sessions.crew]\n[seats.worker]\nsession="crew"\nharness="codex"\nmodel="synthetic-source"\n'
            + f'pool={pools}\n')
        return load(tmp_path)

    ship = declaration(tmp_path, '["first", "second", "third"]')
    running = Seat(ship, ship.seat('worker'), SimpleNamespace(pane=lambda ident: None))
    running.trusted.mkdir(parents=True, exist_ok=True)
    seatmod.Launched('%1', 101, '2026-11-24T00:00:00+00:00', 'third', None, ['codex'], 'synthetic',
                     'codex').write(running.trusted / 'launch.json')
    declaration(tmp_path, '["first", "second"]')  # the edit the answer describes: third is dropped
    walls(monkeypatch, *walled)
    seat = SimpleNamespace(name='worker', is_up=True, pool=SimpleNamespace(name='third'))
    said = cli.pool_order_guidance(ship, seat, 'first,second', switching=switching)
    choice = 'second (' if walled else 'first ('  # Seat.launch_choice: first spent, second has room
    if switching:  # qd fresh decides its own pool: no prediction is appended
        assert said == '  pool written; qd fresh worker applies it with the harness'
    else:
        assert said.startswith('  pool order written; third is no longer declared: worker keeps running there, '
                               f'and its next launch takes {choice}')
