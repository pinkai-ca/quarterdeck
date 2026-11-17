"""A test run's drive writes count the whole tree it waited for, not the runner alone, and say so.

A child process writes a known amount through to the real drive with fsync and is waited, as pytest is by the
runner and its workers are by pytest; the figure writes_since reports holds it. Written on the real drive only: tmpfs
and ramfs charge no write, so a run whose scratch is in memory writes in the project's disk temp folder instead.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tests.testrunner import layout, scratch

SIZE = 8 << 20  # 8 MiB, a whole number of pages: the kernel charges writes a page at a time
WRITE = ("import os, sys\n"
         "f = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)\n"
         "os.write(f, bytes(int(sys.argv[2])))\n"
         "os.fsync(f)\n"
         "os.close(f)\n")


def on_the_drive(tmp_path):
    """A fresh folder on the real drive: tmp_path when it is one, else under the project's disk temp folder."""
    from tests.conftest import socket_project
    candidates = [tmp_path]
    try:
        candidates.append(socket_project() / layout.ROOT_NAME / layout.PLAIN_TEMP)
    except (OSError, subprocess.CalledProcessError):
        pass
    for base in candidates:
        try:
            if base.is_dir() and not scratch.in_memory(base):
                return Path(tempfile.mkdtemp(prefix='drive-writes-', dir=base))
        except OSError:
            continue
    pytest.skip('no folder on the real drive is writable here (a jail shows the project read-only)')


def test_a_waited_childs_writes_to_the_drive_are_in_the_trees_figure_and_its_scope_says_so(tmp_path):
    from tests.conftest import socket_project
    folder = on_the_drive(tmp_path)
    try:
        project = socket_project()
        before, io_before = scratch.diskstats(project), scratch.process_io()
        subprocess.run([sys.executable, '-I', '-c', WRITE, str(folder / 'synthetic-blob'), str(SIZE)], check=True)
        found = scratch.writes_since(project, before, io_before)
    finally:
        shutil.rmtree(folder)
    assert SIZE <= found['process_write_bytes'] < SIZE + (4 << 20), \
        "the child's fsynced writes, counted once it was waited, within the file system's own extra writes"
    assert 'descendant' in found['process_scope'] and 'tmpfs' in found['process_scope'], \
        'the figure names its scope, as the device figure does'
    assert 'machine-wide' in found['device_scope']


def test_a_child_not_yet_waited_is_not_in_it_and_the_scope_says_so(tmp_path):
    folder = on_the_drive(tmp_path)
    try:
        io_before = scratch.process_io()
        child = subprocess.Popen([sys.executable, '-I', '-c', WRITE, str(folder / 'synthetic-blob'), str(SIZE)])
        try:
            os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)  # ended, and still not waited
            assert scratch.process_io().get('write_bytes', 0) - io_before.get('write_bytes', 0) < SIZE
        finally:
            child.wait()
        assert scratch.process_io().get('write_bytes', 0) - io_before.get('write_bytes', 0) >= SIZE
    finally:
        shutil.rmtree(folder)
    assert 'never waited' in scratch.PROCESS_SCOPE
