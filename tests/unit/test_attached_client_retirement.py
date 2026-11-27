"""Controlled-state and real-process retirement of the terminal-client helper."""

from contextlib import nullcontext
from io import BytesIO
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from quarterdeck.procstat import EXITED
from tests.integration import waiting


def command(ready, *, ignore=False, descendant=None, output="", stopped=None):
    code = "import pathlib, signal, subprocess, sys, time\n"
    if ignore:
        code += "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    if stopped is not None:
        code += ("def stop(*_):\n"
                 f"    pathlib.Path({str(stopped)!r}).touch()\n"
                 "    sys.exit(0)\n"
                 "signal.signal(signal.SIGTERM, stop)\n")
    if descendant is not None:
        child_ready = str(descendant) + '.ready'
        child = ("import pathlib, signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                 "pathlib.Path(" + repr(child_ready) + ").write_text('synthetic-ready-zzzz'); signal.pause()")
        code += "child = subprocess.Popen([sys.executable, '-c', " + repr(child) + "])\n"
        code += "deadline = time.monotonic() + 5\n"
        code += "while not pathlib.Path(" + repr(child_ready) + ").exists():\n"
        code += "    assert time.monotonic() < deadline\n    time.sleep(0.001)\n"
        code += "pathlib.Path(" + repr(str(descendant)) + ").write_text(str(child.pid))\n"
    if output:
        code += "print(" + repr(output) + ", flush=True)\n"
    code += "pathlib.Path(" + repr(str(ready)) + ").write_text('synthetic-ready-zzzz')\n"
    code += "while True: signal.pause()\n"
    return shlex.join([sys.executable, '-u', '-c', code])


def alive(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0] not in EXITED
    except FileNotFoundError:
        return False


def test_ready_client_retires_without_the_script_shutdown_delay(tmp_path, monkeypatch):
    ready, stopped = tmp_path / 'ready', tmp_path / 'stopped'
    with waiting.attached_client(command(ready, stopped=stopped), ready.exists) as client:
        assert client.poll() is None
        assert not stopped.exists()
        monkeypatch.setattr(client, 'terminate', lambda: pytest.fail('retirement signalled the script wrapper'))
    assert client.poll() is not None
    assert stopped.exists(), 'the terminal command received SIGTERM directly'


def test_nested_clients_retire_independently(tmp_path):
    outer_ready, inner_ready = tmp_path / 'outer', tmp_path / 'inner'
    outer_stopped, inner_stopped = tmp_path / 'outer-stopped', tmp_path / 'inner-stopped'
    with waiting.attached_client(command(outer_ready, stopped=outer_stopped), outer_ready.exists) as outer:
        with waiting.attached_client(command(inner_ready, stopped=inner_stopped), inner_ready.exists) as inner:
            assert inner.poll() is None
        assert inner.poll() is not None
        assert inner_stopped.exists() and not outer_stopped.exists() and outer.poll() is None
    assert outer.poll() is not None
    assert outer_stopped.exists()


@pytest.mark.parametrize('reaps_after_kill', [True, False])
def test_force_retirement_stays_bounded_by_the_explicit_window(tmp_path, monkeypatch, reaps_after_kill):
    clock = SimpleNamespace(now=0.0)
    waits, signals = [], []
    proc = tmp_path / 'proc'
    group = proc / '102'
    group.mkdir(parents=True)
    children = proc / '101' / 'task' / '101' / 'children'
    children.parent.mkdir(parents=True)
    children.write_text('102')
    (group / 'task' / '102').mkdir(parents=True)
    (group / 'task' / '102' / 'children').write_text('')

    def group_state(state):
        fields = [state, '101', '102', '102'] + ['0'] * 16
        fields[19] = '1'
        (group / 'stat').write_text('102 (synthetic-client) ' + ' '.join(fields))

    group_state('S')

    class Client:
        pid = 101
        returncode = None
        stdin = BytesIO()

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            waits.append(timeout)
            clock.now += timeout
            raise subprocess.TimeoutExpired('synthetic-client', timeout)

        def kill(self):
            signals.append(('client', signal.SIGKILL, clock.now))
            if reaps_after_kill:
                self.returncode = -signal.SIGKILL

    client = Client()

    def signal_group(pid, signum):
        assert pid == 102
        signals.append(('group', signum, clock.now))
        if signum == signal.SIGKILL and reaps_after_kill:
            group_state('Z')

    monkeypatch.setattr(waiting, 'Path', lambda path: proc if path == '/proc' else Path(path))
    monkeypatch.setattr(waiting, 'time', SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(waiting, 'os', SimpleNamespace(environ=waiting.os.environ, killpg=signal_group))
    monkeypatch.setattr(waiting, 'subprocess', SimpleNamespace(
        Popen=lambda *args, **kwargs: client, PIPE=subprocess.PIPE,
        STDOUT=subprocess.STDOUT, TimeoutExpired=subprocess.TimeoutExpired))

    patience = 0.1
    outcome = nullcontext() if reaps_after_kill else pytest.raises(subprocess.TimeoutExpired)
    with outcome as error:
        with waiting.attached_client('synthetic-client', lambda: True, patience=patience) as attached:
            assert attached is client
            assert client.poll() is None
    assert client.stdin.closed
    assert signals == [
        ('group', signal.SIGTERM, 0.0),
        ('group', signal.SIGKILL, pytest.approx(patience)),
        ('client', signal.SIGKILL, pytest.approx(patience)),
    ]
    assert all(0 < timeout <= patience for timeout in waits)
    assert sum(waits) == pytest.approx(patience if reaps_after_kill else 2 * patience)
    if reaps_after_kill:
        assert client.poll() == -signal.SIGKILL
    else:
        assert client.poll() is None
        assert error.value.cmd == 'synthetic-client'
        assert error.value.timeout == patience


def test_term_ignoring_client_is_reaped(tmp_path):
    ready = tmp_path / 'ready'
    with waiting.attached_client(command(ready, ignore=True), ready.exists, patience=0.1) as client:
        assert client.poll() is None
    assert client.poll() is not None


def test_owned_group_member_retires_even_when_the_wrapper_exits_first(tmp_path):
    ready, descendant = tmp_path / 'ready', tmp_path / 'descendant'
    with waiting.attached_client(command(ready, descendant=descendant), ready.exists, patience=0.1) as client:
        owned = int(descendant.read_text())
        assert alive(owned)
    assert client.poll() is not None
    assert not alive(owned)


def test_body_error_is_preserved_after_client_retirement(tmp_path):
    ready = tmp_path / 'ready'
    with pytest.raises(ValueError, match='synthetic body error'):
        with waiting.attached_client(command(ready), ready.exists) as client:
            raise ValueError('synthetic body error')
    assert client.poll() is not None


def test_attach_failure_keeps_all_output_and_retires_client(tmp_path, monkeypatch):
    ready = tmp_path / 'ready'
    text = 'synthetic-output-start-zzzz\n' + 'full ordinary output\n' * 100 + 'synthetic-output-end-zzzz'

    def fail_after_output(predicate):
        deadline = time.monotonic() + 5
        while not predicate():
            assert time.monotonic() < deadline
            time.sleep(0.005)
        raise AssertionError('synthetic attach failure')

    monkeypatch.setattr(waiting, 'wait_for', fail_after_output)
    with pytest.raises(AssertionError, match='synthetic attach failure') as error:
        with waiting.attached_client(command(ready, output=text), ready.exists):
            pytest.fail('an attach failure must not yield')
    captured = str(error.value)
    assert 'output:\n' in captured
    assert text.replace('\n', '\r\n') in captured or text in captured
