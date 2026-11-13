"""Deterministic interrupts in a synthetic waiter's registration lifetime; no terminal is opened."""

from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


WAITER = textwrap.dedent('''\
    from datetime import datetime
    from contextlib import contextmanager
    from pathlib import Path
    import os, signal, sys
    from quarterdeck import durable, inbox_watch, post

    runtime, stage = Path(sys.argv[1]), sys.argv[2]
    name = 'seat-synthetic'
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT})
    expected_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    original_write, original_register, original_retire = durable.replace_file, inbox_watch.register, inbox_watch.retire
    original_admission = post.admission
    returned = False

    @contextmanager
    def admission(*args, **kwargs):
        if stage == 'before-admission':
            os.kill(os.getpid(), signal.SIGINT)
        with original_admission(*args, **kwargs):
            yield

    def write(path, text, **kwargs):
        original_write(path, text, **kwargs)
        if stage == 'at-publication' and path == post.watch_file(runtime, name):
            os.kill(os.getpid(), signal.SIGINT)

    def register(*args, **kwargs):
        global returned
        registration = original_register(*args, **kwargs)
        if stage.startswith('before-helper-return'):
            os.kill(os.getpid(), signal.SIGINT)
        returned = True
        return registration

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            if returned and stage == 'after-helper-return':
                os.kill(os.getpid(), signal.SIGINT)
            return super().now(tz)

    def retire(*args, **kwargs):
        original_retire(*args, **kwargs)
        if stage.endswith('cleanup-error'):
            raise OSError('synthetic cleanup error')

    durable.replace_file, inbox_watch.register, inbox_watch.retire = write, register, retire
    post.admission = admission
    post.datetime = Clock
    try:
        result = post.wait_batch(runtime, name, stop_after=0)
    except KeyboardInterrupt:
        print('interrupted')
    else:
        assert result is None
        print('completed')
    assert signal.pthread_sigmask(signal.SIG_BLOCK, set()) == expected_mask
''')


@pytest.mark.parametrize('stage', ['at-publication', 'before-helper-return', 'after-helper-return',
                                  'before-helper-return-cleanup-error'])
def test_interrupted_registration_retires_before_any_wait_body(tmp_path: Path, stage: str):
    runtime = tmp_path / 'runtime-synthetic'
    result = subprocess.run([sys.executable, '-c', WAITER, str(runtime), stage],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'interrupted'
    assert not (runtime / 'seats/seat-synthetic/watch').exists()
    assert not (runtime / 'seats/seat-synthetic/watch-proof.json').exists()
    assert (runtime / 'seats/seat-synthetic/watch-generation').is_file()


def test_retirement_error_preserves_the_primary_wait_result(tmp_path: Path):
    runtime = tmp_path / 'runtime-synthetic'
    result = subprocess.run([sys.executable, '-c', WAITER, str(runtime), 'cleanup-error'],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'completed'
    assert 'inbox watch retirement unconfirmed' in result.stderr
    assert not (runtime / 'seats/seat-synthetic/watch').exists()
    assert not (runtime / 'seats/seat-synthetic/watch-proof.json').exists()


def test_interrupt_before_admission_publishes_no_registration(tmp_path: Path):
    runtime = tmp_path / 'runtime-synthetic'
    result = subprocess.run([sys.executable, '-c', WAITER, str(runtime), 'before-admission'],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'interrupted'
    assert not (runtime / 'seats/seat-synthetic/watch').exists()
    assert not (runtime / 'seats/seat-synthetic/watch-proof.json').exists()
    assert not (runtime / 'seats/seat-synthetic/watch-generation').exists()
