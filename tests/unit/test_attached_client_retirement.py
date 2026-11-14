"""Real-process retirement of the ordinary terminal-client helper."""

from pathlib import Path
import shlex
import sys
import time

import pytest

from quarterdeck.procstat import EXITED
from tests.integration import waiting


def command(ready, *, ignore=False, descendant=None, output=""):
    code = "import pathlib, signal, subprocess, sys, time\n"
    if ignore:
        code += "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
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


def test_ready_client_retires_without_the_script_shutdown_delay(tmp_path):
    ready = tmp_path / 'ready'
    with waiting.attached_client(command(ready), ready.exists) as client:
        assert client.poll() is None
        before = time.monotonic()
    assert client.poll() is not None
    assert time.monotonic() - before < 1.5


def test_nested_clients_retire_independently(tmp_path):
    outer_ready, inner_ready = tmp_path / 'outer', tmp_path / 'inner'
    with waiting.attached_client(command(outer_ready), outer_ready.exists) as outer:
        with waiting.attached_client(command(inner_ready), inner_ready.exists) as inner:
            before = time.monotonic()
        assert time.monotonic() - before < 1.5
        assert inner.poll() is not None
        assert outer.poll() is None
    assert outer.poll() is not None


def test_force_retirement_stays_bounded_by_the_explicit_window(tmp_path):
    ready = tmp_path / 'ready'
    with waiting.attached_client(command(ready, ignore=True), ready.exists, patience=0.1) as client:
        before = time.monotonic()
    elapsed = time.monotonic() - before
    assert client.poll() is not None
    assert 0.07 <= elapsed < 1.5


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
