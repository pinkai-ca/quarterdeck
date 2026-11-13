"""Ordinary two-process creation of a fresh test root, with a deterministic rendezvous."""
from pathlib import Path
import os
import selectors
import subprocess
import sys
import time


CREATOR = r'''
import os
from pathlib import Path
import sys

sys.path.insert(0, sys.argv[1])
from testrunner import layout

project, ready, release = Path(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
original = layout.open_child

def rendezvous(parent_fd, name, *args, **kwargs):
    if name == layout.ROOT_NAME:
        os.write(ready, b'R')
        if os.read(release, 1) != b'G':
            raise RuntimeError('creator rendezvous closed before release')
    return original(parent_fd, name, *args, **kwargs)

layout.open_child = rendezvous
root = layout.Root(project)
root.close()
'''


def test_concurrent_initial_ignore_creators(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    subprocess.run(['git', 'init', '-q', str(project)], check=True, capture_output=True)
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    processes = []
    try:
        for _ in range(2):
            processes.append(subprocess.Popen(
                [sys.executable, '-c', CREATOR, str(Path(__file__).resolve().parents[1]), str(project),
                 str(ready_write), str(release_read)], pass_fds=(ready_write, release_read),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True))
        # Both Root constructors have observed the missing root before either may make it. Their actual
        # mkdir, exclusive file creation and Git ignore check run unchanged against the same filesystem.
        with selectors.DefaultSelector() as selector:
            selector.register(ready_read, selectors.EVENT_READ)
            ready = b''
            deadline = time.monotonic() + 30
            while len(ready) < 2:
                remaining = deadline - time.monotonic()
                assert remaining > 0 and selector.select(remaining), 'creators did not rendezvous'
                part = os.read(ready_read, 2 - len(ready))
                assert part, 'creator closed its readiness pipe'
                ready += part
        assert ready == b'RR'
        assert os.write(release_write, b'GG') == 2
        for process in processes:
            output, _ = process.communicate(timeout=30)
            assert process.returncode == 0, output
        root = project / '.testruns'
        assert (root / '.gitignore').read_bytes() == b'*\n'
        assert sorted(path.name for path in root.iterdir()) == ['.gitignore']
    finally:
        for fd in (ready_read, ready_write, release_read, release_write):
            os.close(fd)
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
