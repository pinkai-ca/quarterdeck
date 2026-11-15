"""Explicit-only installed-wheel settlement trials on four CPUs under CPU load.

The existing run-temporary install test runs unchanged. Its installed drill enters
through the same console main under an observational wrapper; product bytes and
the original wait bound remain unchanged. No native program or provider is used.
Supply DRILL_PYTHON312 when selecting the explicit Python 3.12 trials.
"""

from contextlib import nullcontext
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from tests.integration import test_plain_install as installed
from tests.shared_install import shared_wheel  # noqa: F401


PY312 = os.environ.get('DRILL_PYTHON312')
TRIALS = 8
LOAD = 8

OBSERVE = r'''
import collections, json, sys, time
from quarterdeck import drill
import quarterdeck
original = drill.Drill.fresh_after_delivery
def measured(self, seat, order):
    began = time.monotonic()
    fresh = seat.fresh
    called = []
    busy = [0]
    def transition():
        at = time.monotonic()
        try:
            result = fresh()
        except drill.lifecycle.Busy:
            busy[0] += 1
            raise
        called.append((at, time.monotonic()))
        return result
    seat.fresh = transition
    result = None
    try:
        result = original(self, seat, order)
        ended = time.monotonic()
        settled_later = None
        phases = {}
        errors = 0
        # A timeout remains a timeout. Observe only, never call fresh or resend,
        # for one additional minute to distinguish a late event from a stuck one.
        if result[0] is None:
            until = ended + 60.0
            while True:
                proofs, invalid = drill.journals(seat)
                phases = dict(collections.Counter(p.data.get('phase', 'unknown') for p in proofs))
                errors = len(invalid)
                active = any(phases.get(p, 0) for p in ('prepared','pasting','submitting','submitted'))
                attempts = self.attempts.all()
                unfinished = any(a.seat == seat.name and a.result is None for a in attempts)
                if not errors and not active and not unfinished:
                    settled_later = time.monotonic() - began
                    break
                if time.monotonic() >= until:
                    break
                time.sleep(.1)
        print('DRILL_SETTLEMENT ' + json.dumps(dict(
            python=sys.version.split()[0], role=seat.spec.role, human=seat.session.human,
            bound_seconds=self.patience, success=result[0] is not None,
            call_seconds=ended-began,
            settlement_seconds=called[-1][0]-began if called else None,
            fresh_seconds=called[-1][1]-called[-1][0] if called else None,
            lifecycle_busy=busy[0], settled_after_timeout_seconds=settled_later,
            observed_seconds=time.monotonic()-began, final_phases=phases, final_errors=errors)),
            file=sys.stderr, flush=True)
        return result
    finally:
        seat.fresh = fresh
drill.Drill.fresh_after_delivery = measured
sys.argv = ['qd', 'drill']
sys.exit(quarterdeck.main())
'''


@pytest.fixture(scope='module')
def cpu_load():
    cpus = sorted(os.sched_getaffinity(0))
    assert len(cpus) == 4, 'run this explicit measurement pinned to four CPUs'
    processes = []
    try:
        for _ in range(LOAD):
            processes.append(subprocess.Popen([sys.executable, '-I', '-c', 'while True: pass'],
                                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL))
        yield cpus, processes
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


def load_cpu(processes):
    result = 0.0
    for process in processes:
        assert process.poll() is None
        fields = (Path('/proc') / str(process.pid) / 'stat').read_text().rsplit(') ', 1)[1].split()
        result += (int(fields[11]) + int(fields[12])) / os.sysconf('SC_CLK_TCK')
    return result


@pytest.mark.parametrize('version', ['ordinary', '3.12'])
@pytest.mark.parametrize('trial', range(TRIALS))
def test_installed_drill_settlement_trials(tmp_path, private_socket_root, capsys, shared_wheel,
                                          monkeypatch, cpu_load, version, trial):
    cpus, processes = cpu_load
    samples = []
    original = installed.command
    def command(*argv, cwd, env, timeout=180):
        if len(argv) != 2 or argv[1] != 'drill':
            return original(*argv, cwd=cwd, env=env, timeout=timeout)
        # This is the installed wheel's own isolated interpreter and product.
        python = Path(argv[0]).parent / 'python'
        done = subprocess.run([str(python), '-I', '-c', OBSERVE], cwd=cwd, env=env,
                              capture_output=True, text=True, timeout=timeout + 180)
        samples.extend(json.loads(line.removeprefix('DRILL_SETTLEMENT '))
                       for line in done.stderr.splitlines() if line.startswith('DRILL_SETTLEMENT '))
        unexpected = [line for line in done.stderr.splitlines() if not line.startswith('DRILL_SETTLEMENT ')]
        assert not unexpected, unexpected
        assert done.returncode == 0, (done.returncode, done.stdout)
        return done.stdout
    began, cpu = time.monotonic(), load_cpu(processes)
    try:
        with monkeypatch.context() as scope:
            scope.setattr(installed, 'command', command)
            if version == '3.12':
                assert PY312 and Path(PY312).is_file(), 'set DRILL_PYTHON312 to an installed 3.12 interpreter'
                # Only venv creation inside this unchanged test uses this value.
                # Pytest/controller stays on the ordinary interpreter.
                scope.setattr(sys, 'executable', PY312)
            installed.test_wheel_installed_laws_and_fake_drydock(
                tmp_path, private_socket_root, SimpleNamespace(disabled=nullcontext), shared_wheel)
    finally:
        capsys.readouterr()  # successful source inventories are not timing evidence
        with capsys.disabled():
            print('INSTALLED_SETTLEMENT_TRIAL ' + json.dumps(dict(
                trial=trial, interpreter=version, cpus=cpus, cpu_load_processes=LOAD,
                wall_seconds=time.monotonic()-began, load_cpu_seconds=load_cpu(processes)-cpu,
                samples=samples), sort_keys=True), flush=True)
