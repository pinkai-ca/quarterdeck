"""Explicit ordinary layout probe; select this file only with the CI-layout mode."""
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys

from tests.testrunner import layout, scratch
from quarterdeck.tmux import Tmux

ROOT = Path(__file__).resolve().parents[2]


def git(folder, *arguments):
    return subprocess.check_output(['git', '-C', str(folder), *arguments], text=True).strip()


def test_actual_checkout_has_its_own_git_common_directory():
    assert ROOT.joinpath('.git').is_dir()
    assert not ROOT.joinpath('.git').is_symlink()
    assert Path(git(ROOT, 'rev-parse', '--path-format=absolute', '--git-common-dir')) == ROOT / '.git'
    assert git(ROOT, 'rev-parse', '--is-shallow-repository') == 'false'


def test_actual_test_temp_is_inside_that_checkout_and_inherits_its_git_context(tmp_path):
    assert tmp_path.is_relative_to(ROOT / '.testruns/t')
    fd = os.open(tmp_path, scratch.DIRECTORY)
    try:
        assert scratch.physical_filesystem(fd) in ('tmpfs', 'ramfs')
        assert scratch.filesystem(fd) is None
    finally:
        os.close(fd)
    assert scratch.in_memory(tmp_path) is False  # the code under test sees the runner's disk world
    assert Path(os.environ['TMPDIR']) == tmp_path
    assert Path(git(tmp_path, 'rev-parse', '--show-toplevel')) == ROOT


def test_actual_ci_environment_and_nested_python_use_the_owned_venv_and_disk_policy(tmp_path):
    assert Path(sys.prefix) == ROOT / '.venv'
    assert Path(os.environ['VIRTUAL_ENV']) == ROOT / '.venv'
    assert 'TESTRUNS_RUN' not in os.environ
    assert 'TESTRUN_SESSION_RECORDS' not in os.environ
    assert os.environ[scratch.CI_DISK_ENV] == '1'
    assert list(scratch.choices()) == []
    code = ('import json, os; print(json.dumps({"run": os.environ.get("TESTRUNS_RUN"), '
            '"disk": os.environ.get("TESTRUNS_CI_DISK"), "tmp": os.environ["TMPDIR"]}))')
    child = subprocess.run([sys.executable, '-I', '-c', code], capture_output=True, text=True,
                           check=True, timeout=10)
    assert json.loads(child.stdout) == {'run': None, 'disk': '1', 'tmp': str(tmp_path)}


def test_logical_disk_fallback_stays_on_physical_memory_and_uses_the_genuine_reason(tmp_path):
    project = tmp_path / 'synthetic-project'
    project.mkdir()
    subprocess.run(['git', 'init', '-q', project], check=True, timeout=10)
    root = layout.Root(project)
    try:
        choice = scratch.prepare(root, '20000101-000000-abcd')
        assert choice['kind'] == 'disk'
        assert Path(choice['path']).is_relative_to(project / '.testruns')
        fd = os.open(project, scratch.DIRECTORY)
        try:
            assert scratch.physical_filesystem(fd) in ('tmpfs', 'ramfs')
            assert scratch.filesystem(fd) is None
        finally:
            os.close(fd)
        assert scratch.in_memory(Path(choice['path'])) is False
        assert choice['reason'] == ('no writable executable tmpfs/ramfs has room for a 4 GiB reservation '
                                    'plus the 8 GiB margin and outstanding reservations')
    finally:
        root.close()


def test_ordinary_private_server_still_starts_and_ends_without_normal_registry_variables(tmp_path):
    tmux = Tmux.private(tmp_path / 'ordinary-private-server')
    try:
        identity = tmux.new_session('ordinary-ci-probe')
        assert identity.id.startswith('$')
    finally:
        assert tmux.close() is True


def test_nested_tool_uses_the_activated_checkout_python_and_can_start_xdist(tmp_path):
    ordinary = ROOT / '.venv/bin/python3'
    assert Path(shutil.which('python3')) == ordinary
    project = tmp_path / 'nested-ordinary-project'
    project.mkdir()
    (project / 'test_sample.py').write_text('import xdist\ndef test_sample():\n    assert xdist.__version__\n')
    (project / 'pyproject.toml').write_text(
        '[build-system]\nrequires = ["setuptools"]\nbuild-backend = "setuptools.build_meta"\n'
        '[project]\nname = "synthetic-ci-nested"\nversion = "0.0.1"\n'
        '[tool.setuptools]\npy-modules = []\n')
    for args in (['init', '-q'], ['add', 'test_sample.py', 'pyproject.toml'],
                 ['-c', 'user.name=Synthetic Author', '-c', 'user.email=author@example.invalid',
                  'commit', '-qm', 'Synthetic ordinary test']):
        subprocess.run(['git', '-C', str(project), *args], check=True, timeout=10)
    result = subprocess.run([str(ordinary), '-I', str(ROOT / 'tests/testrun.py'), 'run',
                             '--target', str(project), '-n', '2', '--timeout', '60', 'test_sample.py'],
                            cwd=project, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    results = list((project / '.testruns/results').glob('*/result.json'))
    assert len(results) == 1
    evidence = json.loads(results[0].read_text())
    assert evidence['tools']['ordinary_python3']['path'] == str(ordinary)
    assert evidence['tools']['run_venv']['xdist'] is not None
    assert evidence['evidence']['counts']['passed'] == 1
    assert evidence['cleanup']['outcome'] == 'complete'
