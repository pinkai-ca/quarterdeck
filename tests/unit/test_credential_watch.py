"""A renewal by replacement is back in the pool's place within seconds, by the service's own watch, not its next pass.

The proof runs the real service's watch thread on a ship copy with seats on the native program, never launched, and a
fake sign-in: nothing reaches a provider. The renewal is made as Claude Code makes it: a complete temporary beside the
name, renamed over it, which replaces the seat's link with a private file.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from quarterdeck import credential, harness as harnesses, ship as shipmod
from quarterdeck.service import Service
from quarterdeck.tmux import Tmux
from tests.conftest import ROOT

BOUND = 2.0  # seconds from the rename to every other seat's next read finding the renewal
RENEWED = "fake-renewed-7f3a"


def fake(token: str, expires_in: float) -> str:
    expiry = int((time.time() + expires_in) * 1000)
    return json.dumps({"claudeAiOauth": {"accessToken": token, "refreshToken": token, "expiresAt": expiry}})


@pytest.fixture
def crew(tmp_path: Path):
    """The drydock's three seats on the native program and one subscription pool, its sign-in fake; nothing launched."""
    folder = tmp_path / "ships" / "drydock"
    shutil.copytree(ROOT / "ships" / "drydock", folder)
    pool = tmp_path / "pool"
    pool.mkdir()
    text = (folder / "ship.toml").read_text()
    text = text.replace('[pools.fake]\nprovider = "synthetic"', f'[pools.fake]\nprovider = "anthropic"\nconfig_dir = "{pool}"')
    (folder / "ship.toml").write_text(text.replace('harness = "fake"', 'harness = "claude"').replace('model = "fake-1"', 'model = "sonnet"'))
    ship = shipmod.load(folder)
    source = credential.pool_file(ship.pools["fake"])
    source.write_text(fake("fake-old", 60))
    service = Service(ship, Tmux(socket=str(tmp_path / "private.sock")))
    for name in ship.seats:
        home = ship.runtime / "seats" / name / "home"
        home.mkdir(parents=True)
        (home / source.name).symlink_to(source)
    return service, source


def renew_by_replacement(name: Path, text: str) -> None:
    """As the program writes: a complete temporary in the same folder, then a rename over the name (the link)."""
    temporary = name.with_name(f"{name.name}.tmp.0123abcd")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(text)
    os.rename(temporary, name)


def read_token(name: Path) -> str | None:
    try:
        return json.loads(name.read_text())["claudeAiOauth"]["refreshToken"]
    except (OSError, ValueError, KeyError):
        return None


def watching(service: Service):
    stop = threading.Event()
    thread = threading.Thread(target=service.watch_credentials, args=(stop,), daemon=True)
    thread.start()
    return stop, thread


def test_every_other_seats_next_read_finds_a_renewal_by_replacement_within_the_bound(crew):
    service, source = crew
    service.credentials()  # one pass: it names the linked seats the watch looks at, and finds nothing detached
    seats, names = service.credential_watch
    assert sorted(path.parent.parent.name for path in names) == ["captain", "desk", "worker-1"]
    stop, thread = watching(service)
    try:
        renewer = service.ship.runtime / "seats" / "worker-1" / "home" / source.name
        others = [service.ship.runtime / "seats" / name / "home" / source.name for name in ("captain", "desk")]
        started = time.monotonic()
        renew_by_replacement(renewer, fake(RENEWED, 8 * 3600))
        assert not renewer.is_symlink(), "the rename replaced the seat's link with a private file"
        while not all(read_token(name) == RENEWED for name in others):
            assert time.monotonic() - started < BOUND, [read_token(name) for name in others]
            time.sleep(0.02)
        elapsed = time.monotonic() - started
    finally:
        stop.set()
        thread.join(timeout=2)
    assert elapsed < BOUND
    assert read_token(source) == RENEWED, "the pool's own file holds the renewal"
    assert renewer.is_symlink() and renewer.resolve() == source.resolve(), "the renewing seat is linked again"
    log = service.log_path.read_text()
    events = [json.loads(line) for line in log.splitlines()]
    assert any(e["event"] == "credential" and e.get("by") == "watch" and "reseeded from worker-1" in e["proof"] for e in events)
    assert RENEWED not in log and "fake-old" not in log, "no token value is logged"


def test_the_watch_handles_a_renewal_once_and_is_quiet_after(crew):
    service, source = crew
    service.credentials()
    stop, thread = watching(service)
    try:
        renew_by_replacement(service.ship.runtime / "seats" / "desk" / "home" / source.name, fake(RENEWED, 8 * 3600))
        deadline = time.monotonic() + BOUND
        while read_token(source) != RENEWED:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        time.sleep(4 * credential.WATCH_SECONDS)
        before = service.log_path.read_text()
        time.sleep(4 * credential.WATCH_SECONDS)
        assert service.log_path.read_text() == before, "nothing detached is left, so nothing more is done or logged"
    finally:
        stop.set()
        thread.join(timeout=2)


def test_a_stub_left_by_a_lost_login_is_relinked_and_never_installed(crew):
    service, source = crew
    service.credentials()
    stop, thread = watching(service)
    try:
        stub = service.ship.runtime / "seats" / "captain" / "home" / source.name
        renew_by_replacement(stub, "{}")
        deadline = time.monotonic() + BOUND
        while not stub.is_symlink():
            assert time.monotonic() < deadline
            time.sleep(0.02)
    finally:
        stop.set()
        thread.join(timeout=2)
    assert read_token(source) == "fake-old", "a stub never replaces the pool's sign-in"


def test_every_reseed_waits_for_the_one_lock(crew):
    service, source = crew
    renew_by_replacement(service.ship.runtime / "seats" / "desk" / "home" / source.name, fake(RENEWED, 8 * 3600))
    lock = service.ship.runtime / "credentials" / "reseed.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    held = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(held, fcntl.LOCK_EX)  # another process's reseed, holding the lock
    done = []
    worker = threading.Thread(target=lambda: done.append(credential.reseed(service.ship, service.resolved_seats())))
    worker.start()
    time.sleep(0.3)
    assert not done and read_token(source) == "fake-old", "no install while another reseed holds the lock"
    os.close(held)
    worker.join(timeout=5)
    assert done and read_token(source) == RENEWED


def test_a_program_whose_seats_hold_a_copy_is_left_to_the_pass(crew, monkeypatch):
    service, source = crew
    monkeypatch.setattr(harnesses.native_for("anthropic"), "credential_copy", True, raising=False)
    assert credential.linked_names(service.ship, service.resolved_seats()) == []
