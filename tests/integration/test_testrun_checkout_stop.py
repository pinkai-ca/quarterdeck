"""A stop while checkout is still inside execute must retire its already registered Git entry."""
import subprocess
import sys
import time

import pytest

from tests.integration.testrun_synthetic import (LEAP, LEAP_SECONDS, TOOL, add_worktree, admin_entries, make,
                                               result, results, scratch, tool_at_phase)


HOLD = r'''
import json, os, pathlib, signal, subprocess, sys, time
scratch = pathlib.Path.cwd()
project = pathlib.Path(sys.argv[1])
state = json.loads((scratch / 'state.json').read_text())
saved = json.loads((project / '.testruns' / 'results' / scratch.name / 'ownership.json').read_text())
intent = ('checkout', 'admin', 'checkout_intent')
assert {key: state[key] for key in intent} == {key: saved[key] for key in intent}
assert state['checkout'] == str(scratch / ('checkout-' + scratch.name))
assert pathlib.Path(state['admin']).name == pathlib.Path(state['checkout']).name
assert not pathlib.Path(state['admin']).exists()
print('checkout intent recorded before add', flush=True)
if os.environ['CHECKOUT_PHASE'] == 'registered':
    subprocess.run(sys.argv[2:], check=True)
    assert pathlib.Path(state['admin']).is_dir()
(scratch / 'checkout-ready').touch()
if os.environ['CHECKOUT_STOP'] == 'signal':
    os.kill(os.getppid(), signal.SIGTERM)
time.sleep(120)
'''

PATCH = f'''
original = run.Runner.execute
def hold_checkout(self, argv, **kwargs):
    if kwargs['title'] == 'checkout':
        argv = [sys.executable, '-I', '-c', {HOLD!r}, str(self.root.project), *map(str, argv)]
    return original(self, argv, **kwargs)
run.Runner.execute = hold_checkout
'''
DRIVER = LEAP.replace('sys.exit(cli.main', PATCH + '\nsys.exit(cli.main')

REFUSE_REMOVAL = '''
def cannot_remove(admin):
    return False
run.clean.drop_worktree_admin = cannot_remove
'''


@pytest.mark.parametrize('stop,phase', [('timeout', 'registered'), ('signal', 'registered'), ('timeout', 'before')])
def test_checkout_stop_removes_its_entry_and_reports_the_fixed_ending(tmp_path, monkeypatch, stop, phase):
    project, worktree = make(tmp_path, {})
    peer = add_worktree(project, 'peer', {})
    before = {name: (project / '.git/worktrees' / name / 'gitdir').read_bytes() for name in admin_entries(project)}
    monkeypatch.setenv('CHECKOUT_STOP', stop)
    monkeypatch.setenv('CHECKOUT_PHASE', phase)
    args = ('run', '--target', str(worktree), '-n', '0', '--timeout', '600')
    gate = 'checkout-ready'
    if stop == 'timeout':
        done, leapt = tool_at_phase(gate, *args, cwd=project, driver=DRIVER)
        assert time.monotonic() - leapt < 90
    else:
        done = subprocess.run([sys.executable, '-I', '-c', DRIVER, str(TOOL), 'never-opens', str(LEAP_SECONDS),
                               *args], cwd=project, capture_output=True, text=True, timeout=120)
    assert done.returncode == (3 if stop == 'timeout' else 4), done.stdout + done.stderr
    found = result(project)
    assert found['outcome']['status'] == stop.replace('signal', 'interrupted')
    assert found['outcome']['pytest_exit'] is None and 'argv' not in found
    assert found['cleanup']['outcome'] == 'complete' and found['cleanup']['git_admin']['removed']
    assert scratch(project) == [] and admin_entries(project) == sorted(before)
    assert peer.is_dir() and {name: (project / '.git/worktrees' / name / 'gitdir').read_bytes()
                             for name in before} == before
    folder = results(project)[-1]
    report = (folder / 'report.md').read_text()
    assert 'checkout intent recorded before add' in report and 'complete' in report
    assert sorted(path.name for path in folder.iterdir()) == ['report.md', 'result.json']


def test_checkout_stop_reports_held_when_its_admin_cannot_be_removed(tmp_path, monkeypatch):
    project, worktree = make(tmp_path, {})
    monkeypatch.setenv('CHECKOUT_STOP', 'timeout')
    monkeypatch.setenv('CHECKOUT_PHASE', 'registered')
    driver = DRIVER.replace('sys.exit(cli.main', REFUSE_REMOVAL + '\nsys.exit(cli.main')
    done, _ = tool_at_phase('checkout-ready', 'run', '--target', str(worktree), '-n', '0',
                            '--timeout', '600', cwd=project, driver=driver)
    assert done.returncode == 3, done.stdout + done.stderr
    found = result(project)
    assert found['outcome']['status'] == 'timeout' and found['cleanup']['outcome'] == 'held'
    admin = found['cleanup']['git_admin']
    assert not admin['removed'] and not admin['command_ok']
    assert admin_entries(project) == sorted(['feature', admin['path'].rsplit('/', 1)[-1]])
    assert 'remains' in found['cleanup']['reason'] and 'cleanup held' in done.stdout
    assert (results(project)[-1] / 'ownership.json').is_file()
