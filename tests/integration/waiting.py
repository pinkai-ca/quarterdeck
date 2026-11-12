"""Waiting on a private tmux server: a condition polled until its deadline."""

from contextlib import contextmanager
import os
import subprocess
import tempfile
import time

from quarterdeck.tmux import PrivateTmuxError


def wait_for(predicate, seconds: float = 10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            value = predicate()
        except PrivateTmuxError:
            value = None  # retry fresh evidence within the same existing deadline
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


@contextmanager
def attached_client(command, attached):
    """Own a terminal client and keep its output in the test run's temporary folder."""
    with tempfile.NamedTemporaryFile(prefix='tmux-client-', suffix='.log', delete=False) as output:
        client = subprocess.Popen(['script', '-qfc', command, '/dev/null'], stdin=subprocess.DEVNULL,
                                  stdout=output, stderr=subprocess.STDOUT)
        ready = False
        try:
            try:
                wait_for(attached)
                ready = True
                yield client
            finally:
                client.terminate()
                try:
                    client.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    client.kill()
                    client.wait(timeout=5)
        except AssertionError as error:
            if ready:
                raise
            output.seek(0)
            captured = output.read().decode('utf-8', errors='replace')
            raise AssertionError(f'{error}; tmux attach script exit={client.returncode}, '
                                 f'TERM={os.environ.get("TERM")!r}, output={output.name}:\n{captured}') from error
