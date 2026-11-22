"""What a named up of a resting attempt may release, and who may release it. Synthetic sign-ins and panes only."""

from dataclasses import replace
from datetime import timedelta
import json
import os
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from quarterdeck import cli, compaction, lifecycle, post, relaunch_intent, resting_failover as rf, write_bound
from quarterdeck.harness.claude import ClaudeHarness
from quarterdeck.harness.types import AUTHENTICATION, State, TurnFailure
from quarterdeck.seat import Seat
from quarterdeck.ship import ShipError
from quarterdeck.usage_cache import parse
from .resting_attempts import save_attempt
from .test_resting_failover import world  # noqa: F401
from .test_ship_server_recovery import choice_world, stub_up_cli  # noqa: F401


INTERRUPTIONS = (None, 'before-launch', 'after-launch')


@pytest.fixture
def wired(world, monkeypatch):  # noqa: F811
    w = world
    w.svc.tmux.ship = None
    monkeypatch.delenv('QD_SEAT', raising=False)
    monkeypatch.setattr(cli, 'load_ship', lambda _: w.ship)
    monkeypatch.setattr(cli, 'seat_of', lambda *_args, **_kwargs: w.seat)
    monkeypatch.setattr(cli.install, 'prepare_up', lambda _: ([], False))
    monkeypatch.setattr(cli.install, 'delegate_up', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli.service, 'start', lambda _: 'synthetic service')
    monkeypatch.setattr('quarterdeck.credential.reseed', lambda *_args, **_kwargs: [])
    monkeypatch.setattr('quarterdeck.jail_end.end', lambda *_: {})
    w.ship.seats['spock'] = replace(w.seat.spec, name='spock', role='systems')
    w.ship.seats['bridge'] = replace(w.seat.spec, name='bridge', role='desk')
    w.ship.seats['desk'] = replace(w.seat.spec, name='desk', role='systems')  # the role decides, never the name
    return w


def up(w, pool=None):
    return cli.cmd_up(SimpleNamespace(seat=w.seat.name, fresh=False, pool=pool))


def down(w):
    cli.cmd_down(SimpleNamespace(seat=w.seat.name))


def killed_up(w, monkeypatch, when, pool=None):
    """A process killed between the release's two writes, before its launch or after it."""
    with monkeypatch.context() as cut:
        if when == 'after-launch':
            write = rf.outcome

            def stop(runtime, attempt, phase, destination):
                if phase != 'releasing':
                    raise SystemExit('synthetic kill')
                return write(runtime, attempt, phase, destination)

            cut.setattr(rf, 'outcome', stop)
        else:
            cut.setattr(Seat, '_up_with_pool_choice', Mock(side_effect=SystemExit('synthetic kill')))
        with pytest.raises(SystemExit, match='synthetic kill'):
            up(w, pool)


def releases(w):
    log = w.ship.runtime / 'log' / 'service.jsonl'
    rows = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return [row for row in rows if row['event'] == 'resting-release']


def after(launch, seconds=1):
    return parse(launch.at) + timedelta(seconds=seconds)


def prove_first_turn(w, monkeypatch, launch):
    turn = SimpleNamespace(timed=True, successful=True, ended=after(launch), failure=None)
    state = SimpleNamespace(failure=None, walls=(), completed_turns=(turn,), turn_open=False)
    monkeypatch.setattr(ClaudeHarness, 'state', lambda *_: state)


def reject_first_turn(w, monkeypatch, launch):
    failure = TurnFailure('synthetic refusal', after(launch), kind=AUTHENTICATION)
    monkeypatch.setattr(ClaudeHarness, 'state', lambda *_: State(turn_open=False, failure=failure))
    source = w.ship.pool(launch.pool).config_dir / ClaudeHarness.credential_file
    stamp = after(launch, 5).timestamp()
    os.utime(source, (stamp, stamp))  # a newer sign-in, which ordinary recovery would act on


@pytest.mark.parametrize('ending', ['proved', 'rejected'])
@pytest.mark.parametrize('interrupted', INTERRUPTIONS)
@pytest.mark.parametrize('origin', ['launched', 'reserved'])
def test_down_then_up_stays_provisional_until_its_first_turn(wired, monkeypatch, origin, interrupted, ending):
    w = wired
    choice, _ = save_attempt(w.seat, origin, pool='rest')
    down(w)
    if interrupted:
        killed_up(w, monkeypatch, interrupted, 'rest')
        row = rf.attempt_for(w.seat)
        assert (row.phase, row.origin) == ('releasing', origin)
        assert rf.hold_state(w.seat) is rf.Hold.HELD
    up(w, 'rest')
    launch = w.seat.launched()
    row = rf.attempt_for(w.seat)
    assert launch.pool == 'rest' and len(w.live.launches) == 1
    assert (row.phase, row.destination, row.origin) == ('launched', launch.at, None)
    assert rf.hold_state(w.seat) is rf.Hold.HELD and rf.spent(w.ship.runtime, choice)
    assert not releases(w)

    relaunch = Mock(return_value='synthetic relaunch')
    monkeypatch.setattr(w.svc, 'relaunch', relaunch)
    if ending == 'proved':
        prove_first_turn(w, monkeypatch, launch)
        w.svc.authentications()
        assert rf.attempt_for(w.seat).phase == 'ready' and rf.hold_state(w.seat) is rf.Hold.CLEAR
    else:
        reject_first_turn(w, monkeypatch, launch)
        w.svc.authentications()
        assert rf.attempt_for(w.seat).phase == 'rejected' and rf.hold_state(w.seat) is rf.Hold.HELD
    relaunch.assert_not_called()


@pytest.mark.parametrize('interrupted', INTERRUPTIONS)
def test_a_reservation_landing_on_another_subscription_goes_back(wired, monkeypatch, interrupted):
    w = wired
    _, row = save_attempt(w.seat, 'reserved', pool='rest')
    down(w)
    if interrupted:
        killed_up(w, monkeypatch, interrupted, 'third')
    up(w, 'third')
    assert w.seat.launched().pool == 'third' and len(w.live.launches) == 1
    settled = rf.attempt_for(w.seat)
    assert (settled.phase, settled.destination, settled.origin) == ('released', w.seat.launched().at, None)
    assert rf.hold_state(w.seat) is rf.Hold.CLEAR
    assert len(releases(w)) == (2 if interrupted == 'after-launch' else 1)  # logged before the write, so a kill repeats it
    line = releases(w)[-1]
    assert (line['seat'], line['phase']) == (w.seat.name, 'reserved')
    down(w)
    assert rf.hold_state(w.seat) is rf.Hold.CLEAR  # a settled row never matches again


def test_a_reservation_whose_source_still_runs_is_released_and_logged(wired):
    w = wired
    save_attempt(w.seat, 'reserved', pool='rest')
    launch = w.seat.launched()
    assert rf.hold_state(w.seat) is rf.Hold.HELD
    assert up(w)[0] == f'{w.seat.name} already up in {launch.pane}'
    settled = rf.attempt_for(w.seat)
    assert (settled.phase, settled.destination) == ('released', launch.at)
    assert rf.hold_state(w.seat) is rf.Hold.CLEAR and not w.live.launches
    [line] = releases(w)
    assert (line['seat'], line['phase'], line['by']) == (w.seat.name, 'reserved', w.ship.operator)


@pytest.mark.parametrize('phase', ['reserved', 'launched', *rf.DESK_RELEASES])
def test_a_retry_whose_launch_fails_puts_the_row_back_as_it_stood(wired, monkeypatch, phase):
    w = wired
    _, row = save_attempt(w.seat, phase, pool='rest')
    down(w)
    killed_up(w, monkeypatch, 'before-launch', 'rest')
    assert (rf.attempt_for(w.seat).phase, rf.attempt_for(w.seat).origin) == ('releasing', phase)
    w.live.start_error = ShipError('synthetic failed launch')
    with pytest.raises(ShipError, match='synthetic failed launch'):
        up(w, 'rest')
    assert rf.attempt_for(w.seat) == row and rf.hold_state(w.seat) is rf.Hold.HELD
    assert not releases(w)


def test_an_alias_is_the_resting_subscription_and_an_unreadable_one_counts_as_it(wired):
    w = wired
    w.ship.pools['alias'] = replace(w.ship.pools['rest'], name='alias')
    before = rf.Attempt('account', 'generation', w.seat.name, 'rest', 'synthetic source', None, 'reserved')
    landed = lambda pool: SimpleNamespace(pool=pool, at='synthetic launch')  # noqa: E731
    assert rf._after_up(w.seat, before, landed('alias')) == ('launched', 'synthetic launch')
    assert rf._after_up(w.seat, before, landed('undeclared')) == ('launched', 'synthetic launch')
    assert rf._after_up(w.seat, before, landed('third')) == ('released', 'synthetic launch')


@pytest.mark.parametrize('live', [True, False], ids=['live', 'down'])
@pytest.mark.parametrize('killed', [False, True], ids=['first', 'retry'])
@pytest.mark.parametrize('caller', ['spock', 'worker', 'desk', 'undeclared'])
@pytest.mark.parametrize('phase', rf.DESK_RELEASES)
def test_another_seat_is_refused_with_nothing_changed(wired, monkeypatch, phase, caller, killed, live):
    w = wired
    _, row = save_attempt(w.seat, phase)
    if killed:
        assert rf.outcome(w.ship.runtime, row, 'releasing', row.destination)
        row = rf.attempt_for(w.seat)
        assert row.origin == phase
    if not live:
        down(w)
    launch, file = w.seat.launched(), w.ship.runtime / rf.FILE
    saved = file.read_bytes()
    lift, disarm = Mock(), Mock()
    monkeypatch.setattr(write_bound, 'lift', lift)
    monkeypatch.setattr(compaction, 'disarm', disarm)
    monkeypatch.setenv('QD_SEAT', caller)
    with pytest.raises(rf.Held, match='tell the desk'):
        up(w)
    lift.assert_not_called()
    disarm.assert_not_called()
    assert file.read_bytes() == saved and rf.attempt_for(w.seat) == row
    assert w.seat.launched() == launch and not w.live.launches and not releases(w)


@pytest.mark.parametrize('live', [True, False], ids=['live', 'down'])
@pytest.mark.parametrize('killed', [False, True], ids=['first', 'retry'])
@pytest.mark.parametrize('caller', ['bridge', None], ids=['desk-role', 'person'])
@pytest.mark.parametrize('phase', rf.DESK_RELEASES)
def test_the_desk_or_a_person_releases_and_the_log_names_them(wired, monkeypatch, phase, caller, killed, live):
    w = wired
    choice, row = save_attempt(w.seat, phase)
    if killed:
        assert rf.outcome(w.ship.runtime, row, 'releasing', row.destination)
    if not live:
        down(w)
    if caller:
        monkeypatch.setenv('QD_SEAT', caller)
    up(w)
    settled = rf.attempt_for(w.seat)
    assert (settled.phase, settled.destination, settled.origin) == ('released', w.seat.launched().at, None)
    assert rf.hold_state(w.seat) is rf.Hold.CLEAR and rf.spent(w.ship.runtime, choice)
    assert len(w.live.launches) == (0 if live else 1)
    [line] = releases(w)
    assert (line['seat'], line['phase'], line['by']) == (w.seat.name, phase, caller or w.ship.operator)


@pytest.mark.parametrize('killed', [False, True], ids=['recorded', 'interrupted'])
@pytest.mark.parametrize('phase', rf.DESK_RELEASES)
def test_status_and_hold_name_the_desk_for_a_recorded_failure(wired, phase, killed):
    w = wired
    _, row = save_attempt(w.seat, phase)
    if killed:
        assert rf.outcome(w.ship.runtime, row, 'releasing', row.destination)
    [line] = rf.status(w.ship, {w.seat.name: w.seat})
    assert line.endswith(f'needs the desk (qd up {w.seat.name})')
    assert rf.reason(w.seat, rf.Hold.HELD) == f'resting move held; needs the desk (qd up {w.seat.name})'


@pytest.mark.parametrize('phase', rf.DESK_RELEASES)
def test_a_surviving_intent_names_the_desk_for_a_recorded_failure(wired, monkeypatch, phase):
    w = wired
    save_attempt(w.seat, phase)
    relaunch_intent._write(w.ship.runtime, dict(
        version=1, seat=w.seat.name, kind=relaunch_intent.RESTING, resume=True, at=post.now().isoformat(),
        phase=relaunch_intent.FAILED, owner={'pid': 1, 'start': 'synthetic'}, before=w.seat.launched().at))
    monkeypatch.setattr(relaunch_intent, '_gone', lambda *_: True)
    assert relaunch_intent.status(w.ship, {w.seat.name: w.seat}) == [
        f'{w.seat.name}: resting move outcome unknown, needs the desk (qd up {w.seat.name})']


@pytest.mark.parametrize('phase', ['reserved', 'launched'])
def test_an_unproved_row_keeps_its_own_act_for_any_seat(wired, monkeypatch, phase):
    w = wired
    save_attempt(w.seat, phase, pool='rest')
    down(w)
    assert 'the desk' not in ' '.join(rf.status(w.ship, {w.seat.name: w.seat}))
    assert 'the desk' not in rf.reason(w.seat, rf.Hold.HELD)
    monkeypatch.setenv('QD_SEAT', 'spock')
    up(w, 'rest')
    assert rf.attempt_for(w.seat).phase == 'launched' and len(w.live.launches) == 1


def test_a_busy_database_refuses_only_a_seat_that_could_own_an_attempt(wired):
    w = wired
    save_attempt(w.seat, 'ready')
    down(w)
    db = sqlite3.connect(w.ship.runtime / rf.FILE, timeout=0, isolation_level=None)
    try:
        db.execute('BEGIN EXCLUSIVE')
        with pytest.raises(rf.Held, match=f'busy.*qd up {w.seat.name}'):
            up(w)
        assert not w.live.launches and not w.seat.is_up
        w.ship.sessions['crew'] = replace(w.ship.sessions['crew'], human=True)  # now it can own none
        assert rf.hold_state(w.seat) is rf.Hold.CLEAR
        up(w)
    finally:
        db.close()
    assert w.seat.is_up and len(w.live.launches) == 1


@pytest.mark.parametrize('origin', ['launched', 'reserved'])
def test_point_1_a_launch_landing_on_codex_settles_and_is_logged(choice_world, monkeypatch, tmp_path, origin):  # noqa: F811
    w = choice_world('next')
    stub_up_cli(w, monkeypatch)
    monkeypatch.setattr(cli, 'seat_of', lambda *_args, **_kwargs: w.seat)
    resting = tmp_path / 'resting'
    resting.mkdir()
    w.ship.pools['second'] = replace(w.ship.pool('second'), config_dir=resting)
    save_attempt(w.seat, origin, pool='second')
    assert rf.hold_state(w.seat) is rf.Hold.HELD and not w.seat.is_up

    cli.cmd_up(SimpleNamespace(seat=w.seat.name, fresh=True, pool='first'))

    launch = w.seat.launched()
    assert (launch.pool, launch.harness) == ('first', 'codex') and len(w.starts) == 1
    settled = rf.attempt_for(w.seat)
    assert (settled.phase, settled.pool, settled.destination, settled.origin) == ('released', 'second', launch.at, None)
    assert rf.hold_state(w.seat) is rf.Hold.CLEAR and not rf.status(w.ship, {w.seat.name: w.seat})
    [line] = releases(w)
    assert (line['seat'], line['phase'], line['by']) == (w.seat.name, origin, w.ship.operator)


@pytest.mark.parametrize('origin', ['launched', 'reserved'])
def test_point_1_a_codex_launch_settles_an_undeclared_resting_pool(choice_world, monkeypatch, tmp_path, origin):  # noqa: F811
    w = choice_world('next')
    stub_up_cli(w, monkeypatch)
    monkeypatch.setattr(cli, 'seat_of', lambda *_args, **_kwargs: w.seat)
    resting = tmp_path / 'resting'
    resting.mkdir()
    w.ship.pools['second'] = replace(w.ship.pool('second'), config_dir=resting)
    save_attempt(w.seat, origin, pool='second')
    del w.ship.pools['second']  # the resting subscription has left the ship file while the row is unsettled

    cli.cmd_up(SimpleNamespace(seat=w.seat.name, fresh=True, pool='first'))

    launch = w.seat.launched()
    assert (launch.pool, launch.harness) == ('first', 'codex') and len(w.starts) == 1
    settled = rf.attempt_for(w.seat)
    assert (settled.phase, settled.pool, settled.destination, settled.origin) == ('released', 'second', launch.at, None)
    assert rf.hold_state(w.seat) is rf.Hold.CLEAR
    [line] = releases(w)
    assert (line['seat'], line['phase'], line['by']) == (w.seat.name, origin, w.ship.operator)


@pytest.mark.parametrize('interrupted', [False, True], ids=['first', 'retry-after-kill'])
@pytest.mark.parametrize('resting', ['first', 'alias'], ids=['same-pool', 'alias'])
def test_point_3_a_source_launch_never_becomes_launched(wired, monkeypatch, resting, interrupted):
    w = wired
    w.ship.pools['alias'] = replace(w.ship.pools['first'], name='alias')  # the source runs on the resting sign-in
    _, row = save_attempt(w.seat, 'reserved', pool=resting)
    source = w.seat.launched()
    assert row.source == source.at and rf._lands_on(w.seat, row, source)
    if interrupted:
        killed_up(w, monkeypatch, 'after-launch')
        assert (rf.attempt_for(w.seat).phase, rf.attempt_for(w.seat).origin) == ('releasing', 'reserved')
    assert up(w)[0] == f'{w.seat.name} already up in {source.pane}'
    settled = rf.attempt_for(w.seat)
    assert (settled.phase, settled.destination, settled.origin) == ('released', source.at, None)
    assert rf.hold_state(w.seat) is rf.Hold.CLEAR and w.seat.launched() == source and not w.live.launches
    assert rf._after_up(w.seat, replace(row, phase='launched'), source) == ('released', source.at)


def files_under(folder):
    return {path: path.read_bytes() for path in folder.rglob('*') if path.is_file()}


@pytest.mark.parametrize('caller', [None, 'bridge', 'spock'], ids=['person', 'desk', 'other-seat'])
@pytest.mark.parametrize('phase', ['reserved', *rf.DESK_RELEASES])
def test_point_4_a_refused_pool_on_a_live_seat_writes_nothing(wired, monkeypatch, tmp_path, phase, caller):
    w = wired
    _, row = save_attempt(w.seat, phase, pool='rest')
    if caller:
        monkeypatch.setenv('QD_SEAT', caller)
    lift, disarm = Mock(), Mock()
    monkeypatch.setattr(write_bound, 'lift', lift)
    monkeypatch.setattr(compaction, 'disarm', disarm)
    with lifecycle.claim(w.ship.runtime, w.seat.name):
        pass  # the claim's own empty lock file exists before the count
    launch, saved, files = w.seat.launched(), w.seat._saved_name(), files_under(tmp_path)

    with pytest.raises(ShipError):
        up(w, 'third')

    assert files_under(tmp_path) == files
    assert rf.attempt_for(w.seat) == row and w.seat.launched() == launch and w.seat._saved_name() == saved
    assert rf.hold_state(w.seat) is rf.Hold.HELD and not releases(w) and not w.live.launches
    lift.assert_not_called()
    disarm.assert_not_called()
