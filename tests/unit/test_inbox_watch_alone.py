"""The seat's registered watch, proved alone in a shell of its own under the launched program.

Where a record cannot name the watch's call, the screen's count of one shell stands for it, so the process table
must show the registered watch as that one shell's whole: the registered process has no child, its parent is a
shell whose only child it is, and that shell's parent is the launched program. A watch run straight under the
program, beside other work in one shell, or under a shell of a shell, is not proved, and neither is one that ended.

The watch is the real `qd inbox --wait`, run as the program's shell runs it, against a real drydock ship, so its
registration is the framework's own. The launched program is this test's own process.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import inbox_watch, post, trusted_files
from quarterdeck import ship as shipmod

QD = Path(sys.executable).parent / 'qd'  # this checkout's own qd, never an installed copy
SEAT = 'desk'
WATCH = f'{QD} inbox --wait'


class Case:
    def __init__(self, ship):
        self.ship, self.runtime = ship, ship.runtime
        self.seat = SimpleNamespace(name=SEAT, ship=ship)
        self.started: list[subprocess.Popen] = []
        (self.runtime / 'seats' / SEAT).mkdir(parents=True, exist_ok=True)
        self.launch(datetime.now(timezone.utc) - timedelta(seconds=60))

    def launch(self, at: datetime) -> None:
        """The launch record, where the service keeps it and trusts it: this test's own process is the program."""
        record = trusted_files.path(self.runtime, SEAT, trusted_files.LAUNCH)
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps(dict(pid=os.getpid(), at=at.isoformat())) + '\n')

    def run(self, argv) -> subprocess.Popen:
        env = {key: value for key, value in os.environ.items() if not key.startswith(('QD_', 'TMUX'))}
        env.update(QD_SHIP=str(self.ship.folder), QD_SEAT=SEAT)
        process = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   start_new_session=True)
        self.started.append(process)
        return process

    def registered(self, process) -> int:
        """Wait for the watch that ``process`` started to register; its process id."""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(f'the command ended before its watch registered: {process.communicate()}')
            try:
                proof = json.loads(inbox_watch.proof_path(self.runtime, SEAT).read_text())
                pid = int(post.watch_file(self.runtime, SEAT).read_text().split()[0])
                if proof['generation'] == inbox_watch.generation(self.runtime, SEAT) and inbox_watch.process(pid):
                    return pid
            except (OSError, ValueError, KeyError, IndexError):
                pass
            time.sleep(0.05)
        pytest.fail('the watch never registered')

    def stop(self, process) -> None:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()

    def close(self) -> None:
        for process in self.started:
            self.stop(process)


@pytest.fixture
def case(drydock, monkeypatch):
    monkeypatch.setenv('QD_SHIP', str(drydock))
    monkeypatch.delenv('QD_SEAT', raising=False)
    ship = shipmod.load(drydock)
    shipmod.fence(ship)
    built = Case(ship)
    yield built
    built.close()


def parent(pid):
    return inbox_watch.process(pid)[0]


def test_a_watch_in_a_shell_of_its_own_under_the_program_is_proved_alone(case):
    shell = case.run(['bash', '-c', f'{WATCH}; true'])  # the shell stays, as the program's own wrapper does
    pid = case.registered(shell)
    assert parent(pid) == shell.pid and parent(shell.pid) == os.getpid(), 'program, shell, watch'
    assert inbox_watch.children(shell.pid) == {pid} and inbox_watch.children(pid) == set()
    assert inbox_watch.alone(case.seat) is True
    called = datetime.now(timezone.utc)
    assert inbox_watch.current(case.seat, called) is False, 'a call after the registration is still no candidate'

    case.stop(shell)
    assert inbox_watch.alone(case.seat) is False, 'an ended watch proves nothing'


def test_a_watch_straight_under_the_program_is_not_a_shell_of_its_own(case):
    watch = case.run([str(QD), 'inbox', '--wait'])
    pid = case.registered(watch)
    assert pid == watch.pid and parent(pid) == os.getpid()
    assert inbox_watch.alone(case.seat) is False
    before = datetime.now(timezone.utc) - timedelta(seconds=30)
    assert inbox_watch.current(case.seat, before) is True, 'the record path, with its own candidate, is unchanged'


def test_a_watch_beside_other_work_in_one_shell_is_not_alone(case):
    shell = case.run(['bash', '-c', f'sleep 120 & {WATCH}; true'])
    pid = case.registered(shell)
    assert parent(pid) == shell.pid and len(inbox_watch.children(shell.pid)) == 2
    assert inbox_watch.alone(case.seat) is False


def test_a_watch_under_a_shell_of_a_shell_is_not_directly_under_the_program(case):
    outer = case.run(['bash', '-c', f"bash -c '{WATCH}; true'; true"])
    pid = case.registered(outer)
    inner = parent(pid)
    assert inner != outer.pid and parent(inner) == outer.pid and parent(outer.pid) == os.getpid()
    assert inbox_watch.children(inner) == {pid}
    assert inbox_watch.alone(case.seat) is False


def test_a_registration_older_than_the_launch_is_not_this_programs_watch(case):
    shell = case.run(['bash', '-c', f'{WATCH}; true'])
    case.registered(shell)
    assert inbox_watch.alone(case.seat) is True
    case.launch(datetime.now(timezone.utc) + timedelta(seconds=30))
    assert inbox_watch.alone(case.seat) is False, 'a new launch is another binding and another time'


def test_the_kernels_list_of_children_is_read_or_said_unknown():
    assert inbox_watch.children(os.getpid()) is not None
    child = subprocess.Popen(['sleep', '30'])
    try:
        assert child.pid in inbox_watch.children(os.getpid())
        assert inbox_watch.children(child.pid) == set()
    finally:
        child.kill()
        child.wait()
    for value in (0, -1, None, '1', 2 ** 22 + 12345):
        assert inbox_watch.children(value) is None, value
