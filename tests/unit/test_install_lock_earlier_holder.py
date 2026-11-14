"""Who may hold the install lock's earlier place while this code starts: the recorded installer alone.

An installer of the earlier code holds ``runtime/install.lock`` for its whole sync and starts this code's service
with its start token. Its journal records its process and that process's start before it does. Only that process,
holding that very file as the kernel lists it, is accepted; any other holder of the earlier place is refused.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from quarterdeck import install, servicectl, ship as shipmod

HOLDER = r'''
import fcntl, sys
handle = open(sys.argv[1], 'a')
fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
print('held', flush=True)
sys.stdin.read()
'''
IDLE = 'import sys; print("idle", flush=True); sys.stdin.read()'


class Process:
    """A stand-in process: holding a lock file for its life, or holding nothing."""

    def __init__(self, path: Path | None):
        argv = [sys.executable, '-I', '-c', HOLDER, str(path)] if path else [sys.executable, '-I', '-c', IDLE]
        self.child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        assert self.child.stdout.readline().strip() in ('held', 'idle')
        self.pid = self.child.pid
        self.start = servicectl.process_start(self.pid)

    def journal(self) -> dict:
        return dict(phase='target_ready', token='a' * 32, owner_pid=self.pid, owner_start=self.start)

    def close(self) -> None:
        self.child.stdin.close()
        self.child.wait(timeout=10)


@pytest.fixture
def processes():
    started = []

    def make(path: Path | None = None) -> Process:
        started.append(Process(path))
        return started[-1]
    yield make
    for process in started:
        process.close()


@pytest.fixture
def ship(drydock: Path) -> shipmod.Ship:
    loaded = shipmod.load(drydock)
    loaded.runtime.mkdir(parents=True, exist_ok=True)
    return loaded


def test_the_recorded_earlier_installer_holding_the_earlier_place_is_known(ship, processes):
    installer = processes(ship.runtime / install.LEGACY_INSTALL_LOCK)
    assert install._parent_holds_install_lock(ship, installer.journal()) == 'earlier'


def test_a_current_installer_holding_this_codes_place_is_known_as_before(ship, processes):
    path = install.install_lock_path(ship.runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    installer = processes(path)
    assert install._parent_holds_install_lock(ship, installer.journal()) == 'current'


def test_another_holder_of_the_earlier_place_is_never_the_installer(ship, processes):
    """The recorded installer is alive but holds nothing; an older operation holds the earlier place."""
    installer = processes()
    processes(ship.runtime / install.LEGACY_INSTALL_LOCK)
    assert install._parent_holds_install_lock(ship, installer.journal()) is None


def test_a_recorded_installer_that_is_not_the_live_process_is_not_accepted(ship, processes):
    installer = processes(ship.runtime / install.LEGACY_INSTALL_LOCK)
    assert install._parent_holds_install_lock(ship, dict(installer.journal(), owner_start='1')) is None
    assert install._parent_holds_install_lock(ship, dict(installer.journal(), owner_pid=installer.pid + 1)) is None


def test_a_link_at_the_earlier_place_names_no_holder(ship, processes, tmp_path):
    elsewhere = tmp_path / 'elsewhere.lock'
    installer = processes(elsewhere)
    (ship.runtime / install.LEGACY_INSTALL_LOCK).symlink_to(elsewhere)
    assert install._parent_holds_install_lock(ship, installer.journal()) is None


def test_a_concurrent_operation_of_the_earlier_code_is_still_refused(ship, processes):
    processes(ship.runtime / install.LEGACY_INSTALL_LOCK)
    with pytest.raises(shipmod.ShipError, match='a copy operation of the earlier code'):
        with install.install_lock(ship, blocking=False):
            pass
    assert (ship.runtime / install.LEGACY_INSTALL_LOCK).exists(), 'a held earlier lock is left to its holder'


def test_the_earlier_service_lock_is_held_for_the_installer_and_let_go_after(ship, processes):
    descriptor = servicectl.hold_legacy(ship.runtime)
    legacy = servicectl.legacy_lock_path(ship.runtime)
    assert servicectl._held(legacy) is True, 'the earlier installer sees its service'
    with pytest.raises(shipmod.ShipError, match='earlier code'):
        servicectl.hold_legacy(ship.runtime)
    servicectl.release_legacy(ship.runtime, descriptor)
    assert not legacy.exists()


def test_the_earlier_service_lock_held_by_another_refuses_the_start(ship, processes):
    processes(servicectl.legacy_lock_path(ship.runtime))
    with pytest.raises(shipmod.ShipError, match='earlier code'):
        servicectl.hold_legacy(ship.runtime)
