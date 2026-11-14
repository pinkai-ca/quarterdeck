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


def _unlink_refused(monkeypatch, refused: Path) -> None:
    """The earlier place's removal fails with PermissionError; every other path unlinks as usual."""
    original = Path.unlink

    def unlink(self, *args, **kwargs):
        if self == refused:
            raise PermissionError(13, 'Permission denied', str(self))
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', unlink)


def test_a_failed_release_of_the_earlier_service_lock_keeps_it_held_to_try_again(ship, monkeypatch):
    descriptor = servicectl.hold_legacy(ship.runtime)
    legacy = servicectl.legacy_lock_path(ship.runtime)
    with monkeypatch.context() as patched:
        _unlink_refused(patched, legacy)
        with pytest.raises(PermissionError):
            servicectl.release_legacy(ship.runtime, descriptor)
    assert legacy.exists() and servicectl._held(legacy) is True, 'still held: the earlier installer is not misled'
    servicectl.release_legacy(ship.runtime, descriptor)  # the same descriptor, still open
    assert not legacy.exists()


def test_the_service_survives_a_failed_release_and_tries_again_next_pass(ship, processes, monkeypatch):
    from types import SimpleNamespace
    import json
    from quarterdeck.service import Service

    installer = processes()
    installer.close()  # the earlier installer has exited
    svc = Service(ship, SimpleNamespace())
    svc.earlier_installer = (installer.pid, installer.start)
    svc.legacy_lock = servicectl.hold_legacy(ship.runtime)
    legacy = servicectl.legacy_lock_path(ship.runtime)

    def events() -> list[dict]:
        return [json.loads(line) for line in svc.log_path.read_text().splitlines()] if svc.log_path.exists() else []

    with monkeypatch.context() as patched:
        _unlink_refused(patched, legacy)
        svc.release_earlier_installer()  # must not raise: in tick() nothing would catch it
    failed = [e for e in events() if e.get('step') == 'release_earlier_installer']
    assert failed and failed[-1]['error'] == 'earlier-installer release failed'
    assert failed[-1]['kind'] == 'PermissionError'
    assert svc.legacy_lock is not None and servicectl._held(legacy) is True

    svc.release_earlier_installer()  # the next pass
    assert svc.legacy_lock is None and not legacy.exists()
    assert [e for e in events() if e['event'] == 'earlier-installer-done']


def _service_holding_for(ship, installer: tuple[int, str]):
    from types import SimpleNamespace
    from quarterdeck.service import Service

    svc = Service(ship, SimpleNamespace())
    svc.earlier_installer = installer
    svc.legacy_lock = servicectl.hold_legacy(ship.runtime)
    return svc


def _done(svc) -> list[dict]:
    import json
    lines = svc.log_path.read_text().splitlines() if svc.log_path.exists() else []
    return [event for event in map(json.loads, lines) if event['event'] == 'earlier-installer-done']


def test_the_earlier_service_lock_goes_on_the_first_pass_after_its_installer_has_exited(ship, processes):
    installer = processes()
    svc = _service_holding_for(ship, (installer.pid, installer.start))
    legacy = servicectl.legacy_lock_path(ship.runtime)
    svc.release_earlier_installer()  # a pass while the installer runs
    assert svc.legacy_lock is not None and servicectl._held(legacy) is True and not _done(svc)
    installer.close()  # its pid is gone: no reading, which never equals the recorded start
    assert servicectl.process_start(installer.pid) is None
    svc.release_earlier_installer()  # the next pass
    assert svc.legacy_lock is None and not legacy.exists()
    assert [event['pid'] for event in _done(svc)] == [installer.pid]


def test_the_earlier_service_lock_goes_when_the_installers_pid_runs_another_process(ship, processes):
    other = processes()  # the recorded pid, reused by a process with another start
    assert other.start is not None
    svc = _service_holding_for(ship, (other.pid, str(int(other.start) - 1)))
    svc.release_earlier_installer()
    assert svc.legacy_lock is None and not servicectl.legacy_lock_path(ship.runtime).exists()
    assert [event['pid'] for event in _done(svc)] == [other.pid]
