"""Cached opt-out seeding with synthetic linked sign-ins; no native program or account calls."""
import json
from datetime import datetime, timezone
import tomllib

import pytest

from quarterdeck import watch
from quarterdeck.harness import grok
from quarterdeck.harness.limits import Usage
from quarterdeck.ledger import Count
from quarterdeck.seat import Launched
from quarterdeck.ship import Pool, SeatSpec


@pytest.fixture
def setup_home(tmp_path, monkeypatch):
    pool_home = tmp_path / 'synthetic-sign-in'
    pool_home.mkdir()
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    brief = tmp_path / 'brief.md'
    brief.write_text('Synthetic startup fixture.\n')
    program = tmp_path / 'synthetic-grok'
    program.write_text('Synthetic selected program; never executed.\n')
    pool = Pool('example-subscription', 'xai', config_dir=pool_home)
    adapter = grok.GrokHarness()
    monkeypatch.setattr(adapter, 'preflight', lambda *args: None)
    monkeypatch.setattr(adapter, 'selected_program', lambda: program)
    monkeypatch.setattr(adapter, 'workspace_notice', lambda *args: 'workspace example')
    monkeypatch.setattr(grok.grok_workspace, 'launch_args', lambda cwd: ('--trust',))
    def build(payload, *, name='crew-example', human=False, raw=False):
        if payload != 'missing-file':
            (pool_home / 'auth.json').write_text(payload if raw else json.dumps(payload))
        home = tmp_path / name
        spec = SeatSpec(name, name, 'worker', 'grok', (pool.name,), cwd=workspace,
                        sandbox=False, human_session=human)
        adapter.build_home(home, spec, pool, None)
        return adapter, pool, home, spec, brief
    return build


def launch(adapter, pool, home, spec, brief):
    return adapter.launch(spec, pool, home, brief, None, None)


@pytest.mark.parametrize(('name', 'human'), [('crew-example', False), ('human-example', True), ('check-example', False)])
def test_true_cached_opt_out_seeds_before_launch_and_names_its_evidence(setup_home, name, human, capsys):
    args = setup_home({'synthetic-account': {'coding_data_retention_opt_out': True,
                                           'unrelated': 'synthetic-private-value'}}, name=name, human=human)
    adapter, pool, home, spec, brief = args
    assert (home / 'auth.json').is_symlink()
    selected = launch(*args)
    privacy = tomllib.loads((home / 'config.toml').read_text())['privacy']
    at = datetime.fromisoformat(privacy['privacy_banner_acked'])
    assert at.tzinfo is not None
    assert set(privacy) == {'privacy_banner_acked'}
    assert selected.notice == 'workspace example; ' + grok.PRIVACY_ACK_NOTICE
    assert 'synthetic-private-value' not in selected.notice
    assert 'synthetic-account' not in selected.notice
    assert capsys.readouterr() == ('', '')
    # Restart/resume command construction preserves the same acknowledgement.
    launch(*args)
    assert tomllib.loads((home / 'config.toml').read_text())['privacy'] == privacy


@pytest.mark.parametrize('payload', [
    {'synthetic-account': {'coding_data_retention_opt_out': False}},
    {'synthetic-account': {}},
    {'synthetic-account': {'coding_data_retention_opt_out': None}},
    {'synthetic-account': {'coding_data_retention_opt_out': 'true'}},
    {'synthetic-account': {'coding_data_retention_opt_out': 1}},
    {'first-example': {'coding_data_retention_opt_out': True}, 'second-example': {}},
    [],
    'missing-file',
])
def test_no_positive_boolean_leaves_the_native_question_unacknowledged(setup_home, payload, capsys):
    args = setup_home(payload)
    selected = launch(*args)
    assert 'privacy' not in tomllib.loads((args[2] / 'config.toml').read_text())
    assert selected.notice == 'workspace example'
    assert capsys.readouterr() == ('', '')


def test_unreadable_stub_sign_in_seeds_nothing_and_logs_no_file_content(setup_home, capsys):
    args = setup_home('{ synthetic-unreadable-account-text', raw=True)
    selected = launch(*args)
    assert 'privacy' not in tomllib.loads((args[2] / 'config.toml').read_text())
    assert selected.notice == 'workspace example'
    assert capsys.readouterr() == ('', '')


def test_native_usage_home_uses_the_same_conditional_acknowledgement(setup_home, monkeypatch):
    adapter, pool, home, spec, brief = setup_home({'synthetic-account': {'coding_data_retention_opt_out': True}})
    observed = []
    def billing(program, used_home, env):
        observed.append(tomllib.loads((used_home / 'config.toml').read_text())['privacy'])
        return Usage((), datetime.now(timezone.utc))
    monkeypatch.setattr(grok.grok_usage, 'billing', billing)
    adapter.usage_in_home(pool, home)
    assert len(observed) == 1
    assert set(observed[0]) == {'privacy_banner_acked'}


def test_launch_record_and_watch_show_cached_evidence_without_calling_it_a_press(setup_home, tmp_path):
    selected = launch(*setup_home({'synthetic-account': {'coding_data_retention_opt_out': True}}))
    path = tmp_path / 'launch.json'
    recorded = Launched('synthetic-pane', 12345, '2040-01-01T00:00:00+00:00',
                        'example-subscription', None, list(selected.argv), 'synthetic-version',
                        'grok', notice=selected.notice)
    recorded.write(path)
    saved = json.loads(path.read_text())
    assert saved['notice'] == selected.notice
    assert 'answered' not in saved
    reading = watch.SeatReading('example', watch.Activity('idle'), 'open', None, Count(),
                                'example-subscription', None, 'grok')
    data = {'seat': reading, 'record': None, 'state': None, 'record_error': None,
            'record_absent': None, 'record_limits': None, 'screen': None, 'usage': [],
            'answered': [], 'launch_notice': saved['notice']}
    shown = '\n'.join(watch.render_full(data))
    assert grok.PRIVACY_ACK_NOTICE in shown
    assert 'pressed at' not in shown


@pytest.mark.parametrize('replacement', [
    {'synthetic-account': {'coding_data_retention_opt_out': False}},
    {'synthetic-account': {}},
    '{ unreadable synthetic sign-in',
])
def test_rebuild_drops_a_framework_seed_when_the_flag_no_longer_reads_true(setup_home, replacement):
    args = setup_home({'synthetic-account': {'coding_data_retention_opt_out': True}})
    adapter, pool, home, spec, brief = args
    assert grok.PRIVACY_ACK_NOTICE in launch(*args).notice
    marker = home / grok.PRIVACY_SEED_RECORD
    assert json.loads(marker.read_text())['writer'] == 'quarterdeck'
    source = pool.config_dir / 'auth.json'
    source.write_text(replacement if isinstance(replacement, str) else json.dumps(replacement))
    adapter.build_home(home, spec, pool, None)
    assert 'privacy' not in tomllib.loads((home / 'config.toml').read_text())
    assert not marker.exists()
    assert launch(*args).notice == 'workspace example'


def test_launch_rechecks_and_removes_its_previous_seed(setup_home):
    args = setup_home({'synthetic-account': {'coding_data_retention_opt_out': True}})
    adapter, pool, home, spec, brief = args
    assert grok.PRIVACY_ACK_NOTICE in launch(*args).notice
    (pool.config_dir / 'auth.json').write_text(json.dumps(
        {'synthetic-account': {'coding_data_retention_opt_out': False}}))
    assert launch(*args).notice == 'workspace example'
    assert 'privacy' not in tomllib.loads((home / 'config.toml').read_text())
    assert not (home / grok.PRIVACY_SEED_RECORD).exists()


@pytest.mark.parametrize('opted_out', [False, True])
def test_program_press_acknowledgement_without_seed_record_survives_rebuild(setup_home, opted_out):
    args = setup_home({'synthetic-account': {'coding_data_retention_opt_out': False}})
    adapter, pool, home, spec, brief = args
    path = home / 'config.toml'
    config = tomllib.loads(path.read_text())
    native = {'privacy_banner_acked': '2040-01-01T00:00:00+00:00'}
    config['privacy'] = native
    path.write_text(grok.config_text(config))  # the state left by a native opt-out press
    assert not (home / grok.PRIVACY_SEED_RECORD).exists()
    (pool.config_dir / 'auth.json').write_text(json.dumps(
        {'synthetic-account': {'coding_data_retention_opt_out': opted_out}}))
    adapter.build_home(home, spec, pool, None)
    assert tomllib.loads(path.read_text())['privacy'] == native
    assert launch(*args).notice == 'workspace example'
    assert not (home / grok.PRIVACY_SEED_RECORD).exists()
