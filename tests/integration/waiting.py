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
    """Own a terminal client. Its output is kept in a temporary file, embedded whole in an attach failure, and the
    file is removed on every exit."""
    client_env = os.environ.copy()
    inherited_term = client_env.get('TERM')
    client_env['TERM'] = 'xterm-256color'
    with tempfile.NamedTemporaryFile(prefix='tmux-client-', suffix='.log') as output:  # removed when closed
        client = subprocess.Popen(['script', '-qfc', command, '/dev/null'], stdin=subprocess.DEVNULL,
                                  stdout=output, stderr=subprocess.STDOUT, env=client_env)
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
                                 f'inherited TERM={inherited_term!r}, client TERM={client_env["TERM"]!r}, '
                                 f'output:\n{captured}') from error
