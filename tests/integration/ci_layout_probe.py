"""Ordinary real-checkout probe, selected explicitly by a focused ``run --ci-layout``.

The filename deliberately keeps this mode-specific probe out of ordinary/full collection. It verifies
the running checkout, not a copied fixture; the wrapper's retained result accounts for its final cleanup.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import shutil

import quarterdeck

ROOT = Path(__file__).resolve().parents[2]


def git(folder, *args, check=True):
    return subprocess.run(['git', '-C', str(folder), *args], check=check, capture_output=True, text=True)


def run_state():
    scratch = Path(os.environ['TESTRUN_EVIDENCE']).parent
    return scratch, json.loads((scratch / 'state.json').read_text())


def test_real_checkout_identity_import_and_complete_history():
    scratch, state = run_state()
    result = json.loads((scratch.parents[1] / 'results' / state['run'] / 'result.json').read_text())
    assert ROOT == scratch / 'checkout'
    assert (ROOT / '.git').is_dir() and not (ROOT / '.git').is_symlink()
    assert git(ROOT, 'rev-parse', '--path-format=absolute', '--git-common-dir').stdout.strip() == str(ROOT / '.git')
    assert not (ROOT / '.git/objects/info/alternates').exists()
    assert git(ROOT, 'rev-parse', 'HEAD').stdout.strip() == result['target']['head'] == state['head']
    assert git(ROOT, 'rev-parse', 'HEAD^{tree}').stdout.strip() == result['target']['tree']
    assert git(ROOT, 'rev-parse', '--is-shallow-repository').stdout.strip() == 'false'
    assert sorted(git(ROOT, 'rev-list', 'HEAD').stdout.splitlines()) == sorted(
        git(state['target'], 'rev-list', state['head']).stdout.splitlines())
    module = Path(quarterdeck.__file__).resolve()
    assert module.is_relative_to(ROOT / 'src')
    assert module.read_text() == git(ROOT, 'show', f'HEAD:{module.relative_to(ROOT)}').stdout
    assert Path(sys.prefix) == ROOT / '.venv'
    assert Path(shutil.which('python')) == ROOT / '.venv' / 'bin' / 'python'
    assert Path(shutil.which('qd')) == ROOT / '.venv' / 'bin' / 'qd'


def test_ordinary_temp_and_nested_git_are_inside_disposable_checkout(tmp_path):
    expected = ROOT / '.testruns' / 't'
    assert Path(os.environ['TMPDIR']) == Path(tempfile.gettempdir()) == expected
    assert tmp_path.resolve().is_relative_to(expected)
    nested = tmp_path / 'uninitialised-ship'
    nested.mkdir()
    declaration = nested / 'ship.toml'
    declaration.write_text('# synthetic declaration\n')
    assert git(nested, 'rev-parse', '--show-toplevel').stdout.strip() == str(ROOT)
    assert git(nested, 'rev-parse', '--path-format=absolute', '--git-common-dir').stdout.strip() == str(ROOT / '.git')
    assert git(ROOT, 'check-ignore', '--', str(declaration)).returncode == 0
    # This is the actual ignored-path behavior CI must exercise, without staging any tracked file.
    assert git(nested, 'add', '--dry-run', '--', 'ship.toml', check=False).returncode != 0
    assert not git(ROOT, 'ls-files', '--', str(declaration)).stdout


def test_descendants_have_plain_environment_and_owned_workflow_identity():
    expected = ROOT / '.testruns' / 't'
    code = ('import json, os, tempfile; '
            'f = tempfile.TemporaryFile(); '
            'print(json.dumps({"present": "TESTRUNS_RUN" in os.environ, '
            '"tmpdir": os.environ.get("TMPDIR"), "tempdir": tempfile.gettempdir()})); f.close()')
    child = subprocess.run([sys.executable, '-c', code], check=True, capture_output=True, text=True)
    assert json.loads(child.stdout) == {'present': False, 'tmpdir': str(expected), 'tempdir': str(expected)}
    assert 'TESTRUNS_RUN' not in os.environ
    assert git(ROOT, 'config', '--global', 'user.name').stdout.strip() == 'Quarterdeck CI'
    assert git(ROOT, 'config', '--global', 'user.email').stdout.strip() == 'ci@example.invalid'
    assert Path(os.environ['GIT_CONFIG_GLOBAL']).parent == Path(os.environ['HOME'])
    assert os.environ['PIP_CONFIG_FILE'] == '/dev/null'


def test_live_evidence_identifies_focused_mode_and_actual_session_layout():
    scratch, state = run_state()
    result = json.loads((scratch.parents[1] / 'results' / state['run'] / 'result.json').read_text())
    assert result['mode'] == 'ci-layout'
    assert result['ci_full_invocation'] is False
    assert result['runner']['matches_head'] is True
    role = os.environ.get('PYTEST_XDIST_WORKER', 'main')
    events = [json.loads(line) for line in (scratch / 'evidence' / f'{role}.jsonl').read_text().splitlines()]
    layout = next(event['layout'] for event in events if event['kind'] == 'start')
    assert layout['cwd'] == str(ROOT)
    assert layout['TMPDIR'] == layout['tempdir'] == str(ROOT / '.testruns' / 't')
    assert layout['testruns_run_present'] is False
