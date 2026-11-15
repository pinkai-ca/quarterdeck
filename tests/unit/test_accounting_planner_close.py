"""Retiring the planner always closes its files and reaps a delayed exit next pass."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from quarterdeck.accounting_slices import Driver
from .test_accounting_slices import world


@pytest.mark.parametrize('reap_with', ['step', 'close'])
def test_delayed_killed_child_is_closed_now_and_reaped_on_next_pass(tmp_path, monkeypatch, reap_with):
    svc, _, _, _ = world(tmp_path, monkeypatch)
    driver = Driver()
    plan = tempfile.TemporaryFile(dir=svc.runtime)
    child = subprocess.Popen(
        [sys.executable, '-c', 'import signal; signal.raise_signal(signal.SIGSTOP)'],
        stdin=subprocess.PIPE, stdout=plan, stderr=subprocess.DEVNULL)
    poll, kill, wait = child.poll, child.kill, child.wait
    killed, deferred = False, False
    waits, kill_polls = [], []

    def delayed_poll():
        nonlocal deferred
        if killed:
            kill_polls.append(True)
            if not deferred:
                deferred = True
                return None  # the first post-kill reading cannot confirm the exit yet
        return poll()

    def killed_now():
        nonlocal killed
        kill()
        killed = True

    def waited(timeout=None):
        waits.append(timeout)
        if killed:
            raise subprocess.TimeoutExpired(child.args, timeout)
        return wait(timeout=timeout)

    try:
        # A stopped child cannot handle terminate; the bounded first wait must expire.
        assert os.WIFSTOPPED(os.waitpid(child.pid, os.WUNTRACED)[1])
        monkeypatch.setattr(child, 'poll', delayed_poll)
        monkeypatch.setattr(child, 'kill', killed_now)
        monkeypatch.setattr(child, 'wait', waited)
        driver.process, driver.plan, driver.plan_offset = child, plan, 123
        driver.close()
        assert waits == [0.2], 'there must be no second blocking wait after kill'
        assert len(kill_polls) == 1, 'one non-blocking exit reading immediately after kill'
        assert driver.process is driver.plan is None and driver.plan_offset == 0
        assert child.stdin.closed and plan.closed
        assert driver.reaping == [child], 'an unconfirmed exit keeps its process handle'

        # Observe a real zombie without reaping it: the next service step must do that.
        until = time.monotonic() + 5
        while time.monotonic() < until:
            state = Path(f'/proc/{child.pid}/stat').read_text().rsplit(') ', 1)[1].split()[0]
            if state == 'Z':
                break
            time.sleep(0.005)
        else:
            pytest.fail('the killed synthetic child did not exit')
        if reap_with == 'step':
            # Even a step that has no new accounting job must collect the old exit.
            monkeypatch.setattr(type(driver.gate), 'allowed', lambda *args, **kwargs: False)
            driver.step(svc)
        else:
            driver.close()
        assert driver.reaping == [] and child.returncode == -signal.SIGKILL
        with pytest.raises(ChildProcessError):
            os.waitpid(child.pid, os.WNOHANG)
        assert not Path(f'/proc/{child.pid}').exists()
        assert child.stdin.closed and plan.closed
        assert driver.process is driver.plan is None
    finally:
        # Use the native methods so a failed assertion never leaves our stopped child behind.
        if poll() is None:
            kill()
        wait(timeout=5)
        child.stdin.close()
        plan.close()
        driver.close()
        svc.ledger.close()


def test_unexpected_wait_error_still_releases_both_files_and_the_process_slot(tmp_path):
    driver = Driver()
    stdin, plan = tempfile.TemporaryFile(dir=tmp_path), tempfile.TemporaryFile(dir=tmp_path)
    child = SimpleNamespace(returncode=None, stdin=stdin, poll=Mock(return_value=None),
                            terminate=Mock(), wait=Mock(side_effect=OSError('synthetic wait failure')),
                            kill=Mock())
    driver.process, driver.plan = child, plan
    try:
        with pytest.raises(OSError, match='synthetic wait failure'):
            driver.close()
        assert stdin.closed and plan.closed
        assert driver.process is driver.plan is None
        assert driver.reaping == [child]
        child.returncode = 0
        child.poll.return_value = 0
        driver.close()
        assert driver.reaping == []
    finally:
        stdin.close()
        plan.close()
