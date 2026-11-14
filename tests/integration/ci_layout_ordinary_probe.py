"""Explicit ordinary layout probe; select this file only with the CI-layout mode."""
import json
import os
from pathlib import Path
import subprocess
import sys

from tests.testrunner import scratch

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
    assert scratch.in_memory(tmp_path) is False
    assert Path(os.environ['TMPDIR']) == tmp_path
    assert Path(git(tmp_path, 'rev-parse', '--show-toplevel')) == ROOT


def test_actual_ci_environment_and_nested_python_use_the_owned_venv_and_disk_policy(tmp_path):
    assert Path(sys.prefix) == ROOT / '.venv'
    assert Path(os.environ['VIRTUAL_ENV']) == ROOT / '.venv'
    assert 'TESTRUNS_RUN' not in os.environ
    assert os.environ[scratch.CI_DISK_ENV] == '1'
    assert list(scratch.choices()) == []
    code = ('import json, os; print(json.dumps({"run": os.environ.get("TESTRUNS_RUN"), '
            '"disk": os.environ.get("TESTRUNS_CI_DISK"), "tmp": os.environ["TMPDIR"]}))')
    child = subprocess.run([sys.executable, '-I', '-c', code], capture_output=True, text=True,
                           check=True, timeout=10)
    assert json.loads(child.stdout) == {'run': None, 'disk': '1', 'tmp': str(tmp_path)}
