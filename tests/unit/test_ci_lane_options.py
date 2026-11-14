"""Ordinary CI selection, command and declared disk-layout behavior."""
import os
from pathlib import Path

import pytest

from tests.testrunner import cli, environment, run, scratch


def test_ci_layout_is_orthogonal_to_explicit_whole_and_author_fast(tmp_path):
    (tmp_path / 'tests/unit').mkdir(parents=True)
    whole = cli.parser().parse_args(['run', '--ci-layout', '--whole'])
    assert whole.ci_layout is True
    assert run.selection(whole, tmp_path) == {
        'name': 'whole', 'targets': ['tests'], 'workers': 8, 'slots': 1,
    }
    fast = cli.parser().parse_args(['run', '--ci-layout'])
    assert fast.ci_layout is True
    assert run.selection(fast, tmp_path) == {
        'name': 'fast', 'targets': ['tests/unit'], 'workers': 16, 'slots': 2,
    }


def test_ci_named_targets_keep_their_identity_and_worker_override(tmp_path):
    args = cli.parser().parse_args(['run', '--ci-layout', '-n', '2', 'example.py::test_sample'])
    assert run.selection(args, tmp_path) == {
        'name': 'targets', 'targets': ['example.py::test_sample'], 'workers': 2, 'slots': 1,
    }


@pytest.mark.parametrize('workers', [0, 2, 8, 16])
def test_ci_pytest_command_uses_workflow_distribution_and_no_forced_basetemp(workers):
    python, folder = Path('example-python'), Path('example-scratch')
    command = run.pytest_argv(python, folder, workers, ['tests'], ci_layout=True)
    expected = [python, '-m', 'pytest', '-q', '-rs', '-p', 'qd_testrun_evidence']
    if workers:
        expected += ['-n', str(workers)]
    assert command == expected + ['tests']
    assert '--dist' not in command
    assert not any(str(item).startswith('--basetemp') for item in command)


def test_normal_pytest_command_retains_worksteal_and_owned_basetemp():
    command = run.pytest_argv(Path('python'), Path('scratch'), 8, ['tests'])
    assert '--basetemp=scratch/tmp/b' in command
    assert command[command.index('--dist') + 1] == 'worksteal'


def test_ci_environment_declares_disk_layout_without_the_run_layout_flag(tmp_path):
    env, record = environment.build(tmp_path / 'outer', 'example-run', ci_layout=True)
    assert 'TESTRUNS_RUN' not in env
    assert 'TESTRUN_SESSION_RECORDS' not in env
    assert 'TESTRUN_CI_SESSION_CONTEXT' not in env
    assert env[scratch.CI_DISK_ENV] == '1'
    assert env['OPENBLAS_NUM_THREADS'] == '1'
    assert record['fixed'][scratch.CI_DISK_ENV] == '1'
    config = Path(env['GIT_CONFIG_GLOBAL'])
    assert config.parent == Path(env['HOME'])
    assert 'Quarterdeck CI' in config.read_text()
    assert env['PIP_CONFIG_FILE'] == '/dev/null'


def test_nested_default_tool_environment_retains_the_ci_no_room_constraint(tmp_path, monkeypatch):
    monkeypatch.setenv(scratch.CI_DISK_ENV, '1')
    monkeypatch.setenv('TESTRUN_CI_SESSION_CONTEXT', 'outer-instrumentation')
    env, record = environment.build(tmp_path / 'nested', 'example-nested')
    assert env['TESTRUNS_RUN'] == 'example-nested'
    assert 'TESTRUN_CI_SESSION_CONTEXT' not in env
    assert env[scratch.CI_DISK_ENV] == '1'
    assert record['fixed'][scratch.CI_DISK_ENV] == '1'
    assert list(scratch.choices()) == []
    assert scratch.choose() is None
    assert scratch.plain(tmp_path) is None


def test_ci_default_candidates_model_real_bases_but_explicit_candidates_keep_their_folder(tmp_path, monkeypatch):
    monkeypatch.setenv(scratch.CI_DISK_ENV, '1')
    monkeypatch.setenv('TMPDIR', str(tmp_path))
    assert scratch.candidates() == [Path('/dev/shm'), Path('/tmp')]
    assert scratch.candidates({'TMPDIR': str(tmp_path)})[0] == tmp_path
    monkeypatch.setattr(scratch, 'candidates', lambda: [tmp_path])
    monkeypatch.setattr(scratch, 'filesystem', lambda descriptor: 'tmpfs')
    assert list(scratch.choices(bound=1)) == [(tmp_path, 'tmpfs')]


@pytest.mark.parametrize('base', ['/dev/shm', '/tmp'])
def test_ci_only_real_bases_have_simulated_no_room_while_physical_admission_is_real(base, tmp_path, monkeypatch):
    monkeypatch.setenv(scratch.CI_DISK_ENV, '1')
    monkeypatch.setattr(scratch, 'filesystem', lambda descriptor: 'tmpfs')
    fd = os.open(tmp_path, scratch.DIRECTORY)
    try:
        actual = os.fstatvfs(fd)
        expected = actual.f_bavail * actual.f_frsize
        assert scratch.available_bytes(Path(base), fd) == 0
        assert scratch.available_bytes(Path(base), fd, physical=True) == expected
        assert scratch.available_bytes(tmp_path, fd) == expected
    finally:
        os.close(fd)
