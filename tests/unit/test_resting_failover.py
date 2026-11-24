"""Exceptional automatic routing with synthetic sign-ins, readers and panes."""

from contextvars import Context
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from quarterdeck import (
    auth_failure, cli, lifecycle, move_hold, pending, post, quota, relaunch_intent, resting_failover, service, shutdown,
    unit_hold, watch, write_bound,
)
from quarterdeck.harness import claude_sign_in
from quarterdeck.harness.claude import ClaudeHarness, LOGIN_QUESTION
from quarterdeck.harness.limits import Usage, Wall, Window
from quarterdeck.harness import resume_target
from quarterdeck.harness.synthetic_record import ClaudeShape
from quarterdeck.harness.types import AUTHENTICATION, Screen, State, TurnFailure
from quarterdeck.seat import Launched, Seat
from quarterdeck.service import Service
from quarterdeck.ship import Pool, SeatSpec, Session, Ship, ShipError, _from_text
from quarterdeck.tmux import Pane
from .resting_attempts import UNSETTLED, save_attempt, settle
from .test_spent_only_walls import refusal, terminal_refusal_rows


def make_sign_in(pool, expiry, *, refresh=True, grant=None):
    source = pool.config_dir / ClaudeHarness.credential_file
    body = {
        "access" + "Token": "invented access",
        "refresh" + "Token": "invented renewal" if refresh else "",
        "expiresAt": expiry,
    }
    if grant is not None:
        body["refresh" + "TokenExpiresAt"] = int(grant.timestamp() * 1000)
    source.write_text(json.dumps({"claudeAiOauth": body}))


@pytest.fixture
def world(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    clock = SimpleNamespace(wall=now, mono=10000.0)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.wall.astimezone(tz) if tz else clock.wall.replace(tzinfo=None)

    monkeypatch.setattr(post, 'now', lambda: clock.wall)
    monkeypatch.setattr(quota, 'datetime', Clock)
    monkeypatch.setattr(quota, 'time', SimpleNamespace(monotonic=lambda: clock.mono))
    names = ('first', 'rest', 'third')
    pools = {name: Pool(name, 'claude', config_dir=tmp_path / name) for name in names}
    for pool in pools.values():
        pool.config_dir.mkdir()
        make_sign_in(pool, (now - timedelta(minutes=1)).timestamp())
    spec = SeatSpec('worker', 'crew', 'worker', 'claude', names, model='claude-sonnet')
    ship = Ship(tmp_path, 'synthetic', 'operator', tmp_path, pools,
                {'crew': Session('crew', 'crew')}, {'worker': spec})
    live = SimpleNamespace(pane=Pane('%1', 'crew', 'worker', 101, 80, 24, False, False, None),
                           screen=Screen('', '', working=False), launches=[], start_error=None, landing=None)
    tmux = SimpleNamespace(pane=lambda ident: live.pane if live.pane and live.pane.id == ident else None,
                           kill_window=lambda ident, **kwargs: setattr(live, 'pane', None),
                           capture=lambda *args, **kwargs: '')
    seat = Seat(ship, spec, tmux)
    seat.home.mkdir(mode=0o700, parents=True)
    seat.trusted.mkdir(parents=True, exist_ok=True)
    record = ClaudeShape(seat.home, tmp_path, 'synthetic-root', spec.model)
    record.begin('synthetic completed input', spec.model, 'none')
    record.end('synthetic completion')
    Launched('%1', 101, (now - timedelta(minutes=2)).isoformat(), 'first', None,
             ['claude'], 'synthetic', 'claude').write(seat.trusted / 'launch.json')
    (seat.trusted / 'record').write_text(str(record.path))
    seat.record()

    def start(argv, env, path):
        live.launches.append(argv)
        if live.start_error:
            raise live.start_error
        live.pane = Pane('%2', 'crew', 'worker', 102, 80, 24, False, False, None)
        if live.landing is not None:
            live.screen = live.landing
        return live.pane

    monkeypatch.setattr(seat, '_open_window', start)
    monkeypatch.setattr(seat, '_survives', lambda *_: None)
    monkeypatch.setattr(service, 'seats', lambda *_: [seat])
    monkeypatch.setattr('quarterdeck.seat.shutil.which', lambda *args, **kwargs: '/usr/bin/sh')
    monkeypatch.setattr(ClaudeHarness, 'version', lambda self: 'synthetic')
    monkeypatch.setattr(ClaudeHarness, 'screen', lambda *a: live.screen)
    monkeypatch.setattr(ClaudeHarness, 'usage', lambda *a: pytest.fail('unexpected provider read'))
    monkeypatch.setattr(unit_hold, '_run', lambda *a: pytest.fail('unexpected systemd read'))
    svc = Service(ship, tmux)
    svc.ledger.finish_catchup(svc.accounting_epoch)
    svc.ledger.db.commit()
    services = [svc]
    w = SimpleNamespace(svc=svc, seat=seat, live=live, record=record, clock=clock, ship=ship)

    def remember(name, *windows, at=None, error=None):
        at = at or now - timedelta(minutes=5)
        w.svc.ledger.record_usage(name, Usage(windows, at, error), at)

    def expired(name='rest'):
        pool = pools[name]
        usage = quota.read_usage(w.svc, pool, ClaudeHarness())
        assert usage is not None and not usage.called
        quota.record_read(w.svc, name, usage, ClaudeHarness.usage_interval, clock.wall)
        return usage

    def restart():
        w.svc.ledger.close()
        services.remove(w.svc)
        w.svc = Service(w.ship, tmux)
        services.append(w.svc)

    def advance(seconds):
        clock.wall += timedelta(seconds=seconds)
        clock.mono += seconds

    def window(used=0.4, *, name='5h', group=None, reset=1800):
        return Window(name, used, now + timedelta(seconds=reset) if reset is not None else None, group=group)

    remember('first', window(1))
    remember('third', window(1))
    w.remember, w.expired, w.restart, w.advance, w.window = remember, expired, restart, advance, window
    try:
        yield w
    finally:
        for running in services:
            running.ledger.close()


def candidate(w, name='rest'):
    return resting_failover.candidate(w.svc, w.seat, name, w.clock.wall)


def test_missing_attempt_database_reads_leave_no_file(world):
    w = world
    choice = resting_failover.Candidate('rest', ('synthetic-account', 'synthetic-generation'))
    assert not resting_failover.attempts(w.ship.runtime)
    assert not resting_failover.spent(w.ship.runtime, choice)
    assert resting_failover.hold_state(w.seat) is resting_failover.Hold.CLEAR
    assert not (w.ship.runtime / resting_failover.FILE).exists()


def test_first_attempt_read_rolls_back_a_killed_writer(world):
    w = world
    runtime = w.ship.runtime
    path = runtime / resting_failover.FILE
    journal = path.with_name(path.name + '-journal')
    chosen = resting_failover.Candidate('rest', ('committed-account', 'committed-generation'))
    abandoned = resting_failover.Candidate('third', ('uncommitted-account', 'uncommitted-generation'))
    claim = resting_failover.claim(w.seat, chosen, w.seat.launched().at)
    assert claim is not None and not journal.exists()
    original = path.read_bytes()
    writer = """
import sqlite3
import sys

db = sqlite3.connect(sys.argv[1])
db.execute('PRAGMA synchronous=FULL')
db.execute('PRAGMA cache_size=1')
db.execute('BEGIN IMMEDIATE')
db.execute('INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?)', (*sys.argv[2:], 'unrelated',
           'third', 'uncommitted-launch', None, 'reserved', None))
db.execute('UPDATE attempts SET pool = ?', ('spilled-' * 65536,))
print('transaction spilled', flush=True)
sys.stdin.buffer.read(1)
"""
    with subprocess.Popen([sys.executable, '-I', '-c', writer, str(path), *abandoned.identity],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as child:
        try:
            assert child.stdout.readline() == 'transaction spilled\n'
            assert journal.exists() and path.read_bytes() != original
        finally:
            child.kill()
        assert child.wait() == -signal.SIGKILL

    assert journal.exists()
    assert resting_failover.hold_state(w.seat) is resting_failover.Hold.HELD, 'the first ordinary read recovers and sees the committed claim'
    assert not journal.exists()
    unrelated = Seat(w.ship, replace(w.seat.spec, name='unrelated'), w.svc.tmux)
    assert resting_failover.hold_state(unrelated) is resting_failover.Hold.CLEAR
    assert resting_failover.attempts(runtime) == {w.seat.name: claim}
    assert resting_failover.spent(runtime, chosen)
    assert not resting_failover.spent(runtime, abandoned)


def test_corrupt_attempt_database_blocks_new_provisional_moves_only(world, monkeypatch):
    w = world
    w.remember('rest', w.window())
    w.expired()
    assert candidate(w) is not None and resting_failover.eligible_source(w.seat)
    path = w.ship.runtime / resting_failover.FILE
    damaged = b'synthetic unusable SQLite data'
    path.write_bytes(damaged)
    assert resting_failover.hold_state(w.seat) is resting_failover.Hold.CLEAR
    assert candidate(w) is None
    w.svc.failover()
    assert not w.live.launches and w.seat.launched().pool == 'first'
    alarm = f'{path.name} unreadable, needs a human'
    status = watch.facts(w.ship, w.svc.tmux, walls=[], login=[])
    assert alarm in status.relaunches and alarm in status.line()

    source = w.ship.pool('first').config_dir / ClaudeHarness.credential_file
    os.utime(source, (w.clock.wall.timestamp(),) * 2)
    failure = TurnFailure('synthetic sign-in refusal', w.clock.wall, kind=AUTHENTICATION)
    monkeypatch.setattr(ClaudeHarness, 'state', lambda *_: State(turn_open=False, failure=failure))
    relaunch = Mock(return_value='synthetic sign-in resumed')
    monkeypatch.setattr(w.svc, 'relaunch', relaunch)
    w.svc.authentications()
    relaunch.assert_called_once_with(w.seat, True)
    assert auth_failure._attempt(w.seat) is not None
    assert path.read_bytes() == damaged


def test_recovers_exact_provenance_across_repeated_expiry_and_restart(world):
    w = world
    started = w.clock.wall
    w.remember('rest', w.window(reset=5 * 3600), at=started - timedelta(minutes=55))
    result = w.expired()
    assert isinstance(result, claude_sign_in.ExpiredAccess)
    assert candidate(w).identity == result.identity
    for minutes in (10, 61, 240):
        w.advance((started + timedelta(minutes=minutes) - w.clock.wall).total_seconds())
        w.restart()
        assert candidate(w).identity == result.identity
        assert w.expired().identity == result.identity
        usage = w.svc.ledger.usage
        assert usage.expired_access('rest', result.identity, usage.rows('rest'), w.clock.wall) == (started, [])
        assert not quota.cached_open(w.ship, w.svc.ledger, 'rest', w.seat)
        assert candidate(w).identity == result.identity


@pytest.mark.parametrize('case', ['missing', 'aged', 'future-full', 'elapsed-full', 'elapsed-underfull',
                                 'incomplete', 'scope-unknown', 'independent-error', 'remote-error',
                                 'text-only', 'malformed-provenance', 'retimed-attempt', 'retimed-window'])
def test_refuses_windows_and_failed_reads_without_complete_proof(world, case):
    w = world
    windows, at, error = [w.window()], None, None
    if case == 'missing':
        windows = []
    elif case == 'aged':
        at = w.clock.wall - timedelta(hours=1)
    elif case == 'future-full':
        windows = [w.window(1)]
    elif case.startswith('elapsed'):
        windows = [w.window(1 if case == 'elapsed-full' else .4, reset=-1)]
    elif case == 'incomplete':
        windows += [w.window(name='7d', reset=None)]
    elif case == 'scope-unknown':
        windows += [w.window(name='other', group='unknown')]
    elif case == 'independent-error':
        windows += [replace(w.window(name='7d'), uncertainty='synthetic uncertainty')]
    elif case == 'remote-error':
        error = 'HTTP 401'
    w.remember('rest', *windows, at=at, error=error)
    if case == 'text-only':
        w.svc.ledger.record_usage('rest', Usage((), w.clock.wall, quota.ACCESS_WAITING_FOR_RENEWAL, called=False))
    else:
        w.expired()
    db = w.svc.ledger.db
    if case == 'independent-error':
        assert all(row.error.endswith(quota.ACCESS_WAITING_FOR_RENEWAL) for row in w.svc.ledger.usage.rows('rest'))
    elif case == 'malformed-provenance':
        db.execute("UPDATE usage_expired_access SET prior_errors='{}' WHERE pool='rest'")
    elif case == 'retimed-attempt':
        db.execute("UPDATE usage_reads SET at=? WHERE pool='rest'", ((w.clock.wall-timedelta(seconds=1)).isoformat(),))
    elif case == 'retimed-window':
        db.execute("UPDATE pools SET resets_at=? WHERE pool='rest'", ((w.clock.wall+timedelta(hours=2)).isoformat(),))
    db.commit()
    assert candidate(w) is None
    w.advance(300)
    w.expired()
    assert candidate(w) is None


@pytest.mark.parametrize('minutes', [10, 61, 240])
def test_passed_reset_stays_out_until_a_suitable_new_read(world, minutes):
    w = world
    w.remember('rest', w.window(reset=minutes * 60))
    w.expired()
    w.advance(minutes * 60 - 1)
    w.expired()
    assert candidate(w)
    w.advance(1)
    assert candidate(w) is None
    w.expired()
    assert candidate(w) is None
    w.advance(1)
    w.remember('rest', w.window(reset=(minutes + 60) * 60), at=w.clock.wall)
    w.advance(1)
    w.expired()
    assert candidate(w)


@pytest.mark.parametrize('case', ['applicable', 'ambiguous', 'other-family'])
def test_refusals_require_proof_they_do_not_apply(world, case):
    w = world
    w.remember('rest', w.window())
    w.expired()
    wall = Wall('synthetic wall', w.clock.wall, w.clock.wall + timedelta(hours=1), harness='claude',
                group={'applicable': 'sonnet', 'ambiguous': None, 'other-family': 'opus'}[case])
    w.svc.ledger.quota.observe('synthetic', 'other', 'rest', wall, w.clock.wall)
    assert bool(candidate(w)) is (case == 'other-family')


def test_other_family_errors_do_not_erase_proof_of_applicable_windows(world):
    w = world
    w.remember('rest', w.window(), w.window(1, name='opus / 7d', group='opus'),
               w.window(name='opus / 5h', group='opus', reset=None))
    w.expired()
    assert candidate(w)


@pytest.mark.parametrize('retained_pool', ['alias', 'first'])
def test_resting_means_no_live_seat_uses_any_declared_alias(world, monkeypatch, retained_pool):
    w = world
    w.remember('rest', w.window())
    w.expired()
    w.ship.pools['alias'] = replace(w.ship.pool('rest'), name='alias')
    w.ship.seats['another'] = replace(w.seat.spec, name='another', pools=(retained_pool,))
    occupying = SimpleNamespace(launched=lambda: SimpleNamespace(pool='alias'), is_up=True)
    monkeypatch.setattr(w.svc, 'seat', lambda name: occupying)
    assert candidate(w) is None
    occupying.is_up = False
    assert candidate(w)


@pytest.mark.parametrize('age', [55, 60])
def test_last_good_alias_rows_use_the_expiry_anchor(world, age):
    w = world
    w.ship.pools['alias'] = replace(w.ship.pool('rest'), name='alias')
    w.remember('alias', w.window(reset=5 * 3600), at=w.clock.wall - timedelta(minutes=age))
    w.remember('rest', w.window(reset=5 * 3600))
    w.expired()
    w.advance(240 * 60)
    assert bool(candidate(w)) is (age < 60)


@pytest.mark.parametrize('case', ['new-expiry', 'no-refresh', 'ended-grant', 'rejected', 'malformed'])
def test_expired_identity_and_renewal_are_rechecked(world, monkeypatch, case):
    w = world
    w.remember('rest', w.window())
    w.expired()
    pool = w.ship.pool('rest')
    old = (w.clock.wall - timedelta(minutes=1)).timestamp()
    if case == 'rejected':
        monkeypatch.setattr(ClaudeHarness, 'renewal_failed', lambda *a: True)
    else:
        make_sign_in(pool, old - 1 if case == 'new-expiry' else 'unknown' if case == 'malformed' else old,
                     refresh=case != 'no-refresh', grant=w.clock.wall if case == 'ended-grant' else None)
    assert candidate(w) is None


def test_a_changed_generation_needs_its_own_last_good_read(world):
    w = world
    w.remember('rest', w.window(reset=5 * 3600))
    old = w.expired().identity
    w.advance(61 * 60)
    assert candidate(w).identity == old
    make_sign_in(w.ship.pool('rest'), (w.clock.wall - timedelta(minutes=1)).timestamp())
    assert candidate(w) is None
    assert w.expired().identity != old
    assert candidate(w) is None
    w.advance(1)
    w.remember('rest', w.window(reset=5 * 3600), at=w.clock.wall)
    w.advance(1)
    fresh = w.expired().identity
    assert fresh != old and candidate(w).identity == fresh


@pytest.mark.parametrize('scope', ['human', 'desk', 'office'])
def test_scope_excludes_person_owned_seats(world, scope):
    w = world
    w.remember('rest', w.window())
    w.expired()
    if scope == 'human':
        w.ship.sessions['crew'] = replace(w.ship.sessions['crew'], human=True)
    elif scope == 'desk':
        w.seat.spec = replace(w.seat.spec, role='desk')
    else:
        w.ship.sessions['crew'] = replace(w.ship.sessions['crew'], name='office')
    w.svc.failover()
    assert not w.live.launches and not resting_failover.attempts(w.ship.runtime)


def test_ordinary_destination_and_explicit_select_do_not_claim(world):
    w = world
    w.remember('rest', w.window())
    w.expired()
    w.remember('third', w.window())
    w.svc.failover()
    assert w.seat.launched().pool == 'third'
    assert not resting_failover.attempts(w.ship.runtime)
    w.svc.select(w.seat, 'rest')
    assert w.seat.launched().pool == 'rest'
    assert not resting_failover.attempts(w.ship.runtime)


@pytest.mark.parametrize('rewrite', ['milliseconds', 'negative-zero'])
def test_attempt_is_shared_by_aliases_and_same_expiry_rewrites(world, tmp_path, rewrite):
    w = world
    folder = tmp_path / 'alias'
    folder.symlink_to(w.ship.pool('rest').config_dir, target_is_directory=True)
    w.ship.pools['alias'] = replace(w.ship.pool('rest'), name='alias', config_dir=folder)
    expiry = 0 if rewrite == 'negative-zero' else (w.clock.wall - timedelta(minutes=1)).timestamp()
    make_sign_in(w.ship.pool('rest'), expiry)
    w.seat._declaration = replace(w.seat.declaration, pools=(*w.seat.spec.pools, 'alias'))
    w.remember('rest', w.window())
    w.remember('alias', w.window())
    original = w.expired()
    alias = w.expired('alias')
    assert original.identity == alias.identity
    w.svc.failover()
    assert w.seat.launched().pool == 'rest'
    make_sign_in(w.ship.pool('alias'), -0.0 if rewrite == 'negative-zero' else expiry * 1000)
    w.restart()
    rewritten = w.expired('alias')
    assert rewritten.identity == original.identity
    choice = resting_failover.Candidate('alias', rewritten.identity)
    assert resting_failover.spent(w.ship.runtime, choice)
    assert resting_failover.claim(w.seat, choice, 'another launch') is None


@pytest.mark.parametrize('change', ['unit', 'unknown-unit', 'binding', 'composer', 'became-ordinary', 'access',
                                    'busy', 'resume', 'unconfirmed-delivery'])
def test_revalidation_holds_without_spending_the_attempt(world, monkeypatch, change):
    w = world
    w.remember('rest', w.window())
    w.expired()
    original = w.seat.launched()

    def recheck(_):
        if change == 'unit':
            return unit_hold.SOON
        if change == 'unknown-unit':
            return unit_hold.UNKNOWN
        if change == 'binding':
            replace(original, at=(w.clock.wall-timedelta(seconds=30)).isoformat()).write(w.seat.trusted/'launch.json')
        if change == 'composer':
            w.live.screen = Screen('', 'unfinished typing', working=False)
        if change == 'became-ordinary':
            w.remember('third', w.window())
        if change == 'access':
            make_sign_in(w.ship.pool('rest'), w.clock.wall.timestamp(), refresh=False)
        return None

    monkeypatch.setattr(resting_failover, 'unit_reason', recheck)
    if change == 'resume':
        def refuse_resume(*_):
            raise resume_target.Unproved(w.seat.name, launch=original)
        monkeypatch.setattr(w.seat, '_require_resume', refuse_resume)
    if change == 'unconfirmed-delivery':
        monkeypatch.setattr(pending, 'active', lambda *_: [SimpleNamespace(data={'phase': 'submitting'})])
    with shutdown.Stop(w.ship.runtime).installed():
        if change == 'busy':
            with w.seat._lifecycle():
                w.svc.failover()
        else:
            w.svc.failover()
    assert not w.live.launches and not resting_failover.attempts(w.ship.runtime)
    assert not relaunch_intent.path(w.ship.runtime, w.seat.name).exists()
    if change in ('unit', 'unknown-unit'):
        message = 'configured user unit' if change == 'unit' else 'needs a human'
        assert message in move_hold.wall_reading(w.seat)[0]
        monkeypatch.setattr(resting_failover, 'unit_reason', lambda *_: None)
        w.svc.failover()
        assert w.seat.launched().pool == 'rest'


@pytest.mark.parametrize('failure', ['raise', 'login', 'record'])
def test_failed_or_rejected_launch_remains_visible_and_does_not_retry(world, failure):
    w = world
    w.remember('rest', w.window())
    identity = w.expired().identity
    if failure == 'raise':
        w.live.start_error = ShipError('synthetic failed launch')
    elif failure == 'login':
        w.live.landing = Screen('', None, interruption=LOGIN_QUESTION)
    w.svc.failover()
    if failure == 'record':
        failed = TurnFailure('synthetic rejection', datetime.now(timezone.utc), kind=AUTHENTICATION)
        attempt = resting_failover.attempt_for(w.seat)
        assert resting_failover.observe_record(w.seat, attempt, SimpleNamespace(failure=failed))
        auth_failure.run(w.svc)
    w.restart()
    relaunch_intent.finish(w.svc)
    w.svc.failover()
    assert len(w.live.launches) == 1
    assert resting_failover.claim(w.seat, resting_failover.Candidate('rest', identity), 'later source') is None
    assert resting_failover.hold_state(w.seat) is resting_failover.Hold.HELD
    expected = {'raise': 'launch failed', 'login': 'needs a login', 'record': 'rejected the sign-in'}[failure]
    stated = watch.facts(w.ship, w.svc.tmux, walls=[], login=[])
    assert any(expected in item for item in stated.relaunches)
    assert expected in stated.line() and 'qd up worker' in stated.line()


def test_restarted_service_leaves_an_interrupted_claim_visible_without_replaying(world, monkeypatch):
    w = world
    w.remember('rest', w.window())
    w.expired()
    chosen = candidate(w)
    started = w.seat.launched()
    assert resting_failover.claim(w.seat, chosen, started.at)
    relaunch_intent._write(w.ship.runtime, dict(
        version=1, seat=w.seat.name, kind=relaunch_intent.RESTING, at=w.clock.wall.isoformat(),
        phase=relaunch_intent.RELAUNCHING, resume=True, owner={'pid': 10, 'start': 'synthetic'},
        before=started.at, destination={'pool': 'rest', 'harness': 'claude'}))
    w.seat._relaunch_down()
    w.restart()
    monkeypatch.setattr(relaunch_intent, '_gone', lambda *_: True)
    monkeypatch.setattr(w.svc, 'seat', lambda *_: w.seat)
    relaunch_intent.finish(w.svc)
    assert not w.live.launches
    assert relaunch_intent.read(w.ship.runtime, 'worker.json', w.ship.seats)['phase'] == relaunch_intent.FAILED
    assert 'resting move outcome unknown' in watch.facts(w.ship, w.svc.tmux, walls=[], login=[]).line()
    assert resting_failover.spent(w.ship.runtime, chosen)


@pytest.mark.parametrize('lost', ['write-error', 'missing-claim', 'service-ended'])
def test_launch_without_a_durable_outcome_keeps_its_intent(world, monkeypatch, lost):
    w = world
    w.remember('rest', w.window())
    w.expired()
    intent = relaunch_intent.path(w.ship.runtime, w.seat.name)

    def interrupted(runtime, attempt, phase, destination):
        assert phase == 'launched' and w.seat.is_up and w.seat.launched().at == destination
        assert json.loads(intent.read_text())['destination'] == {'pool': 'rest', 'harness': 'claude'}
        if lost == 'service-ended':
            raise SystemExit('synthetic interruption after launch')
        if lost == 'write-error':
            raise sqlite3.OperationalError('synthetic outcome write failure')
        return False

    with monkeypatch.context() as cut, shutdown.Stop(w.ship.runtime).installed():
        cut.setattr(resting_failover, 'outcome', interrupted)
        if lost == 'service-ended':
            # A killed service gets no chance to mark the intent failed.
            cut.setattr(relaunch_intent, '_hold_resting', lambda *_: None)
            with pytest.raises(SystemExit, match='synthetic interruption after launch'):
                w.svc.failover()
        else:
            w.svc.failover()
    assert len(w.live.launches) == 1 and w.seat.launched().pool == 'rest'
    assert resting_failover.attempt_for(w.seat).phase == 'reserved'
    expected = relaunch_intent.RELAUNCHING if lost == 'service-ended' else relaunch_intent.FAILED
    assert json.loads(intent.read_text())['phase'] == expected
    w.restart()
    monkeypatch.setattr(relaunch_intent, '_gone', lambda *_: True)
    monkeypatch.setattr(w.svc, 'seat', lambda *_: w.seat)
    relaunch_intent.finish(w.svc)
    assert json.loads(intent.read_text())['phase'] == relaunch_intent.FAILED
    w.svc.authentications()
    w.svc.failover()
    assert len(w.live.launches) == 1
    assert 'qd up worker' in watch.facts(w.ship, w.svc.tmux, walls=[], login=[]).line()
    with pytest.raises(resting_failover.Held, match='qd up worker'):
        w.seat.up()
    assert intent.exists() and resting_failover.hold_state(w.seat) is resting_failover.Hold.HELD
    monkeypatch.setattr(cli, 'load_ship', lambda _: w.ship)
    monkeypatch.setattr(cli, 'seat_of', lambda *_args, **_kwargs: w.seat)
    monkeypatch.setattr(cli.install, 'prepare_up', lambda _: ([], False))
    monkeypatch.setattr(cli.install, 'delegate_up', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli.service, 'start', lambda _: 'synthetic service already running')
    args = SimpleNamespace(seat=w.seat.name, fresh=False)
    assert cli.cmd_up(args)[0] == 'worker already up in %2'
    assert cli.cmd_up(args) == ['worker already up in %2']  # still unproved, so a second up changes nothing
    assert not intent.exists() and len(w.live.launches) == 1
    assert resting_failover.hold_state(w.seat) is resting_failover.Hold.HELD
    attempt = resting_failover.attempt_for(w.seat)
    assert (attempt.phase, attempt.destination) == ('launched', w.seat.launched().at)
    assert resting_failover.spent(w.ship.runtime, resting_failover.Candidate(attempt.pool, (attempt.account, attempt.generation)))


def test_successful_move_resumes_carries_queued_work_and_never_chains(world, monkeypatch):
    w = world
    w.remember('rest', w.window())
    w.expired()
    order = w.svc.store.new('order', 'issuer', w.seat.name, 'synthetic queued work')
    launch = w.seat.launched()
    queued = w.svc.attempts.open(order, w.seat.name, launch.pane, launch.pid, launch.at)
    w.svc.attempts.close(queued, 'queued', 'synthetic queued delivery')
    w.live.screen = Screen('', '', working=False, queued=1)
    intent = relaunch_intent.path(w.ship.runtime, w.seat.name)
    original = resting_failover.outcome

    def checked_outcome(runtime, attempt, phase, destination):
        assert intent.exists() and phase == 'launched'
        return original(runtime, attempt, phase, destination)

    with monkeypatch.context() as check, shutdown.Stop(w.ship.runtime).installed():
        check.setattr(resting_failover, 'outcome', checked_outcome)
        w.svc.failover()
    assert not intent.exists()
    assert w.seat.launched().pool == 'rest' and w.seat.launched().resume == 'synthetic-root'
    assert w.seat.record() == w.record.path
    assert order.id in w.live.launches[0][-1]
    assert w.svc.attempts.latest()[order.id] == 'launch'
    assert w.svc.attempts.for_message(order.id)[0].result == 'queued'
    w.remember('rest', w.window(1), at=w.clock.wall + timedelta(seconds=1))
    w.remember('third', w.window())
    w.expired('third')
    w.restart()
    w.svc.failover()
    assert len(w.live.launches) == 1 and w.seat.launched().pool == 'rest'


@pytest.mark.parametrize('proved', [False, True])
def test_native_quota_refusal_holds_only_before_proved_use(world, monkeypatch, proved):
    w = world
    w.remember('rest', w.window())
    w.expired()
    order = w.svc.store.new('order', 'issuer', w.seat.name, 'synthetic queued work')
    source = w.seat.launched()
    queued = w.svc.attempts.open(order, w.seat.name, source.pane, source.pid, source.at)
    w.svc.attempts.close(queued, 'queued', 'synthetic queued delivery')
    w.live.screen = Screen('', '', working=False, queued=1)
    w.live.landing = Screen('', '', working=False)
    w.svc.failover()
    launch = w.seat.launched()
    assert launch.pool == 'rest'
    assert order.id in w.live.launches[0][-1]
    carried = w.svc.attempts.for_message(order.id)
    assert carried[0].result == 'queued'
    assert w.svc.attempts.latest()[order.id] == 'launch'

    w.clock.wall = datetime.fromisoformat(launch.at) + timedelta(seconds=1)
    monkeypatch.setattr('quarterdeck.harness.synthetic_record.now', lambda: w.clock.wall)
    if proved:
        w.record.begin('synthetic input after launch', w.seat.spec.model, 'none')
        w.record.end('synthetic successful response after launch')
        w.advance(1)
    w.record.begin('synthetic input refused at quota', w.seat.spec.model, 'none')
    row = refusal(at=w.clock.wall, reset=w.clock.wall + timedelta(hours=1))
    w.record.entry('assistant', row['message'], isApiErrorMessage=row['isApiErrorMessage'],
                   error=row['error'], apiError=row['apiError'], apiErrorParams=row['apiErrorParams'])
    w.record.entry('system', {}, subtype='turn_duration', durationMs=1)
    state = w.seat.harness.state(w.record.path)
    assert state.completed_turns[-1].successful is False
    assert len(state.walls) == 1 and state.walls[0].at == w.clock.wall
    w.svc.account_records(process_table={})
    assert quota.current_walled(w.seat, w.svc.ledger)

    w.remember('third', w.window(), at=w.clock.wall - timedelta(seconds=1))
    other = resting_failover.Candidate('third', w.expired('third').identity)
    w.svc.failover()
    assert len(w.live.launches) == 1
    assert not resting_failover.spent(w.ship.runtime, other)
    w.advance(1)
    w.remember('third', w.window(), at=w.clock.wall)
    w.restart()
    w.svc.failover()
    assert len(w.live.launches) == 1
    assert w.svc.attempts.for_message(order.id) == carried
    assert w.svc.attempts.latest()[order.id] == 'launch'

    w.svc.authentications()
    relaunch_intent.finish(w.svc)
    w.svc.failover()
    w.restart()
    w.svc.authentications()
    w.svc.failover()
    attempt = resting_failover.attempt_for(w.seat)
    stated = watch.facts(w.ship, w.svc.tmux, walls=[], login=[])
    if proved:
        assert attempt.phase == 'ready'
        assert len(w.live.launches) == 2 and w.seat.launched().pool == 'third'
        assert not stated.relaunches
    else:
        assert attempt.phase == 'quota-refused' and attempt.destination == launch.at
        assert resting_failover.hold_state(w.seat) is resting_failover.Hold.HELD
        assert len(w.live.launches) == 1 and w.seat.launched() == launch
        assert not resting_failover.spent(w.ship.runtime, other)
        assert w.svc.attempts.for_message(order.id) == carried
        assert w.svc.attempts.latest()[order.id] == 'launch'
        hold = 'worker: resting subscription rest: quota refused before first success; needs the desk (qd up worker)'
        assert stated.relaunches == [hold]
        assert 'qd up worker' in stated.line()


@pytest.mark.parametrize('linked', [True, False], ids=['terminal', 'unbound'])
def test_native_usage_refusal_without_duration_must_close_before_failover(world, monkeypatch, linked):
    w = world
    w.remember('first', w.window(0.25), at=w.clock.wall)
    w.remember('rest', w.window(), at=w.clock.wall)
    assert not quota.current_walled(w.seat, w.svc.ledger)
    rows = terminal_refusal_rows(at=w.clock.wall, reset=w.clock.wall + timedelta(hours=1),
                                session=w.record.session)
    rows[0]['parentUuid'] = w.record.parent
    if not linked:
        rows[-1]['parentUuid'] = w.record.parent
    for row in rows:
        w.record.write(row)
    w.svc.account_records(process_table={})
    assert quota.current_walled(w.seat, w.svc.ledger)

    route = Mock(return_value=None)
    monkeypatch.setattr(w.seat, 'failover_to', route)
    w.svc.failover()
    if linked:
        assert w.svc.not_idle_at_wall(w.seat) is None
        route.assert_called_once_with('rest')
    else:
        assert w.svc.not_idle_at_wall(w.seat) == 'a turn is open'
        route.assert_not_called()
    assert w.live.launches == [] and w.seat.launched().pool == 'first'


def test_optional_named_unit_declaration(tmp_path):
    assert _from_text('[ship]\n', tmp_path).resting_subscription_hold_unit is None
    ship = _from_text('[ship]\nresting_subscription_hold_unit = "example-refresh.timer"\n', tmp_path)
    assert ship.resting_subscription_hold_unit == 'example-refresh.timer'
    with pytest.raises(ShipError, match='systemd user'):
        _from_text('[ship]\nresting_subscription_hold_unit = "?"\n', tmp_path)


@pytest.mark.parametrize('phase', UNSETTLED)
def test_ordinary_failover_waits_for_an_intentless_resting_move(world, phase):
    w = world
    launch = w.seat.launched()
    w.remember('third', w.window())
    choice, attempt = save_attempt(w.seat, phase)
    assert w.svc.known_open(w.seat, 'third')
    w.svc.failover()
    assert not w.live.launches and w.seat.launched() == launch
    assert resting_failover.attempt_for(w.seat) == attempt
    settle(w.seat, attempt)
    w.svc.failover()
    assert len(w.live.launches) == 1 and w.seat.launched().pool == 'third'
    assert resting_failover.spent(w.ship.runtime, choice)


@pytest.mark.parametrize('phase', UNSETTLED)
def test_sign_in_recovery_waits_for_an_intentless_resting_move(world, monkeypatch, phase):
    w = world
    source = w.ship.pool('first').config_dir / ClaudeHarness.credential_file
    os.utime(source, (w.clock.wall.timestamp(),) * 2)
    failure = TurnFailure('synthetic sign-in refusal', w.clock.wall, kind=AUTHENTICATION)
    monkeypatch.setattr(ClaudeHarness, 'state', lambda *_: State(turn_open=False, failure=failure))
    relaunch = Mock(return_value='synthetic sign-in resumed')
    monkeypatch.setattr(w.svc, 'relaunch', relaunch)
    _, attempt = save_attempt(w.seat, phase)
    w.svc.authentications()
    assert auth_failure._attempt(w.seat) is None
    relaunch.assert_not_called()
    saved = resting_failover.attempt_for(w.seat)
    assert saved.phase == ('rejected' if phase == 'launched' else phase)
    settle(w.seat, saved)
    w.svc.authentications()
    relaunch.assert_called_once_with(w.seat, True)


@pytest.mark.parametrize('phase', [*UNSETTLED, 'intent'])
@pytest.mark.parametrize('verb', ['restart', 'fresh'])
def test_direct_and_service_relaunch_refuse_resting_holds(world, monkeypatch, phase, verb):
    w = world
    launch = w.seat.launched()
    intent = relaunch_intent.path(w.ship.runtime, w.seat.name)
    if phase == 'intent':
        relaunch_intent._write(w.ship.runtime, dict(
            version=1, seat=w.seat.name, kind=relaunch_intent.RESTING, resume=True, at=w.clock.wall.isoformat(),
            phase=relaunch_intent.FAILED, owner={'pid': 1, 'start': 'synthetic'}, before=launch.at))
    else:
        save_attempt(w.seat, phase)
    hold_file = intent if phase == 'intent' else w.ship.runtime / resting_failover.FILE
    saved = hold_file.read_bytes()
    lift = Mock(wraps=write_bound.lift)
    monkeypatch.setattr(write_bound, 'lift', lift)
    monkeypatch.setattr(cli, 'load_ship', lambda _: w.ship)
    monkeypatch.setattr(cli, 'seat_of', lambda *_args, **_kwargs: w.seat)
    monkeypatch.setattr(w.svc, 'seat', lambda *_: w.seat)
    command = 'qd down worker, then qd up worker' if phase == 'launched' else 'qd up worker'
    for call in (lambda: getattr(w.seat, verb)(),
                 lambda: getattr(cli, 'cmd_' + verb)(SimpleNamespace(seat=w.seat.name)),
                 lambda: w.svc.answer(dict(kind='relaunch', seat=w.seat.name, resume=verb == 'restart'))):
        with pytest.raises(resting_failover.Held, match=command):
            call()
        assert not w.live.launches and w.seat.launched() == launch
        assert hold_file.read_bytes() == saved
    lift.assert_not_called()
    assert intent.exists() == (phase == 'intent')


@pytest.mark.parametrize('verb', ['restart', 'fresh'])
def test_working_cli_relaunch_reaches_service_while_another_process_owns_lifecycle(world, monkeypatch, verb):
    w = world
    runtime, name = w.ship.runtime, w.seat.name
    w.record.begin('synthetic ongoing input', w.seat.spec.model, 'none')
    assert w.seat.is_working() is True
    launch = w.seat.launched()
    monkeypatch.setattr(cli, 'load_ship', lambda _: w.ship)
    monkeypatch.setattr(cli, 'seat_of', lambda *_: w.seat)
    monkeypatch.setattr(lifecycle, 'WAIT_SECONDS', 0)

    holder = '''
from pathlib import Path
import sys

sys.path.insert(0, sys.argv[1])
from quarterdeck import lifecycle

with lifecycle.claim(Path(sys.argv[2]), sys.argv[3]):
    print('claimed', flush=True)
    sys.stdin.read()
'''
    source = Path(lifecycle.__file__).resolve().parents[1]
    with subprocess.Popen([sys.executable, '-I', '-c', holder, str(source), str(runtime), name],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as child:
        try:
            assert child.stdout.readline() == 'claimed\n'

            def request(target, kind, *, seat, resume, caller, origin):
                assert (target, kind, seat, resume) == (runtime, 'relaunch', name, verb == 'restart')
                assert origin['at'] == launch.at
                with pytest.raises(lifecycle.Busy), lifecycle.claim(runtime, name):
                    pytest.fail('the child released its claim before the request reached the service')
                return 'synthetic queued relaunch'

            ask = Mock(side_effect=request)
            monkeypatch.setattr(cli.service, 'request', ask)
            command = getattr(cli, f'cmd_{verb}')
            assert command(SimpleNamespace(seat=name)) == 'synthetic queued relaunch'
            assert ask.call_count == 1
        finally:
            child.stdin.close()
    assert child.returncode == 0
    assert not w.live.launches and w.seat.launched() == launch


@pytest.mark.parametrize('verb', ['restart', 'fresh'])
def test_direct_cli_relaunch_rechecks_a_resting_attempt_recorded_after_precheck(world, monkeypatch, verb):
    w = world
    launch = w.seat.launched()
    quiet = w.seat.is_working
    assert quiet() is False
    monkeypatch.setattr(cli, 'load_ship', lambda _: w.ship)
    monkeypatch.setattr(cli, 'seat_of', lambda *_: w.seat)

    def record_attempt():
        # A separate writer cannot update the CLI's cached snapshot.
        Context().run(save_attempt, w.seat, 'reserved')
        monkeypatch.setattr(w.seat, 'is_working', quiet)
        return quiet()

    monkeypatch.setattr(w.seat, 'is_working', record_attempt)
    with pytest.raises(resting_failover.Held, match='qd up worker'):
        getattr(cli, f'cmd_{verb}')(SimpleNamespace(seat=w.seat.name))

    assert w.seat.is_up and w.seat.launched() == launch and not w.live.launches
    assert not relaunch_intent.path(w.ship.runtime, w.seat.name).exists()
    assert resting_failover.attempt_for(w.seat).phase == 'reserved'


@pytest.mark.parametrize('kind', sorted(relaunch_intent.KINDS - {relaunch_intent.RESTING}))
def test_non_resting_section_cannot_overwrite_a_hold(world, kind):
    w = world
    save_attempt(w.seat, 'reserved')
    with shutdown.Stop(w.ship.runtime).installed():
        with pytest.raises(resting_failover.Held, match='qd up worker'):
            with relaunch_intent.section(w.svc, w.seat, kind, resume=True):
                pytest.fail('a resting hold let another relaunch begin')
    assert not relaunch_intent.path(w.ship.runtime, w.seat.name).exists()


@pytest.mark.parametrize(('code', 'expected'), [
    (sqlite3.SQLITE_BUSY | (3 << 8), resting_failover.Hold.UNKNOWN),
    (sqlite3.SQLITE_LOCKED | (1 << 8), resting_failover.Hold.UNKNOWN),
    (sqlite3.SQLITE_CORRUPT | (1 << 8), resting_failover.Hold.CLEAR),
])
def test_extended_database_code_uses_its_primary_code(world, monkeypatch, code, expected):
    error = sqlite3.OperationalError('synthetic read failed')
    error.sqlite_errorcode = code
    monkeypatch.setattr(resting_failover, 'attempts', Mock(side_effect=error))
    assert resting_failover.hold_state(world.seat) is expected


def test_sign_in_recovery_reads_busy_attempts_once_then_recovers(world, monkeypatch):
    w = world
    runtime = w.ship.runtime
    save_attempt(w.seat, 'ready')
    source = w.ship.pool('first').config_dir / ClaudeHarness.credential_file
    os.utime(source, (w.clock.wall.timestamp(),) * 2)
    failure = TurnFailure('synthetic sign-in refusal', w.clock.wall, kind=AUTHENTICATION)
    monkeypatch.setattr(ClaudeHarness, 'state', lambda *_: State(turn_open=False, failure=failure))
    relaunch = Mock(return_value='synthetic sign-in resumed')
    monkeypatch.setattr(w.svc, 'relaunch', relaunch)
    reads = Mock(wraps=resting_failover.attempts)
    monkeypatch.setattr(resting_failover, 'attempts', reads)
    bound = w.seat.launched()
    db = sqlite3.connect(runtime / resting_failover.FILE)
    try:
        db.execute('BEGIN EXCLUSIVE')
        with resting_failover.reading_pass():
            w.svc.authentications()
            w.svc.failover()
            assert resting_failover.status(w.ship, {}) == [f'{resting_failover.FILE} busy; waiting']
        reads.assert_called_once_with(runtime)
        assert w.seat.launched() == bound and not w.live.launches and auth_failure._attempt(w.seat) is None
        relaunch.assert_not_called()
    finally:
        db.close()

    reads.reset_mock()
    w.svc.authentications()
    reads.assert_called_once_with(runtime)
    relaunch.assert_called_once_with(w.seat, True)
    assert auth_failure._attempt(w.seat) is not None


@pytest.mark.parametrize(('duty', 'expected'), [('authentications', 'rejected'), ('failover', 'login')])
def test_busy_observation_leaves_attempts_for_retry_and_visits_later_seats(world, monkeypatch, duty, expected):
    w = world
    runtime = w.ship.runtime
    spec = replace(w.seat.spec, name='neighbor')
    w.ship.seats[spec.name] = spec
    neighbor = w.svc.seat(spec.name)
    launch = w.seat.launched()
    other = replace(launch, pane='%4', pid=104)
    other.write(neighbor.trusted / 'launch.json')
    pane = replace(w.live.pane, id=other.pane, pid=other.pid, window=spec.name)
    pane_of = w.svc.tmux.pane
    monkeypatch.setattr(w.svc.tmux, 'pane', lambda ident: pane if ident == pane.id else pane_of(ident))
    monkeypatch.setattr(w.svc, 'resolved_seats', lambda: [w.seat, neighbor])
    save_attempt(w.seat, 'launched')
    attempt = resting_failover.claim(neighbor, resting_failover.Candidate(other.pool, ('neighbor account', 'generation')),
                                     other.at)
    assert resting_failover.outcome(runtime, attempt, 'launched', other.at)
    written = Mock(wraps=resting_failover.outcome)
    monkeypatch.setattr(resting_failover, 'outcome', written)
    if duty == 'authentications':
        failure = TurnFailure('synthetic sign-in refusal', w.clock.wall, kind=AUTHENTICATION)
        monkeypatch.setattr(ClaudeHarness, 'state', lambda *_: State(turn_open=False, failure=failure))
        monkeypatch.setattr(neighbor, 'record', lambda: w.record.path)
    else:
        monkeypatch.setattr(service, 'Door', lambda seat: SimpleNamespace(
            screen=lambda: Screen('', None, interruption=LOGIN_QUESTION)))
    run = getattr(w.svc, duty)
    before = resting_failover.attempts(runtime)
    db = sqlite3.connect(runtime / resting_failover.FILE)
    try:
        db.execute('BEGIN IMMEDIATE')
        run()
        assert [call.args[1].seat for call in written.call_args_list] == [w.seat.name, neighbor.name]
        assert resting_failover.attempts(runtime) == before
    finally:
        db.close()
    run()
    assert {attempt.phase for attempt in resting_failover.attempts(runtime).values()} == {expected}
    assert not w.live.launches


@pytest.mark.parametrize('observer', ['screen', 'record'])
def test_observation_surfaces_other_sqlite_errors(world, monkeypatch, observer):
    w = world
    _, attempt = save_attempt(w.seat, 'launched')
    failure = TurnFailure('synthetic sign-in refusal', w.clock.wall, kind=AUTHENTICATION)
    error = sqlite3.OperationalError('synthetic SQLite write failure')
    error.sqlite_errorcode = sqlite3.SQLITE_IOERR
    monkeypatch.setattr(resting_failover, 'outcome', Mock(side_effect=error))
    with pytest.raises(sqlite3.OperationalError, match='synthetic SQLite write failure'):
        if observer == 'screen':
            resting_failover.observe_screen(w.seat, attempt, Screen('', None, interruption=LOGIN_QUESTION),
                                            w.seat.launched())
        else:
            resting_failover.observe_record(w.seat, attempt, State(turn_open=False, failure=failure))
    assert resting_failover.attempt_for(w.seat) == attempt
