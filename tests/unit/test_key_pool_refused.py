"""A seat on a pool paid by a key is refused at every door until the routed key isolation lands (ship.key_refusal).

Throwaway pools and a key variable that is never set: every refusal here comes before any key is read, so no test
needs a key, real or fake. A subscription and a keyless endpoint are unchanged."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from quarterdeck import check, compaction, daily_live, doctor, harness, quota, ship as shipmod
from quarterdeck.ship import KeyRefused, Pool
from .test_compaction_protocol import ready, world  # noqa: F401
from .test_cross_program_failover import running, usage  # noqa: F401
from .test_status_holds import read, view  # noqa: F401

NEVER_SET = 'QD_SYNTHETIC_NEVER_SET_KEY'
UNSET = f'env:{NEVER_SET}'
LINE = 'is paid by a key, and no seat runs on a key until the routed key isolation lands; nothing was started'

KEYED = [Pool('paid', 'anthropic', api_key=UNSET),
         Pool('paid', 'local', endpoint='https://example.invalid/v1', api_key=UNSET)]
FREE = [Pool('free', 'anthropic'), Pool('free', 'anthropic', config_dir='/nowhere'),
        Pool('free', 'local', endpoint='http://127.0.0.1:9/v1')]


@pytest.fixture(autouse=True)
def no_key(monkeypatch):
    monkeypatch.delenv(NEVER_SET, raising=False)


def keyed(r, name):
    """Make one of the running fixture's pools a key pool, of the same provider."""
    r.seat.ship.pools[name] = Pool(name, r.seat.ship.pools[name].declared_provider, api_key=UNSET)


@pytest.mark.parametrize('pool', KEYED, ids=['key', 'keyed-endpoint'])
def test_a_pool_paid_by_a_key_names_the_reason_and_what_lifts_it(pool):
    assert pool.paid_by_key and shipmod.key_refusal(pool) == f'its pool paid {LINE}'
    found = doctor.pool_check(pool)  # the validation reading: not ready, whatever its key, and nothing probed
    assert not found.passed and found.detail == f'its pool paid {LINE}'.removesuffix('; nothing was started')


@pytest.mark.parametrize('pool', FREE, ids=['subscription', 'subscription-home', 'keyless-endpoint'])
def test_a_subscription_and_a_keyless_endpoint_are_unchanged(pool):
    assert not pool.paid_by_key and shipmod.key_refusal(pool) is None


# -- the service's moves and a launch's choice ----------------------------------------------------------------------

def test_the_service_moves_past_a_key_pool_to_the_next_choice_with_room(running):
    r = running
    keyed(r, 'second')
    assert quota.candidate_pools(r.seat) == ('third',)
    r.svc.failover()
    assert r.seat.pool.name == 'third' and len(r.launches) == 1


def test_with_only_a_key_pool_left_the_seat_is_held_at_its_limit_and_the_line_says_so(view):  # law: nothing-waits-silently
    r = view
    keyed(r, 'second')
    usage(r.svc, 'third', 1.0)
    r.svc.failover()
    assert r.seat.pool.name == 'first' and r.launches == []
    facts = read(r)
    assert facts.held == ['worker'] and 'worker held at limit' in facts.line()


def test_a_down_seat_never_chooses_a_key_pool_for_its_launch(running):
    r = running
    keyed(r, 'second')
    assert r.seat.launch_choice()[0] == 'third'


def test_a_seat_saved_on_a_key_pool_launches_on_its_next_choice(running):
    r = running
    keyed(r, 'first')  # its saved choice, from before the refusal, is now paid by a key
    assert r.seat.launch_choice()[0] == 'second'
    usage(r.svc, 'first', 0.0)  # even where the key pool is shown with room
    assert r.seat.launch_choice()[0] == 'second'


def test_an_explicit_move_to_a_key_pool_is_refused_and_the_seat_runs_on(running):
    r = running
    keyed(r, 'second')
    before = r.seat.launched()
    with pytest.raises(KeyRefused, match=f'^worker: its pool second {LINE}$'):
        r.svc.select(r.seat, 'second')  # qd set pool= on a running seat
    with pytest.raises(KeyRefused, match=f'^worker: its pool second {LINE}$'):
        r.seat.failover_to('second')
    assert r.seat.pool.name == 'first' and r.seat.launched() == before and r.launches == [] and r.seat.pane()


@pytest.mark.parametrize('door', ['restart', 'fresh'])
def test_a_relaunch_on_a_key_pool_is_refused_before_the_seat_is_stopped(running, door):
    """qd restart and qd fresh, and the service's relaunch, which calls the same two."""
    r = running
    keyed(r, 'first')
    before = r.seat.launched()
    with pytest.raises(KeyRefused, match=f'^worker: its pool first {LINE}$'):
        getattr(r.seat, door)()
    assert r.seat.launched() == before and r.seat.pane() is not None and r.launches == []


def test_a_fresh_start_after_compactions_on_a_key_pool_leaves_the_seat_running(world):
    w = world
    ready(w)
    name = w.seat.pool.name
    w.seat.ship.pools[name] = Pool(name, w.seat.ship.pools[name].declared_provider, api_key=UNSET)
    w.source[0].result()
    w.source[0].finish()  # the turn ends at a natural break: the moment compaction would stop and start it
    compaction.tick(w.seat)
    assert w.calls == []  # neither down nor up: refused before the stop
    assert compaction.load(w.svc.runtime, w.seat.name)['reason'] == f'worker: its pool {name} {LINE}'


# -- qd check, the nightly live check and qd doctor's own seat -------------------------------------------------------

def test_qd_check_takes_no_key_pool_and_says_why_when_only_a_key_pays():
    fake = harness.get('fake')
    paid, free = Pool('paid', 'synthetic', api_key=UNSET), Pool('free', 'synthetic')
    assert check.check_pool(SimpleNamespace(pools={'paid': paid, 'free': free}), fake) == (free, None)
    only = SimpleNamespace(pools={'paid': paid})
    refused = f'check-fake: its pool paid {LINE}'
    assert check.check_pool(only, fake) == (None, refused)
    checks, version = check._check(only, 'fake', None, fake)  # returns before any seat, session or home is made
    assert version is None and [(c.name, c.passed, c.detail) for c in checks] == [('pool', False, refused)]
    assert check.check_pool(SimpleNamespace(pools={}), fake) == (None, None)
    now = datetime(2026, 11, 16, tzinfo=timezone.utc)
    assert daily_live.wall_reason(only, 'fake', now) == f'{refused}; try again the next night'


def test_qd_doctor_opens_its_seat_on_no_key_pool():
    found = doctor.fix(SimpleNamespace(pools={'paid': Pool('paid', 'synthetic', api_key=UNSET)}), [], None)
    assert [(c.name, c.passed) for c in found] == [('seat opened', False)]


def test_qd_doctor_names_the_key_refusal_when_only_key_pools_could_carry_its_seat():
    """A ship whose only pools a program runs on are paid by a key is told the key refusal, the true cause, never
    that no program is on the PATH."""
    found = doctor.fix(SimpleNamespace(pools={'paid': Pool('paid', 'synthetic', api_key=UNSET)}), [], None)
    assert [(c.name, c.passed, c.detail) for c in found] == [('seat opened', False, f'doctor: its pool paid {LINE}')]
    assert 'install one first' not in found[0].detail
    nothing = doctor.fix(SimpleNamespace(pools={}), [], None)  # no pool at all: the program's absence, as before
    assert [(c.name, c.passed, c.detail) for c in nothing] == [(
        'seat opened', False, "no declared pool's program is on the PATH, so no seat can be asked; install one first")]


# -- the plan: qd setup's proposal, read before anything is written --------------------------------------------------

@pytest.mark.parametrize('pool,shown', [
    (Pool('fake', 'synthetic', api_key=UNSET), f'an API key ({NEVER_SET}); refused for now: no seat runs on a key '
                                               'until the routed key isolation lands'),
    (Pool('fake', 'local', endpoint='https://example.invalid/v1', api_key=UNSET),
     f'an API key ({NEVER_SET}) at https://example.invalid/v1; refused for now: no seat runs on a key until the '
     'routed key isolation lands'),
    (Pool('fake', 'local', endpoint='http://127.0.0.1:9/v1'), 'a local model at http://127.0.0.1:9/v1'),
    (Pool('fake', 'synthetic'), 'a subscription you are signed in to'),
], ids=['key', 'keyed-endpoint', 'keyless-endpoint', 'subscription'])
def test_the_setup_proposal_names_a_key_pool_refused_before_it_is_written(drydock, pool, shown):
    from quarterdeck import setup_proposal
    ship = shipmod.load(drydock)
    ship.pools['fake'] = pool
    assert setup_proposal.summary(ship, setup_proposal.display_control)['runs_on'] == {'fake': shown}
