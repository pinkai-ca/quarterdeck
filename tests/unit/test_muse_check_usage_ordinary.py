"""Muse check probes with a synthetic usage executable, launch, screen and record."""

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace

import pytest

from quarterdeck import check, check_outcome, cli, launch_opening, pending, post, trusted_files
from quarterdeck.door import Delivery
from quarterdeck.harness import muse_home, muse_screen
from quarterdeck.harness.muse import MuseHarness
from quarterdeck.harness.muse_usage import decode, read_native
from quarterdeck.seat import Launched, Seat
from quarterdeck.ship import Pool, Ship

FIXTURES = Path(__file__).parents[1] / 'fixtures/muse'


def startup_frame(name):
    if name is None:
        return None
    if name.startswith('current-'):
        frame = (FIXTURES / '1.4.25/startup-off.ansi').read_text()
        rows = frame.splitlines(keepends=True)
        if name == 'current-on':
            rows[3] = ('  Contributor mode is on, so your content may be used for product improvement. '
                       'Turn it off in /settings.\n')
        elif name == 'current-altered':
            rows[3] = rows[3].replace('for more information.', 'for further details.')
        elif name == 'current-other':
            rows[3] = '  A different startup notice.\n'
        elif name == 'current-pending':
            del rows[3:5]
        elif name == 'current-unmeasured':
            del rows[3]
        return ''.join(rows)
    fixture = 'idle-off' if name == 'unmeasured-header' else name
    suffix = 'txt' if fixture == 'contributor-on' else 'ansi'
    frame = (FIXTURES / '1.4.20' / f'{fixture}-120x40.{suffix}').read_text()
    return frame.replace(' 1.4.20', ' 1.4.26', 1) if name == 'unmeasured-header' else frame


def assert_startup_evidence(ship, name):
    folder, = (ship.runtime / 'check').glob('*/startup')
    frame = startup_frame(name)
    plain, _ = muse_screen.characters(frame)
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert (folder / 'styled.ansi').read_bytes() == frame.encode()
    assert (folder / 'plain.txt').read_bytes() == plain.encode()


def test_the_explicit_check_usage_runs_the_native_subcommand_and_decodes_buckets(tmp_path):
    program = tmp_path / 'synthetic-usage'
    program.write_text('#!' + sys.executable + '\n' + '''import json, os, sys
from pathlib import Path
Path(os.environ['SYNTHETIC_CALL']).write_text(json.dumps(sys.argv[1:]))
print(json.dumps({'schemaVersion': 1, 'allowance': {
    'window': {'usedPercent': 20, 'resetsAtMs': 1893459600000},
    'weekly': {'usedPercent': 40, 'resetsAtMs': 1894060800000}}}))
''')
    program.chmod(0o700)
    invocation = tmp_path / 'invocation.json'
    usage = read_native(program, tmp_path, {'PATH': os.defpath, 'SYNTHETIC_CALL': str(invocation)})
    assert usage.called is True and usage.error is None
    assert json.loads(invocation.read_text()) == ['usage', '--json']
    assert [(item.name, item.used) for item in usage.windows] == [('window', .2), ('weekly', .4)]


@pytest.fixture
def muse_check(tmp_path, monkeypatch):
    ship = Ship(tmp_path, 'synthetic', 'operator', tmp_path, {'meta': Pool('meta', 'meta')}, {}, {})
    adapter = MuseHarness()
    calls = []
    state = SimpleNamespace(screen=None, frames=[], sampled=[], journal=None, ordinary_readiness=None)
    monkeypatch.setattr(check.harnesses, 'get', lambda name: adapter)
    def version():
        return '1.4.25' if (state.screen or '').startswith('current-') else '1.4.20'

    monkeypatch.setattr(check, '_version_in_runtime', lambda *args: version())
    monkeypatch.setattr(check, 'program_where', lambda *args: None)
    monkeypatch.setattr(cli, 'load_ship', lambda _: ship)
    monkeypatch.setattr(cli.Tmux, 'for_ship', lambda _: object())
    monkeypatch.setattr(adapter, 'check_usage_in_home', lambda *_: decode('{"schemaVersion":1,"allowance":null}'))

    def build_home(home, spec, *_):
        muse_home.prepare(home)
        muse_home.write_settings(home, muse_home.configuration(spec.model, spec.effort, {}, {}))
        muse_home.remember_off(home)
        if version() == '1.4.25':
            settings_path = home / 'config/muse/settings.json'
            settings = json.loads(settings_path.read_text())
            settings['contributor_mode_disclosure_acknowledged'] = True
            settings_path.write_text(json.dumps(settings))

    monkeypatch.setattr(adapter, 'build_home', build_home)
    monkeypatch.setattr(pending, 'current_identity', lambda seat: launch_opening.binding(seat, seat.launched()))
    @contextmanager
    def temporary(ship, tmux, spec, orders, purpose, *, minimal_opening=None, **kwargs):
        folder = ship.runtime / 'check' / spec.name
        home, private = folder / 'home', folder / 'trusted'
        task = folder / 'task'
        task.mkdir(parents=True)
        spec = replace(spec, cwd=task)
        native_record = folder / 'synthetic-session' / 'session.jsonl'
        seat = SimpleNamespace(ship=ship, tmux=tmux, spec=spec, name=spec.name, harness=adapter, pool=ship.pool('meta'),
                               folder=folder, home=home, trusted=private, defer_opening=False, is_up=True)
        seat.launched = lambda: Seat.launched(seat)
        seat.record = lambda: native_record if native_record.exists() else None

        def read_pane():
            name = state.frames.pop(0) if state.frames else state.screen
            state.sampled.append(name)
            frame = startup_frame(name)
            return (SimpleNamespace(id='%123', pid=1), frame) if frame is not None else None

        seat.read_pane = read_pane

        def up(*, resume):
            assert resume is False and spec.model == adapter.check_model
            build_home(home, spec)
            launch = Launched('%123', 1, datetime.now(timezone.utc).isoformat(), 'meta', None, ['synthetic'], version())
            opening = minimal_opening or 'synthetic standard opening'
            launch_opening.prepare(seat, launch, opening)
            launch.write(private / trusted_files.LAUNCH)
            if state.screen == 'idle-after-turn':
                state.ordinary_readiness = adapter.opening_readiness(home, check.Door(seat).screen())[0]
            try:
                Seat._deliver_opening(seat, launch)
            finally:
                state.journal = json.loads(launch_opening.path_for(seat, launch).read_text())
            return 'synthetic launch'

        def deliver(door, text, *, verify, confirm):
            assert verify() and confirm
            assert 'positive off line on this launch' in door.opening_owner.data['startup_proof']
            calls.append(text)
            if minimal_opening is None:
                return Delivery('withheld', 'synthetic hold before the full task')
            (task / 'proof.txt').write_text('OK\n')
            native_record.parent.mkdir()
            events = [{'kind': 'started', 'prompt': text}, {'kind': 'model_completed', 'model': spec.model},
                      {'kind': 'terminal', 'terminal': 'completed'}]
            native_record.write_text(''.join(json.dumps(dict(
                schema_version=1, payload_schema_version=1, id=f'synthetic-{i}', sequence=i,
                recorded_at=1893456000000000 + i * 1000000, stream={'kind': 'session', 'id': native_record.parent.name},
                payload_type='runtime.session', payload={'kind': 'run', 'run_id': 'synthetic-run', 'event': event},
            )) + '\n' for i, event in enumerate(events)))
            return Delivery('delivered', 'synthetic record receipt')

        seat.up = up
        monkeypatch.setattr(check.Door, 'deliver', deliver)
        yield seat

    monkeypatch.setattr(check, 'temporary_seat', temporary)
    return ship, adapter, calls, state


@pytest.mark.parametrize('minimal', [False, True])
@pytest.mark.parametrize('screen,reason', [('contributor-on', 'launch-setting-on'),
                                           ('idle-after-turn', 'launch-setting-unreadable'),
                                           ('unmeasured-header', 'launch-setting-unreadable'),
                                           ('current-on', 'launch-setting-unreadable'),
                                           ('current-altered', 'launch-setting-unreadable'),
                                           ('current-other', 'launch-setting-unreadable'),
                                           ('current-unmeasured', 'launch-setting-unreadable'),
                                           (None, 'launch-setting-unreadable')])
def test_a_check_without_current_contributor_off_never_delivers_its_opening(muse_check, minimal, screen, reason, monkeypatch):
    ship, _, calls, state = muse_check
    state.screen = screen
    times = iter([0, 0, 999] if screen is None else [0, 0])
    monkeypatch.setattr(launch_opening, 'time', SimpleNamespace(monotonic=lambda: next(times), sleep=lambda _: None))
    args = SimpleNamespace(harness='muse', minimal=minimal)
    lines = cli.cmd_check(args)
    assert args.exit_code == 1 and not calls and post.Store(ship.runtime).all() == []
    contributor = next(line for line in lines if 'Muse contributor mode' in line)
    assert contributor.startswith('FAIL') and 'off' not in contributor
    assert state.journal['phase'] == 'held'
    if screen is None:
        assert not list((ship.runtime / 'check').glob('*/startup'))
    else:
        assert_startup_evidence(ship, screen)
    if screen == 'idle-after-turn':
        assert state.ordinary_readiness is True
    runtime = ship.runtime / 'minimal-checks' if minimal else ship.runtime
    outcome = check_outcome.read(runtime, 'muse')
    saved = outcome['definitive']
    assert saved['reason'] == reason and saved['step'] == 'Muse contributor mode'
    assert 'Muse contributor mode' in check_outcome.alarms([outcome])[0]


@pytest.mark.parametrize('minimal', [False, True])
@pytest.mark.parametrize('screen', ['idle-off', 'current-off'])
def test_a_check_reads_positive_startup_contributor_evidence_before_the_opening(muse_check, minimal, screen):
    ship, adapter, calls, state = muse_check
    state.screen = screen
    args = SimpleNamespace(harness='muse', minimal=minimal)
    lines = cli.cmd_check(args)
    assert 'PASS  Muse contributor mode · off in this launch\'s startup screen before typing' in lines
    assert_startup_evidence(ship, screen)
    if minimal:
        assert args.exit_code == 0 and calls == [adapter.minimal_check_task] and state.journal['phase'] == 'delivered'
        saved = check_outcome.read(ship.runtime / 'minimal-checks', 'muse')['definitive']
        assert saved['usage'] == {'before': 'unknown', 'after': 'unknown', 'share_spent': 'unknown'}
        assert any('requested model in record' in line and line.startswith('PASS') for line in lines)
    else:
        assert args.exit_code == 1 and calls == ['synthetic standard opening'] and state.journal['phase'] == 'held'
    assert post.Store(ship.runtime).all() == []


@pytest.mark.parametrize('settled', ['current-off', 'current-on', 'current-altered', 'current-pending'])
def test_a_measured_pending_frame_waits_for_the_next_verdict_or_the_deadline(muse_check, settled, monkeypatch):
    ship, adapter, calls, state = muse_check
    state.screen = 'current-other'  # a later frame cannot replace the verdict's evidence
    state.frames = ['current-pending', settled]
    times = iter([0, 0, 0.05, 999] if settled == 'current-pending' else [0, 0, 0.05])
    sleeps = []

    def wait(seconds):
        assert not calls
        sleeps.append(seconds)

    monkeypatch.setattr(launch_opening, 'time', SimpleNamespace(monotonic=lambda: next(times), sleep=wait))
    args = SimpleNamespace(harness='muse', minimal=True)
    lines = cli.cmd_check(args)
    assert state.sampled[:2] == ['current-pending', settled]
    assert_startup_evidence(ship, settled)
    assert sleeps == [0.05] * (2 if settled == 'current-pending' else 1)
    if settled == 'current-off':
        assert args.exit_code == 0 and calls == [adapter.minimal_check_task]
        assert state.journal['phase'] == 'delivered'
        assert state.sampled[2:] == ['current-other']
    else:
        assert args.exit_code == 1 and not calls and state.journal['phase'] == 'held'
        assert any(line.startswith('FAIL  Muse contributor mode · unknown;') for line in lines)
        saved = check_outcome.read(ship.runtime / 'minimal-checks', 'muse')['definitive']
        assert saved['reason'] == 'launch-setting-unreadable'


def test_the_pending_frame_grants_no_ordinary_opening_authority(tmp_path):
    adapter = MuseHarness()
    muse_home.prepare(tmp_path)
    muse_home.write_settings(tmp_path, muse_home.configuration(adapter.check_model, adapter.check_effort, {}, {}))
    muse_home.remember_off(tmp_path)
    screen = adapter.screen(startup_frame('current-pending'))
    assert adapter.check_launch_reading(screen)[1] is None
    assert not adapter.opening_readiness(tmp_path, screen)[0]
    assert adapter.delivery_obstruction(None, screen)
