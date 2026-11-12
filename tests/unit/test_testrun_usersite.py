"""The user site reaches the run venv as one plain path line, read-only, with nothing in it processed: every
interpreter of the run starts with PYTHONNOUSERSITE, and resolving the user site processes nothing either."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.testrunner import environment, layout, usersite


def test_the_path_line_is_one_plain_line_in_a_new_file(tmp_path):
    site = tmp_path / 'user-site'
    site.mkdir()
    purelib = tmp_path / 'purelib'
    purelib.mkdir()
    line = usersite.add_path_line(purelib, str(site))
    assert line.read_text() == f'{site}\n' and sorted(os.listdir(purelib)) == [usersite.PATH_LINE]
    with pytest.raises(FileExistsError):
        usersite.add_path_line(purelib, str(site))


@pytest.mark.parametrize('bad', ['relative/site', '/a\nimport os', 'import os; os.system("x")', '/a\0b'])
def test_anything_but_an_absolute_plain_path_is_refused(tmp_path, bad):
    with pytest.raises(layout.Refused):
        usersite.add_path_line(tmp_path, bad)
    assert os.listdir(tmp_path) == []


def test_the_user_site_is_resolved_from_the_callers_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('PYTHONUSERBASE', str(tmp_path / 'base'))
    found = usersite.resolve(sys.executable)
    assert found['base'] == str(tmp_path / 'base') and found['site'].startswith(str(tmp_path / 'base'))
    assert found['exists'] is False


def test_bytecode_folders_are_stamped_and_any_change_is_named(tmp_path):
    cache = tmp_path / 'pkg' / '__pycache__'
    cache.mkdir(parents=True)
    (cache / 'a.cpython-314.pyc').write_bytes(b'x')
    before = usersite.stamps(str(tmp_path))
    assert before['count'] == 1 and usersite.changed(before, usersite.stamps(str(tmp_path))) == []
    (cache / 'b.cpython-314.pyc').write_bytes(b'y')
    assert usersite.changed(before, usersite.stamps(str(tmp_path))) == ['pkg/__pycache__']


def test_stale_or_missing_bytecode_is_listed(tmp_path):
    package = tmp_path / 'tool'
    package.mkdir()
    (package / 'fresh.py').write_text('X = 1\n')
    (package / 'missing.py').write_text('Y = 1\n')
    import py_compile
    py_compile.compile(str(package / 'fresh.py'), doraise=True)
    stale = usersite.stale_bytecode(sys.executable, [str(package)], dict(os.environ))
    assert stale == [str(package / 'missing.py')]


HOOK = 'import os, pathlib; pathlib.Path({markers!r}).joinpath(str(os.getpid())).touch()\n'


def hooked_site(site, markers):
    """A test-owned user site: a startup hook that leaves a marker, and an ordinary importable module."""
    site.mkdir(parents=True)
    markers.mkdir(exist_ok=True)
    (site / 'synth_hook.pth').write_text(HOOK.format(markers=str(markers)))
    (site / 'synth_usertool.py').write_text('VALUE = "user site"\n')


def test_the_run_environment_skips_every_user_site_and_carries_the_base_under_its_own_name(tmp_path, monkeypatch):
    monkeypatch.setenv('PYTHONUSERBASE', '/operator/base')
    monkeypatch.setenv('TESTRUNS_USERBASE', '/outer/base')
    env, record = environment.build(tmp_path / 'scratch', 'run-1', '/resolved/base')
    assert env['PYTHONNOUSERSITE'] == '1' and record['fixed']['PYTHONNOUSERSITE'] == '1'
    assert 'PYTHONUSERBASE' not in env and 'PYTHONUSERBASE' not in record['fixed']
    assert env['TESTRUNS_USERBASE'] == record['fixed']['TESTRUNS_USERBASE'] == '/resolved/base'
    assert {'PYTHONUSERBASE', 'TESTRUNS_USERBASE'} <= set(record['removed_names'])
    env, record = environment.build(tmp_path / 'other', 'run-2')
    assert env['PYTHONNOUSERSITE'] == '1' and 'TESTRUNS_USERBASE' not in env and 'PYTHONUSERBASE' not in env


def test_a_run_inside_a_run_resolves_the_carried_base_and_no_other_caller_does(tmp_path, monkeypatch):
    monkeypatch.setenv('PYTHONUSERBASE', str(tmp_path / 'caller'))
    monkeypatch.setenv('TESTRUNS_USERBASE', str(tmp_path / 'carried'))
    monkeypatch.delenv('TESTRUNS_RUN', raising=False)
    assert usersite.resolve(sys.executable)['base'] == str(tmp_path / 'caller')
    monkeypatch.setenv('TESTRUNS_RUN', 'outer-run')
    assert usersite.resolve(sys.executable)['base'] == str(tmp_path / 'carried')


def test_resolving_the_user_site_processes_nothing_in_it(tmp_path, monkeypatch):
    base, markers = tmp_path / 'base', tmp_path / 'markers'
    monkeypatch.delenv('PYTHONNOUSERSITE', raising=False)
    monkeypatch.delenv('TESTRUNS_RUN', raising=False)
    monkeypatch.setenv('PYTHONUSERBASE', str(base))
    python = getattr(sys, '_base_executable', sys.executable)  # a plain interpreter, whose user site is enabled
    site = subprocess.run([python, '-s', '-c', 'import site; print(site.getusersitepackages())'], text=True,
                          capture_output=True, check=True).stdout.strip()
    hooked_site(Path(site), markers)
    found = usersite.resolve(python)
    assert found['site'] == site and found['exists'] and found['enabled']
    assert os.listdir(markers) == []


def test_a_child_started_in_the_run_environment_processes_no_user_site(tmp_path, monkeypatch):
    """The run's HOME holds a user site with a startup hook; a child started with the run's environment skips
    it and still imports the site's module through the plain path line. The same child without the fixed
    setting is the control: it runs the hook."""
    for name in [name for name in os.environ if name.startswith('PYTHON') or name.startswith('TESTRUNS_')]:
        monkeypatch.delenv(name)
    env, _ = environment.build(tmp_path / 'scratch', 'run-1')
    python = getattr(sys, '_base_executable', sys.executable)
    site = subprocess.run([python, '-s', '-c', 'import site; print(site.getusersitepackages())'], env=env,
                          text=True, capture_output=True, check=True).stdout.strip()
    assert Path(site).is_relative_to(env['HOME'])
    markers = tmp_path / 'markers'
    hooked_site(Path(site), markers)
    path_line = tmp_path / 'purelib'
    path_line.mkdir()
    usersite.add_path_line(path_line, site)
    probe = ('import json, site, sys; site.addsitedir(sys.argv[1]); import synth_usertool; '
             'print(json.dumps([sys.flags.no_user_site, synth_usertool.__file__]))')
    done = subprocess.run([python, '-c', probe, str(path_line)], env=env, text=True, capture_output=True,
                          check=True)
    assert json.loads(done.stdout) == [1, os.path.join(site, 'synth_usertool.py')]
    assert os.listdir(markers) == []
    control = {name: value for name, value in env.items() if name != 'PYTHONNOUSERSITE'}
    subprocess.run([python, '-c', 'pass'], env=control, check=True)
    assert len(os.listdir(markers)) == 1
