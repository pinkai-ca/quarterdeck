"""Waiting on a private tmux server: a condition polled until its deadline."""

from contextlib import contextmanager
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

from quarterdeck.harness.json_record import RecordError
from quarterdeck.procstat import EXITED
from quarterdeck.tmux import PrivateTmuxError

RECORD_CHANGED = "record changed while being read"  # the one record refusal a fresh read can cure


def wait_for(predicate, seconds: float = 10.0):
    deadline = time.monotonic() + seconds
    last_read_error = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
        except PrivateTmuxError:
            value = None  # retry fresh evidence within the same existing deadline
        except RecordError as error:
            if str(error) != RECORD_CHANGED:
                raise  # every other record refusal is a finding, never retried
            last_read_error = error  # a concurrent write: read again within the same deadline
            value = None
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not met in time") from last_read_error


def await_drop(text: str, *, seconds: float = 15) -> str:
    """A post returns before the service's tick files it. Wait for that file when a service is up."""
    short = text.split(" ", 1)[0] if text else ""
    ship = os.environ.get("QD_SHIP")
    if not short or not ship:
        return text
    runtime = Path(ship) / "runtime"
    from quarterdeck import post, service
    from quarterdeck.ship import ShipError
    if not service.alive(runtime):
        return text
    store = post.Store(runtime)

    def ready():
        try:
            message = store.get(short)
        except ShipError:
            return None
        # The drop is removed only after the store write, and a group's copies with it.
        folder = runtime / "post" / "drop" / message.sender
        if (folder / f"{message.id}.json").exists() or (folder / f"{message.id}.group.json").exists():
            return None
        return True

    wait_for(ready, seconds)
    return text


@contextmanager
def attached_client(command, attached, *, patience=5.0):
    """Own a terminal client. Its output is kept in a temporary file, embedded whole in an attach failure, and the
    file is removed on every exit."""
    client_env = os.environ.copy()
    inherited_term = client_env.get('TERM')
    client_env['TERM'] = 'xterm-256color'
    with tempfile.NamedTemporaryFile(prefix='tmux-client-', suffix='.log') as output:  # removed when closed
        client = subprocess.Popen(['script', '-qfc', command, '/dev/null'], stdin=subprocess.DEVNULL,
                                  stdout=output, stderr=subprocess.STDOUT, env=client_env, start_new_session=True)

        def process(pid):
            try:
                fields = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
                return int(fields[19]), int(fields[1]), int(fields[2]), int(fields[3]), fields[0]
            except OSError:
                return None

        def children(pid):
            try:
                return [int(value) for value in (Path('/proc') / str(pid) / 'task' / str(pid) / 'children').read_text().split()]
            except OSError:
                return []

        def terminal_group():
            groups = []
            for pid in children(client.pid):
                state = process(pid)
                if state is not None and state[1:4] == (client.pid, pid, pid):
                    groups.append((pid, state[0]))
            return groups[0] if len(groups) == 1 else None

        def group_members(group):
            members, pending = {}, children(client.pid)
            while pending:
                pid = pending.pop()
                state = process(pid)
                if state is None:
                    continue
                if state[2:4] == (group[0], group[0]):
                    members[pid] = state[0]
                    pending.extend(children(pid))
            return members

        def active(members, group):
            for pid, stamp in members.items():
                state = process(pid)
                if state is not None and state[0] == stamp and state[2:4] == (group[0], group[0]) and state[4] not in EXITED:
                    return True
            return False

        def signal_group(group, members, signum):
            if group is not None and active(members, group):
                try:
                    os.killpg(group[0], signum)
                except ProcessLookupError:
                    pass

        def retired(group, members, seconds):
            deadline = time.monotonic() + seconds
            while True:
                done = client.poll() is not None
                if done and (group is None or not active(members, group)):
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                if done:
                    time.sleep(min(0.01, remaining))
                else:
                    try:
                        client.wait(timeout=min(0.05, remaining))
                    except subprocess.TimeoutExpired:
                        pass

        ready = False
        group = None
        members = {}
        try:
            try:
                wait_for(attached)
                group = terminal_group()
                if group is not None:
                    members = group_members(group)
                ready = True
                yield client
            finally:
                # Signal the terminal command, rather than asking script to spend its
                # fixed shutdown grace. Track only this group's original processes.
                if group is None:
                    group = terminal_group()
                state = process(group[0]) if group is not None else None
                valid = (state is not None and state[0] == group[1]
                         and state[1:4] == (client.pid, group[0], group[0]))
                if valid:
                    members.update(group_members(group))
                elif group is None or not active(members, group):
                    group = None
                if group is not None and active(members, group):
                    signal_group(group, members, signal.SIGTERM)
                elif client.poll() is None:
                    client.terminate()
                if not retired(group, members, patience):
                    signal_group(group, members, signal.SIGKILL)
                    client.kill()
                    if not retired(group, members, patience):
                        raise subprocess.TimeoutExpired(command, patience)
        except AssertionError as error:
            if ready:
                raise
            output.seek(0)
            captured = output.read().decode('utf-8', errors='replace')
            raise AssertionError(f'{error}; tmux attach script exit={client.returncode}, '
                                 f'inherited TERM={inherited_term!r}, client TERM={client_env["TERM"]!r}, '
                                 f'output:\n{captured}') from error
