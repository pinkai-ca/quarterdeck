"""A process's start time proves its identity only while it has not exited.

servicectl.process_start and presentation.process_start read field 22 of /proc/<pid>/stat, the start time. An exited
process keeps that field while it is a zombie (Z) and for the instant of its reaping (X, or x before Linux 4.14), so an
ended service could pass for its old self. In an exited state both readers now give None, which matches no record.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from quarterdeck import presentation, procstat, servicectl

READERS = [servicectl.process_start, presentation.process_start]
START = '987654321'


def stat_line(state):
    # pid (comm) state ppid ... with the start time as the 20th field after the command
    fields = [state] + ['0'] * 18 + [START] + ['0'] * 10
    return f'4242 (synthetic prog) {" ".join(fields)}\n'


class FakePath:
    state = 'S'

    def __init__(self, path):
        self.path = str(path)

    def read_text(self):
        assert self.path == '/proc/4242/stat'
        return stat_line(FakePath.state)


@pytest.mark.parametrize('reader', READERS, ids=['servicectl', 'presentation'])
@pytest.mark.parametrize('state', sorted(procstat.EXITED))
def test_an_exited_state_proves_no_identity(monkeypatch, reader, state):
    module = servicectl if reader is servicectl.process_start else presentation
    monkeypatch.setattr(module, 'Path', FakePath)
    FakePath.state = state
    assert reader(4242) is None


@pytest.mark.parametrize('reader', READERS, ids=['servicectl', 'presentation'])
@pytest.mark.parametrize('state', ['R', 'S', 'D', 'T', 't', 'I', 'W'])
def test_a_living_state_gives_its_start(monkeypatch, reader, state):
    module = servicectl if reader is servicectl.process_start else presentation
    monkeypatch.setattr(module, 'Path', FakePath)
    FakePath.state = state
    assert reader(4242) == START


def test_a_real_exited_unreaped_process_no_longer_matches_its_recorded_start():
    child = subprocess.Popen([sys.executable, '-c', 'pass'])
    try:
        recorded = servicectl.process_start(child.pid)
        assert recorded is not None and presentation.process_start(child.pid) == recorded, 'alive: its start'
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if Path(f'/proc/{child.pid}/stat').read_text().rsplit(')', 1)[1].split()[0] == 'Z':
                break
            time.sleep(0.01)
        else:
            pytest.fail('the child never became a zombie')
        assert servicectl.process_start(child.pid) is None, 'exited, not yet reaped: no identity'
        assert presentation.process_start(child.pid) is None
    finally:
        child.wait()
    assert servicectl.process_start(child.pid) is None, 'reaped: gone'


def test_a_living_process_still_matches_and_the_seat_check_follows():
    from quarterdeck import seat
    import os
    mine = servicectl.process_start(os.getpid())
    assert mine is not None and presentation.process_start(os.getpid()) == mine
    child = subprocess.Popen([sys.executable, '-c', 'pass'])
    try:
        recorded = servicectl.process_start(child.pid)
        deadline = time.monotonic() + 10
        while Path(f'/proc/{child.pid}/stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z':
            assert time.monotonic() < deadline, 'the child never became a zombie'
            time.sleep(0.01)
        assert seat.process_start(child.pid) is None
    finally:
        child.wait()
    assert recorded is not None
