"""Adapter-owned effective config, off/restore selection and launch-format boundaries."""

import ast
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest

from quarterdeck import endpoint
from quarterdeck.harness import get
from quarterdeck.harness.types import CappedGeneration
from quarterdeck.seat import Launched, Seat
from quarterdeck.ship import Pool, SeatSpec, ShipError


@pytest.fixture(params=['opencode', 'pi'])
def native(tmp_path, monkeypatch, request):
    monkeypatch.setattr(endpoint, 'models', lambda *a, **k: [endpoint.Served('qwen3.8-27b', 98304)])
    monkeypatch.delenv('OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX', raising=False)
    monkeypatch.delenv('OPENCODE_EXPERIMENTAL_NATIVE_LLM', raising=False)
    name = request.param
    settings = {'theme': 'dark'} if name == 'pi' else {'provider': {'synthetic': {'models': {'qwen3.8-27b': {
        'options': {'chat_template_kwargs': {'enable_thinking': True, 'preserve_thinking': True}}}}}}}
    spec = SeatSpec('worker', 'crew', 'worker', name, ('synthetic',), model='qwen3.8-27b',
                    effort='high' if name == 'pi' else None, settings=settings)
    pool = Pool('synthetic', 'local', endpoint='http://example.invalid/v1')
    harness = get(name)
    home = tmp_path / 'runtime/seats/worker/home'
    harness.build_home(home, spec, pool, None)
    launch = harness.launch(spec, pool, home, tmp_path / 'brief', None, None)
    version = '0.74.2' if name == 'pi' else '1.19.5'
    return SimpleNamespace(name=name, harness=harness, spec=spec, pool=pool, home=home, launch=launch, version=version)


def client(native):
    return native.harness.thinking_recovery(native.home, native.launch.recovery, native.version, native.spec, native.pool)


def test_only_the_proved_native_budget_contract_is_eligible(native):
    found = client(native)
    assert found is not None
    reason, written = (None, 8192) if native.name == 'pi' else (8192, 0)
    cap = CappedGeneration('root', 'original', 'parent', 'generation', 'qwen3.8-27b', 'synthetic', reason, written, 'a' * 64)
    assert found.eligible(cap)
    assert not found.eligible(replace(cap, written=written - 2 if native.name == 'pi' else 2))
    assert not found.eligible(replace(cap, provider='other'))
    if native.name == 'opencode':
        assert found.eligible(replace(cap, reasoning=8191, written=1))
        assert not found.eligible(replace(cap, reasoning=8190, written=2))
        assert native.launch.env['OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX'] == '8192'
        assert native.launch.recovery['environment']['OPENCODE_EXPERIMENTAL_NATIVE_LLM'] == '0'
    else:
        assert not found.eligible(replace(cap, reasoning=8192)), 'Pi cannot invent a separated native reasoning count'
        assert found.off_keys == ('BTab',) and found.restore_keys == ('BTab',) * 4


def test_missing_changed_or_unsupported_launch_sources_never_authorize_controls(native):
    assert native.harness.thinking_recovery(native.home, None, native.version, native.spec, native.pool) is None
    assert native.harness.thinking_recovery(native.home, native.launch.recovery, 'future', native.spec, native.pool) is None
    assert native.harness.thinking_recovery(native.home, native.launch.recovery, native.version,
        replace(native.spec, settings={}), native.pool) is None
    config = native.home / next(iter(native.launch.recovery['configuration']))
    config.write_text(config.read_text() + '\n')
    assert client(native) is None


@pytest.mark.parametrize('alias', ['max_tokens', 'maxOutputTokens'])
def test_opencode_preserves_unproved_budget_overrides_without_claiming_recovery(tmp_path, monkeypatch, alias):
    monkeypatch.setattr(endpoint, 'models', lambda *a, **k: [endpoint.Served('qwen3.8-27b', 98304)])
    pool = Pool('synthetic', 'local', endpoint='http://example.invalid/v1')
    spec = SeatSpec('worker', 'crew', 'worker', 'opencode', ('synthetic',), model='qwen3.8-27b', settings={
        'provider': {'synthetic': {'models': {'qwen3.8-27b': {'options': {
            alias: 123, 'chat_template_kwargs': {'enable_thinking': True}}}}}}})
    harness = get('opencode')
    home = tmp_path / 'runtime/seats/worker/home'
    harness.build_home(home, spec, pool, None)
    config = json.loads((home / 'config/opencode/opencode.json').read_text())
    assert config['provider']['synthetic']['models']['qwen3.8-27b']['options'][alias] == 123
    assert harness.launch(spec, pool, home, tmp_path / 'brief', None, None).recovery is None
    with pytest.raises(ShipError, match='conflicts with native request-budget options'):
        harness.build_home(home, spec, replace(pool, output=16384), None)


def test_pi_explicit_off_has_the_proved_native_compatibility_without_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(endpoint, 'models', lambda *a, **k: [endpoint.Served('qwen3.8-27b', 98304)])
    pool = Pool('synthetic', 'local', endpoint='http://example.invalid/v1')
    spec = SeatSpec('worker', 'crew', 'worker', 'pi', ('synthetic',), model='qwen3.8-27b', effort='off')
    harness = get('pi')
    home = tmp_path / 'runtime/seats/worker/home'
    harness.build_home(home, spec, pool, None)
    config = json.loads((home / 'agent/models.json').read_text())
    assert config['providers']['synthetic']['models'][0]['compat']['thinkingFormat'] == 'qwen-chat-template'
    launch = harness.launch(spec, pool, home, tmp_path / 'brief', None, None)
    assert ('--thinking', 'off') == launch.argv[-2:] and launch.recovery is None


def test_human_context_retains_native_defaults_and_gets_no_recovery_variant(native):
    home = native.home.parent / 'human-home'
    spec = replace(native.spec, human_session=True)
    native.harness.build_home(home, spec, native.pool, None)
    launch = native.harness.launch(spec, native.pool, home, home / 'brief', None, None)
    assert launch.recovery is None
    if native.name == 'opencode':
        config = json.loads((home / 'config/opencode/opencode.json').read_text())
        assert 'variants' not in config['provider']['synthetic']['models']['qwen3.8-27b']
        assert 'OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX' not in launch.env
    else:
        config = json.loads((home / 'agent/models.json').read_text())
        assert 'compat' not in config['providers']['synthetic']['models'][0]


def test_exact_original_and_off_state_require_agreeing_native_evidence(native):
    found = client(native)
    state = SimpleNamespace(model='qwen3.8-27b', effort='high')
    if native.name == 'opencode':
        screen = SimpleNamespace(text='  ┃  Build auto · qwen3.8-27b synthetic\n')
        assert found.mode(state, screen) == 'original'
        path = native.home / 'state/opencode/model.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'variant': {'synthetic/qwen3.8-27b': 'qd-recovery-off'}}))
        assert found.mode(state, screen) is None, 'a UI label alone cannot prove the actual selected variant'
        screen.text = '  ┃  Build auto · qwen3.8-27b synthetic · qd-recovery-off\n'
        assert found.mode(state, screen) == 'off'
        path.write_text(json.dumps({'variant': {'synthetic/qwen3.8-27b': 'custom'}}))
        assert found.mode(state, screen) is None
        # A seat with a working folder: OpenCode shows it at the right of the same line, after the native padding.
        # Measured on two hands' panes, 478 columns wide, on 16 November 2026: two spaces, the bar, two spaces, the
        # status, 401 spaces, the folder ('hands/' and the hand's name) and 29 spaces.
        folder = ' ' * 401 + 'hands/synthetic' + ' ' * 29 + '\n'
        path.write_text(json.dumps({'variant': {'synthetic/qwen3.8-27b': 'default'}}))
        screen.text = '  ┃  Build auto · qwen3.8-27b synthetic' + folder
        assert found.mode(state, screen) == 'original'
        # The same pane on a fresh start: the box centred, 202 spaces in, and no folder on the line.
        screen.text = ' ' * 202 + '┃  Build auto · qwen3.8-27b synthetic\n'
        assert found.mode(state, screen) == 'original'
        screen.text = '  ┃  Build auto · qwen3.8-27b synthetic · qd-recovery-off' + folder
        assert found.mode(state, screen) is None, 'the variant shown still has to agree with the one saved'
        path.write_text(json.dumps({'variant': {'synthetic/qwen3.8-27b': 'qd-recovery-off'}}))
        assert found.mode(state, screen) == 'off'
        screen.text = '  ┃  Build auto · qwen3.8-27b synthetic · custom' + folder
        assert found.mode(state, screen) is None, 'a variant it does not know is never read as a folder'
        screen.text = '  ┃  Build auto · qwen3.8-27b synthetic  · qd-recovery-off' + folder
        assert found.mode(state, screen) is None, 'a variant is never taken for the folder field'
    else:
        screen = SimpleNamespace(shows={'model': 'qwen3.8-27b', 'effort': 'high'})
        assert found.mode(state, screen) == 'original'
        settings = native.home / 'agent/settings.json'
        data = json.loads(settings.read_text())
        settings.write_text(json.dumps({**data, 'defaultThinkingLevel': 'off', 'lastChangelogVersion': '0.74.2'}))
        state.effort = 'off'
        assert found.mode(state, screen) is None
        screen.shows['effort'] = 'off'
        assert found.mode(state, screen) == 'off'
        assert client(native) is not None, 'native level/changelog writes alone do not change other request options'
        settings.write_text(json.dumps({**data, 'theme': 'light', 'defaultThinkingLevel': 'off'}))
        assert client(native) is None, 'unrelated native setting changes must refuse restoration'


def test_pi_native_off_footer_is_known_without_accepting_an_unknown_effort_dialog():
    from quarterdeck.harness.pi import PiHarness
    text = '\n'.join(['─' * 80, '', '─' * 80, '/synthetic',
                      '0.1%/98k (auto)                  qwen3.8-27b • thinking off'])
    screen = PiHarness().screen(text)
    assert screen.box == '' and screen.unknown is None and screen.shows == {'model': 'qwen3.8-27b', 'effort': 'off'}
    assert PiHarness().screen(text.replace('thinking off', 'thinking future')).unknown is not None


@pytest.mark.parametrize('token', ['1.19.5', '1.19.5-beta.1', '1.19.5.1', '0.74.2-beta.1', '0.74.2.1'])
def test_native_version_token_is_not_truncated_into_a_supported_client(monkeypatch, token):
    from quarterdeck.harness import base
    monkeypatch.setattr(base.subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout='Native ' + token + '\n', stderr='9.9.9\n'))
    assert base.program_version(('synthetic', '--version')) == token


@pytest.mark.parametrize('suffix', ['-beta.1', '.1'])
def test_adapter_gate_rejects_the_actual_complete_prerelease_or_extended_token(native, suffix):
    assert native.harness.thinking_recovery(native.home, native.launch.recovery, native.version + suffix,
                                           native.spec, native.pool) is None


def test_old_launch_reader_rejects_the_extension_and_new_reader_accepts_missing_evidence(tmp_path, monkeypatch):
    # Execute only the exact parent's small data class and launched() accessor,
    # not its framework or any runtime. Pin the actual stored-format consequence.
    source = subprocess.check_output(['git', 'show', 'a6d1474:src/quarterdeck/seat.py'], text=True)
    tree = ast.parse(source)
    old_class = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Launched')
    seat_class = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Seat')
    old_read = next(n for n in seat_class.body if isinstance(n, ast.FunctionDef) and n.name == 'launched')
    old_read.name = 'old_read'
    module = ModuleType('synthetic_old_launch_reader')
    monkeypatch.setitem(sys.modules, module.__name__, module)
    module.__dict__.update(dataclass=dataclass, field=field, asdict=asdict, json=json, datetime=datetime,
                           Path=Path, saved_price=lambda path: None)
    exec('from __future__ import annotations\n' + ast.unparse(old_class) + '\n' + ast.unparse(old_read), module.__dict__)
    path = tmp_path / 'launch.json'
    old = dict(pane='%1', pid=10, at='2030-01-01T00:00:00+00:00', pool='synthetic', resume=None,
               argv=['opencode'], version='1.19.5', harness='opencode', records_before=[])
    path.write_text(json.dumps(old))
    holder = SimpleNamespace(folder=tmp_path, trusted=tmp_path)
    assert module.old_read(holder).pane == '%1'
    assert Seat.launched(holder).recovery is None
    path.write_text(json.dumps({**old, 'recovery': {'format': 1}}))
    with pytest.raises(TypeError, match="unexpected keyword argument 'recovery'"):
        module.old_read(holder)
    with pytest.raises(ValueError, match='invalid native thinking-recovery launch evidence'):
        Seat.launched(holder)
