"""Ordinary suite-resource policy; allocation probes run only in small bounded children."""
import json
import os
from pathlib import Path
import resource
import subprocess
import sys

import pytest

from tests.testrunner import cli, resources


def test_default_and_lower_requested_memory_are_bounded():
    assert resources.requested_memory({}) == 8192
    assert resources.requested_memory({resources.MEMORY_ENV: '256'}) == 256


@pytest.mark.parametrize('value', ['0', '-1', '8193', 'inf', 'nan', '1.5', '', 'true'])
def test_invalid_or_larger_memory_request_is_refused(value):
    with pytest.raises(ValueError, match='test memory limit must be an integer'):
        resources.memory_mib(value)


@pytest.mark.parametrize('limits,expected', [((-1, -1), 8192), ((512, 1024), 512), ((1024, -1), 1024)])
def test_soft_and_hard_limits_are_lowered_without_weakening_a_tighter_caller(monkeypatch, limits, expected):
    previous = tuple(value * resources.MIB if value != -1 else resource.RLIM_INFINITY for value in limits)
    calls = []
    monkeypatch.setattr(resource, 'getrlimit', lambda kind: previous)
    monkeypatch.setattr(resource, 'setrlimit', lambda kind, pair: calls.append((kind, pair)))
    assert resources.apply_memory(8192) == expected * resources.MIB
    assert calls == [(resource.RLIMIT_AS, (expected * resources.MIB, expected * resources.MIB))]


@pytest.mark.parametrize('value', ['0', '-1', 'nan', 'inf', '-inf'])
def test_a_run_cannot_disable_the_wall_timeout(value, capsys):
    assert cli.main(['run', '--timeout=' + value], []) == 2
    assert '--timeout must be finite and greater than zero' in capsys.readouterr().err


@pytest.mark.parametrize('mode', ['exec-empty-environment', 'multiprocessing-spawn'])
def test_small_child_limit_survives_exec_and_spawn_and_refuses_allocation(tmp_path, mode):
    script = tmp_path / 'synthetic_memory_zzzz.py'
    script.write_text('''
import json, multiprocessing, os, resource, subprocess, sys
LIMIT = 64 * 1024 * 1024
def probe():
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    try:
        bytearray(2 * LIMIT)
    except MemoryError:
        print(json.dumps(dict(soft=soft, hard=hard, error="MemoryError", pid_local=True)), flush=True)
    else:
        raise AssertionError("allocation unexpectedly exceeded inherited hard cap")
if __name__ == "__main__":
    resource.setrlimit(resource.RLIMIT_AS, (LIMIT, LIMIT))
    if sys.argv[1] == "exec-empty-environment":
        child = subprocess.run([sys.executable, "-c",
            "from synthetic_memory_zzzz import probe; probe()"], env={}, check=True, timeout=10)
    else:
        child = multiprocessing.get_context("spawn").Process(target=probe)
        child.start()
        child.join(10)
        if child.is_alive():
            child.kill()
            child.join(3)
            raise AssertionError("bounded allocation probe did not finish")
        assert child.exitcode == 0
''')
    done = subprocess.run([sys.executable, str(script), mode], cwd=tmp_path,
                          capture_output=True, text=True, timeout=20, check=True)
    found = json.loads(done.stdout)
    assert found['error'] == 'MemoryError'
    assert found['soft'] == found['hard'] == 64 * resources.MIB


def test_this_pytest_process_is_capped_before_tests_start():
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    assert 0 < soft == hard <= resources.DEFAULT_MEMORY_MIB * resources.MIB


def test_runner_sets_openblas_before_starting_pytest_without_disabling_plugins(tmp_path, monkeypatch):
    from tests.testrunner import environment

    monkeypatch.setenv('OPENBLAS_NUM_THREADS', '32')
    env, recorded = environment.build(tmp_path, 'synthetic-memory-policy-zzzz')
    assert env['OPENBLAS_NUM_THREADS'] == recorded['fixed']['OPENBLAS_NUM_THREADS'] == '1'
    assert 'OPENBLAS_NUM_THREADS' in recorded['removed_names']
    assert 'PYTEST_DISABLE_PLUGIN_AUTOLOAD' not in env


@pytest.mark.parametrize('observed', [None, '1', '32'])
def test_shared_header_never_attests_earlier_plugin_import_timing(monkeypatch, observed):
    from types import SimpleNamespace
    from tests import conftest

    monkeypatch.setattr(conftest, 'OPENBLAS_AT_IMPORT', observed)
    header = '\n'.join(conftest.pytest_report_header(SimpleNamespace(_qd_memory_limit=8192 * resources.MIB)))
    assert 'earlier plugin-import timing is unverified' in header
    assert ('setting it now is too late' in header) == (observed != '1')


def test_runner_restores_alarm_state_after_its_deadline():
    import signal
    import time
    from types import SimpleNamespace
    from tests.testrunner import run

    handler = signal.getsignal(signal.SIGALRM)
    timer = signal.getitimer(signal.ITIMER_REAL)
    runner = run.Runner(SimpleNamespace(timeout=.02), [])
    runner.bound(.02, 'time limit of 0.02 s')
    with pytest.raises(run.Stop, match='time limit of 0.02 s'):
        with runner.time_limit():
            time.sleep(.1)
    assert runner.timed_out
    assert signal.getsignal(signal.SIGALRM) == handler
    restored = signal.getitimer(signal.ITIMER_REAL)
    assert restored[1] == timer[1]
    assert restored[0] <= timer[0]
