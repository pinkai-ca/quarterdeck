"""Muse check probes with a synthetic usage executable, launch, screen and record."""

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from quarterdeck import check, check_outcome, cli, launch_opening, pending, post, trusted_files
from quarterdeck.door import Delivery
from quarterdeck.harness import muse_home
from quarterdeck.harness.muse import MuseHarness
from quarterdeck.harness.muse_usage import decode, read_native
from quarterdeck.seat import Launched, Seat
from quarterdeck.ship import Pool, Ship


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
    fixture = Path(__file__).parents[1] / 'fixtures/muse/1.4.20'
    calls = []
    state = SimpleNamespace(screen=None, journal=None, ordinary_readiness=None)
    monkeypatch.setattr(check.harnesses, 'get', lambda name: adapter)
    monkeypatch.setattr(check, '_version_in_runtime', lambda *args: '1.4.20')
    monkeypatch.setattr(check, 'program_where', lambda *args: None)
    monkeypatch.setattr(cli, 'load_ship', lambda _: ship)
    monkeypatch.setattr(cli.Tmux, 'for_ship', lambda _: object())
    monkeypatch.setattr(adapter, 'check_usage_in_home', lambda *_: decode('{"schemaVersion":1,"allowance":null}'))

    def build_home(home, spec, *_):
        muse_home.prepare(home)
        muse_home.write_settings(home, muse_home.configuration(spec.model, spec.effort, {}, {}))
        muse_home.remember_off(home)

    monkeypatch.setattr(adapter, 'build_home', build_home)
    monkeypatch.setattr(pending, 'current_identity', lambda seat: launch_opening.binding(seat, seat.launched()))
    # Keep the real screen parser, opening owner and record reader. The test
    # transport supplies only one startup frame and the ordered task receipt.
    def screen(_door):
        if not state.screen:
            return None
        name = 'idle-off' if state.screen == 'unmeasured-header' else state.screen
        suffix = 'txt' if name == 'contributor-on' else 'ansi'
        frame = (fixture / f'{name}-120x40.{suffix}').read_text()
        if state.screen == 'unmeasured-header':
            frame = frame.replace(' 1.4.20', ' 1.4.25', 1)
        return adapter.screen(frame)

    monkeypatch.setattr(check.Door, 'screen', screen)

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

        def up(*, resume):
            assert resume is False and spec.model == adapter.check_model
            build_home(home, spec)
            launch = Launched('%123', 1, datetime.now(timezone.utc).isoformat(), 'meta', None, ['synthetic'], '1.4.20')
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
                                           (None, 'launch-setting-unreadable')])
def test_a_check_without_current_contributor_off_never_delivers_its_opening(muse_check, minimal, screen, reason, monkeypatch):
    ship, _, calls, state = muse_check
    state.screen = screen
    if screen is None:
        times = iter([0, 0, 999])
        monkeypatch.setattr(launch_opening, 'time', SimpleNamespace(monotonic=lambda: next(times), sleep=lambda _: None))
    args = SimpleNamespace(harness='muse', minimal=minimal)
    lines = cli.cmd_check(args)
    assert args.exit_code == 1 and not calls and post.Store(ship.runtime).all() == []
    contributor = next(line for line in lines if 'Muse contributor mode' in line)
    assert contributor.startswith('FAIL') and 'off' not in contributor
    assert state.journal['phase'] == 'held'
    if screen == 'idle-after-turn':
        assert state.ordinary_readiness is True
    runtime = ship.runtime / 'minimal-checks' if minimal else ship.runtime
    outcome = check_outcome.read(runtime, 'muse')
    saved = outcome['definitive']
    assert saved['reason'] == reason and saved['step'] == 'Muse contributor mode'
    assert 'Muse contributor mode' in check_outcome.alarms([outcome])[0]


@pytest.mark.parametrize('minimal', [False, True])
def test_a_check_reads_positive_startup_contributor_evidence_before_the_opening(muse_check, minimal):
    ship, adapter, calls, state = muse_check
    state.screen = 'idle-off'
    args = SimpleNamespace(harness='muse', minimal=minimal)
    lines = cli.cmd_check(args)
    assert 'PASS  Muse contributor mode · off in this launch\'s startup screen before typing' in lines
    if minimal:
        assert args.exit_code == 0 and calls == [adapter.minimal_check_task] and state.journal['phase'] == 'delivered'
        saved = check_outcome.read(ship.runtime / 'minimal-checks', 'muse')['definitive']
        assert saved['usage'] == {'before': 'unknown', 'after': 'unknown', 'share_spent': 'unknown'}
        assert any('requested model in record' in line and line.startswith('PASS') for line in lines)
    else:
        assert args.exit_code == 1 and calls == ['synthetic standard opening'] and state.journal['phase'] == 'held'
    assert post.Store(ship.runtime).all() == []
