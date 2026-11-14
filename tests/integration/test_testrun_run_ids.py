"""A repeated random run-name candidate never reuses a completed run's evidence."""
import subprocess
import sys

from tests.integration.testrun_synthetic import TOOL, controller_environment, make, result, results


COLLIDING = r"""
import secrets, sys, time
from pathlib import Path
from types import SimpleNamespace
sys.dont_write_bytecode = True
tool = Path(sys.argv[1])
sys.path.insert(0, str(tool.parent))
from testrunner import cli, layout
real = secrets.token_hex
candidates = iter(('abcd', 'bcde'))
secrets.token_hex = lambda count: next(candidates) if count == 2 else real(count)
layout.time = SimpleNamespace(strftime=lambda fmt: '20200101-000000',
                            monotonic=time.monotonic, sleep=time.sleep)
files = [tool, *sorted((tool.parent / 'testrunner').glob('*.py'))]
sys.exit(cli.main(sys.argv[2:], files))
"""


def test_two_tool_runs_keep_their_results_when_the_random_part_repeats(tmp_path):
    project, worktree = make(tmp_path, {})
    command = [sys.executable, '-I', '-c', COLLIDING, str(TOOL), 'run', '--target', str(worktree), '-n', '0']
    first = subprocess.run(command, cwd=project, env=controller_environment(), capture_output=True,
                           text=True, timeout=120)
    assert first.returncode == 0, first.stdout + first.stderr
    first_folder, = results(project)
    assert first_folder.name == '20200101-000000-abcd'
    original = {path.name: path.read_bytes() for path in first_folder.iterdir() if path.is_file()}
    second = subprocess.run(command, cwd=project, env=controller_environment(), capture_output=True,
                            text=True, timeout=120)
    assert second.returncode == 0, second.stdout + second.stderr
    folders = results(project)
    assert [folder.name for folder in folders] == ['20200101-000000-abcd', '20200101-000000-bcde']
    assert original == {path.name: path.read_bytes() for path in first_folder.iterdir() if path.is_file()}
    for folder in folders:
        found = result(project, folder)
        assert found['run'] == folder.name and found['outcome']['status'] == 'passed'
        assert found['cleanup']['outcome'] == 'complete'
