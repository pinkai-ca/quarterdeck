"""A pool paid by a key is declared and chosen like any other. What holds a seat back is its key being missing or
refused: that one seat, at that door, in a line that names the key file. Synthetic keys only."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from quarterdeck import check, compaction, daily_live, doctor, harness, keyfile, quota
from quarterdeck.ship import KeyRefused, Pool
from .test_compaction_protocol import ready, world  # noqa: F401
from .test_cross_program_failover import running, usage  # noqa: F401
from .test_status_holds import read, view  # noqa: F401

KEY = 'sk-synthetic-Q7wM2xC9zT4lP8rV3nH6'
TAIL = KEY[-8:]


def keyed(r, name, reference='keys:paid'):
    """Make one of the running fixture's pools a key pool, of the same provider, in this ship."""
    ship = r.seat.ship
    ship.pools[name] = Pool(name, ship.pools[name].declared_provider, api_key=reference, ship_folder=ship.folder)


# -- the service's moves and a launch's choice ----------------------------------------------------------------------

def test_a_key_pool_is_a_destination_like_any_other(running, key_file):
    r = running
    key_file.write(paid=KEY)
    keyed(r, 'second')
    assert quota.candidate_pools(r.seat) == ('second', 'third')
    r.svc.failover()
    assert r.seat.pool.name == 'second' and len(r.launches) == 1


def test_the_service_moves_past_a_pool_whose_key_is_missing(running, key_file):
    r = running
    key_file.write(other=KEY)
    keyed(r, 'second')
    assert quota.candidate_pools(r.seat) == ('third',)
    r.svc.failover()
    assert r.seat.pool.name == 'third' and len(r.launches) == 1


def test_a_seat_moves_off_a_key_pool_past_its_budget(running, key_file, monkeypatch):
    r = running
    key_file.write(paid=KEY)
    ship = r.seat.ship
    ship.pools['first'] = Pool('first', ship.pools['first'].declared_provider, api_key='keys:paid',
                               budget='1 USD/month', ship_folder=ship.folder)
    assert 'first budget' not in r.svc.ledger.walls(ship)
    monkeypatch.setattr(r.svc.ledger, 'spend', lambda pool, since: (2.0, 0) if pool == 'first' else (0.0, 0))
    assert 'first budget' in r.svc.ledger.walls(ship)
    r.svc.failover()
    assert r.seat.pool.name == 'second' and len(r.launches) == 1


def test_with_only_an_unreadable_key_left_the_seat_is_held_and_the_line_says_so(view, key_file):
    r = view  # law: nothing-waits-silently
    key_file.write(other=KEY)
    keyed(r, 'second')
    usage(r.svc, 'third', 1.0)
    r.svc.failover()
    assert r.seat.pool.name == 'first' and r.launches == []
    facts = read(r)
    assert facts.held == ['worker'] and 'worker held at limit' in facts.line()


def test_a_down_seat_chooses_a_key_pool_whose_key_is_there(running, key_file):
    r = running
    key_file.write(paid=KEY)
    keyed(r, 'second')  # the fixture's first choice is spent
    assert r.seat.launch_choice()[0] == 'second'


def test_a_seat_saved_on_a_pool_whose_key_is_gone_launches_on_its_next_choice(running, key_file):
    r = running
    key_file.write(other=KEY)
    keyed(r, 'first')
    assert r.seat.launch_choice()[0] == 'second'
    usage(r.svc, 'first', 0.0)  # even where the key pool is shown with room
    assert r.seat.launch_choice()[0] == 'second'


def test_an_explicit_move_to_a_pool_whose_key_is_missing_is_refused_and_the_seat_runs_on(running, key_file):
    r = running
    key_file.write(other=KEY)
    keyed(r, 'second')
    before = r.seat.launched()
    line = f'^worker: its pool second: key paid is not in {key_file.path}; nothing was started$'
    with pytest.raises(KeyRefused, match=line):
        r.svc.select(r.seat, 'second')  # qd set pool= on a running seat
    with pytest.raises(KeyRefused, match=line):
        r.seat.failover_to('second')
    assert r.seat.pool.name == 'first' and r.seat.launched() == before and r.launches == [] and r.seat.pane()


@pytest.mark.parametrize('door', ['restart', 'fresh'])
def test_a_relaunch_without_its_key_is_refused_before_the_seat_is_stopped(running, key_file, door):
    r = running
    keyed(r, 'first')
    before = r.seat.launched()
    with pytest.raises(KeyRefused, match=f'^worker: its pool first: no key file at {key_file.path}; nothing was started$'):
        getattr(r.seat, door)()
    assert r.seat.launched() == before and r.seat.pane() is not None and r.launches == []


def test_a_fresh_start_after_compactions_without_its_key_leaves_the_seat_running(world, key_file):
    w = world
    ready(w)
    name = w.seat.pool.name
    ship = w.seat.ship
    ship.pools[name] = Pool(name, ship.pools[name].declared_provider, api_key='keys:paid', ship_folder=ship.folder)
    w.source[0].result()
    w.source[0].finish()  # the turn ends at a natural break: the moment compaction would stop and start it
    compaction.tick(w.seat)
    assert w.calls == []  # neither down nor up: refused before the stop
    assert compaction.load(w.svc.runtime, w.seat.name)['reason'] == (
        f'worker: its pool {name}: no key file at {key_file.path}; nothing was started')


# -- qd check, the nightly live check and qd doctor's own seat -------------------------------------------------------

def test_qd_check_takes_a_key_pool_whose_key_is_there_and_says_why_when_it_is_not(key_file, tmp_path):
    fake = harness.get('fake')
    paid = Pool('paid', 'synthetic', api_key='keys:paid', ship_folder=tmp_path)
    free = Pool('free', 'synthetic')
    assert check.check_pool(SimpleNamespace(pools={'paid': paid, 'free': free}), fake) == (free, None), 'no key yet'
    only = SimpleNamespace(pools={'paid': paid})
    refused = f'check-fake: its pool paid: no key file at {key_file.path}; nothing was started'
    assert check.check_pool(only, fake) == (None, refused)
    checks, version = check._check(only, 'fake', None, fake)  # returns before any seat, session or home is made
    assert version is None and [(c.name, c.passed, c.detail) for c in checks] == [('pool', False, refused)]
    now = datetime(2026, 11, 16, tzinfo=timezone.utc)
    assert daily_live.wall_reason(only, 'fake', now) == f'{refused}; try again the next night'
    key_file.write(paid=KEY)
    assert check.check_pool(only, fake) == (paid, None)
    assert check.check_pool(SimpleNamespace(pools={}), fake) == (None, None)


def test_qd_doctor_names_the_key_when_only_pools_without_one_could_carry_its_seat(key_file, tmp_path):
    paid = Pool('paid', 'synthetic', api_key='keys:paid', ship_folder=tmp_path)
    found = doctor.fix(SimpleNamespace(pools={'paid': paid}), [], None)
    assert [(c.name, c.passed, c.detail) for c in found] == [
        ('seat opened', False, f'doctor: its pool paid: no key file at {key_file.path}; nothing was started')]
    nothing = doctor.fix(SimpleNamespace(pools={}), [], None)  # no pool at all: the program's absence, as before
    assert [(c.name, c.passed, c.detail) for c in nothing] == [(
        'seat opened', False, "no declared pool's program is on the PATH, so no seat can be asked; install one first")]


@pytest.mark.parametrize('program', ['antigravity', 'muse'])
def test_a_program_that_takes_no_key_is_refused_on_a_key_pool(program):
    reason = keyfile.accepts(harness.get(program), Pool('paid', harness.get(program).native_provider, api_key='keys:paid'))
    assert reason and ('takes no key' in reason or 'key and endpoint behavior is unproved' in reason)


# -- what qd status says ---------------------------------------------------------------------------------------------

def test_status_says_present_missing_refused_and_inside_a_jail(key_file, tmp_path):
    pool = Pool('paid', 'anthropic', api_key='keys:paid', ship_folder=tmp_path)
    assert keyfile.status_line(pool) == f'pool paid: key missing: no key file at {key_file.path}'
    assert keyfile.status_line(pool, jailed_caller=True) == 'pool paid: key not visible from inside a jail'
    key_file.write(paid=KEY)
    assert keyfile.status_line(pool) == f'pool paid: key present in {key_file.path}'
    key_file.path.write_text(f'paid = "{KEY}\n')
    line = keyfile.status_line(pool, jailed_caller=True)
    assert line.startswith('pool paid: key refused: ') and KEY not in line
