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
from datetime import datetime, timezone
from pathlib import Path

import pytest

from quarterdeck import credential, harness as harnesses, sandbox, ship as shipmod
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


def test_a_reseed_waits_for_the_one_lock_and_skips_the_round_past_its_bound(crew):
    service, source = crew
    renew_by_replacement(service.ship.runtime / "seats" / "desk" / "home" / source.name, fake(RENEWED, 8 * 3600))
    lock = service.ship.runtime / sandbox.LOCKS / "credential-reseed.lock"
    lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    held = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(held, fcntl.LOCK_EX)  # another process's reseed, or a stuck one, holding the lock
    try:
        started = time.monotonic()
        lines = credential.reseed(service.ship, service.resolved_seats(), wait=0.3)
        assert time.monotonic() - started < 2, "the wait is bounded"
        assert len(lines) == 1 and lines[0].startswith(credential.SKIPPED), lines
        assert read_token(source) == "fake-old", "no install while another reseed holds the lock"
        done = []
        worker = threading.Thread(target=lambda: done.append(credential.reseed(service.ship, service.resolved_seats())))
        worker.start()
        time.sleep(0.3)
        assert not done
    finally:
        os.close(held)
    worker.join(timeout=5)
    assert done and read_token(source) == RENEWED, "released within the bound, the waiting reseed installs"


def test_the_watch_retries_a_skipped_round_on_its_next_tick(crew):
    service, source = crew
    service.credentials()
    lock = service.ship.runtime / sandbox.LOCKS / "credential-reseed.lock"
    lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    held = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(held, fcntl.LOCK_EX)
    stop, thread = watching(service)
    try:
        renew_by_replacement(service.ship.runtime / "seats" / "desk" / "home" / source.name, fake(RENEWED, 8 * 3600))
        deadline = time.monotonic() + 10
        while credential.SKIPPED not in (service.log_path.read_text() if service.log_path.exists() else ""):
            assert time.monotonic() < deadline
            time.sleep(0.05)
        os.close(held)
        held = None
        deadline = time.monotonic() + 2 * credential.RESEED_WAIT + 2
        while read_token(source) != RENEWED:
            assert time.monotonic() < deadline
            time.sleep(0.05)
    finally:
        if held is not None:
            os.close(held)
        stop.set()
        thread.join(timeout=5)


def test_a_copy_as_the_pools_is_not_due_and_takes_no_lock(crew, monkeypatch):
    service, source = crew
    monkeypatch.setattr(type(harnesses.native_for("anthropic")), "credential_copy", True, raising=False)
    for name in service.ship.seats:
        copy = service.ship.runtime / "seats" / name / "home" / source.name
        copy.unlink()
        shutil.copy2(source, copy)  # as the framework makes a copy: the pool's bytes and times
    lock = service.ship.runtime / sandbox.LOCKS / "credential-reseed.lock"
    assert credential.reseed(service.ship, service.resolved_seats()) == []
    assert not lock.exists(), "nothing due, so no lock taken on a pass"
    os.utime(copy, ns=(time.time_ns(), time.time_ns() + 10**9))  # one seat's program refreshed its copy
    assert credential._due(service.ship, service.resolved_seats())


def test_a_program_whose_seats_hold_a_copy_is_left_to_the_pass(crew, monkeypatch):
    service, source = crew
    monkeypatch.setattr(type(harnesses.native_for("anthropic")), "credential_copy", True, raising=False)
    assert credential.linked_names(service.ship, service.resolved_seats()) == []


RETIRED = "check-claude-ab12"  # a temporary check seat's form; never declared, so a later pass cannot rebuild it
SENTINEL = "fake-sentinel-token-5c1e"


def held_lock(service):
    lock = service.ship.runtime / sandbox.LOCKS / "credential-reseed.lock"
    lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    held = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(held, fcntl.LOCK_EX)  # another reseed holding the lock past the seat's wait
    return held


def retire_temporary_seat(service, source, *, renewed: bool, monkeypatch, sandbox: bool | None = None, plant=None):
    """A temporary seat on the pool taken down by its own teardown while another reseed holds the lock.

    ``sandbox=True`` makes it a jailed seat; ``plant`` is (name, text), a file its jail left in its home."""
    from contextlib import ExitStack
    from dataclasses import replace
    from quarterdeck import seat as seatmod

    ship = service.ship
    spec = shipmod.SeatSpec(RETIRED, "qd-" + RETIRED, "worker", "claude", ("fake",), cwd=ship.workspace,
                            sandbox=sandbox)
    treated = replace(ship, sessions={**ship.sessions, spec.session: shipmod.Session(spec.session, spec.session)},
                      seats={**ship.seats, RETIRED: spec})
    seat = seatmod.Seat(treated, spec, service.tmux)
    seat.home.mkdir(parents=True)
    name = seat.home / source.name
    name.symlink_to(source)
    if renewed:
        renew_by_replacement(name, fake(RENEWED, 8 * 3600))  # its program renewed: the only live token is here
    if plant is not None:
        (seat.home / plant[0]).write_text(plant[1])
    monkeypatch.setattr(credential, "DOWN_WAIT", 0.2)
    held = held_lock(service)
    try:
        with ExitStack() as cleanup:
            seatmod._teardown(cleanup, treated, seat, service.tmux, [], "synthetic check", lambda: None,
                              down=lambda: None)
    finally:
        os.close(held)
    return seat


def test_a_skipped_reseed_keeps_a_renewal_in_a_retired_seats_home_names_it_and_a_later_pass_settles_it(
        crew, monkeypatch):
    from quarterdeck import seat as seatmod
    service, source = crew
    seat = retire_temporary_seat(service, source, renewed=True, monkeypatch=monkeypatch)
    assert read_token(seat.home / source.name) == RENEWED, "the home and its renewal are kept"
    assert read_token(source) == "fake-old", "the skipped reseed installed nothing"
    record = service.ship.runtime / credential.HELD / f"{RETIRED}.json"
    assert record.is_file()
    words = seatmod.leftover_checks(service.ship)
    assert any(RETIRED in line and "a renewal held in a retired seat's home" in line for line in words), words
    service.credentials()  # the service's next pass, with the lock free
    assert read_token(source) == RENEWED, "the pool takes the renewal from the kept home"
    assert not seat.folder.exists() and not seat.trusted.exists() and not record.exists()
    assert seatmod.leftover_checks(service.ship) == []
    log = service.log_path.read_text()
    events = [json.loads(line) for line in log.splitlines()]
    assert any(e["event"] == "credential" and e.get("by") == "held" and f"reseeded from {RETIRED}" in e["proof"]
               for e in events), events
    assert RENEWED not in log and "fake-old" not in log, "no token value is logged"


def test_a_skipped_reseed_with_no_renewal_still_removes_the_home(crew, monkeypatch):
    from quarterdeck import seat as seatmod
    service, source = crew
    seat = retire_temporary_seat(service, source, renewed=False, monkeypatch=monkeypatch)
    assert not seat.folder.exists(), "a link left as a link holds nothing to keep"
    assert not (service.ship.runtime / credential.HELD / f"{RETIRED}.json").exists()
    assert seatmod.leftover_checks(service.ship) == []
    assert read_token(source) == "fake-old"


def test_a_failing_watch_logs_each_cause_once_and_its_clearing_once(crew, monkeypatch):
    import errno
    service, source = crew
    service.credentials()
    renew_by_replacement(service.ship.runtime / "seats" / "desk" / "home" / source.name, fake(SENTINEL, 8 * 3600))
    real = credential.reseed
    calls = []
    cause = {"error": lambda: OSError(errno.EACCES, f"denied reading {SENTINEL}", f"/synthetic/{SENTINEL}")}

    def failing(*args, **kwargs):
        calls.append(1)
        error = cause["error"]()
        if error is None:
            return real(*args, **kwargs)
        raise error

    monkeypatch.setattr(credential, "reseed", failing)
    monkeypatch.setattr(credential, "WATCH_SECONDS", 0.002)
    stop, thread = watching(service)

    def ticks(count):
        target = len(calls) + count
        deadline = time.monotonic() + 30
        while len(calls) < target:
            assert time.monotonic() < deadline, len(calls)
            time.sleep(0.01)

    try:
        ticks(100)  # the same failure, a hundred times
        cause["error"] = lambda: ValueError(f"bad token {SENTINEL}")
        ticks(20)  # a new cause
        cause["error"] = lambda: OSError(errno.EACCES, f"denied reading {SENTINEL}")
        ticks(20)  # the first cause again, while still failing: already said
        cause["error"] = lambda: None
        deadline = time.monotonic() + 10
        while read_token(source) != SENTINEL:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        time.sleep(20 * 0.002 + 0.2)
    finally:
        stop.set()
        thread.join(timeout=5)
    log = service.log_path.read_text()
    events = [json.loads(line) for line in log.splitlines()]
    failures = [e for e in events if e["event"] == "error" and e.get("step") == "credential-watch"]
    assert [e["cause"] for e in failures] == ["PermissionError EACCES", "ValueError"], failures
    cleared = [e for e in events if e["event"] == "credential-watch-cleared"]
    assert len(cleared) == 1 and cleared[0]["causes"] == ["PermissionError EACCES", "ValueError"], cleared
    assert SENTINEL not in log and "synthetic" not in log, "no token value nor its path in any log line"


@pytest.mark.parametrize("name", [".x", "a\\b"])
def test_a_held_record_naming_a_hidden_or_path_like_seat_is_unreadable_named_and_never_acted_on(crew, name):
    """The record's name is an rmtree's folder under runtime/seats: it must pass a seat folder's own name rule."""
    from quarterdeck import seat as seatmod
    service, source = crew
    folder = service.ship.runtime / "seats" / name
    (folder / "home").mkdir(parents=True)
    renew_by_replacement(folder / "home" / source.name, fake(RENEWED, 8 * 3600))
    held = service.ship.runtime / credential.HELD
    held.mkdir(parents=True)
    record = held / f"{name}.json"
    record.write_text(json.dumps({"seat": name, "pool": "fake"}) + "\n")
    service.credentials()  # the pass that would settle a readable record
    assert folder.is_dir() and read_token(folder / "home" / source.name) == RENEWED, "nothing of it removed"
    assert record.is_file() and read_token(source) == "fake-old", "never acted on"
    words = seatmod.leftover_checks(service.ship)
    assert words == [f"{name}: a held renewal's record is unreadable; inspect runtime/{credential.HELD}"], words


GROK_OLD, GROK_PLANTED, GROK_READER = "fake-grok-old-1d2e", "fake-grok-planted-9a8b", "fake-grok-reader-4c5d"


def grok_grant(token: str, expires_in: float) -> str:
    expiry = datetime.fromtimestamp(time.time() + expires_in, timezone.utc).isoformat()
    return json.dumps({"synthetic-grant": {"refresh_token": token, "expires_at": expiry}})


@pytest.fixture
def usage_reader(crew):
    """The crew beside a pool whose program keeps a private usage home: a usage reader joins every reseed of the ship,
    and here its copy differs from the pool's, so it is due."""
    service, source = crew
    folder = service.ship.folder
    grok_pool = folder.parent.parent / "grok-pool"
    grok_pool.mkdir()
    text = (folder / "ship.toml").read_text()
    (folder / "ship.toml").write_text(text + f'\n[pools.grok]\nprovider = "xai"\nconfig_dir = "{grok_pool}"\n'
                                      '\n[pools.served]\nprovider = "local"\nendpoint = "http://127.0.0.1:9/v1"\n')
    service.ship = shipmod.load(folder)
    grok_source = credential.pool_file(service.ship.pools["grok"])
    grok_source.write_text(grok_grant(GROK_OLD, 3600))
    reader = credential.usage_home(service.ship, service.ship.pools["grok"])
    reader.mkdir(parents=True)
    (reader / grok_source.name).write_text(grok_grant(GROK_READER, 1800))  # older than the pool's: due, never installed
    assert any(p.usage_reader and p.pool == "grok" for p in credential.participants(service.ship, []))
    return service, source, grok_source


def test_a_jailed_temporary_seat_beside_a_due_usage_reader_has_no_home_kept_and_gives_no_pool_a_sign_in(
        usage_reader, monkeypatch):
    service, source, grok_source = usage_reader
    before = grok_source.read_bytes()
    planted = (grok_source.name, grok_grant(GROK_PLANTED, 8 * 3600))  # newer than the pool's: what a jail would want in
    seat = retire_temporary_seat(service, source, renewed=False, monkeypatch=monkeypatch, sandbox=True, plant=planted)
    assert not (service.ship.runtime / credential.HELD / f"{RETIRED}.json").exists(), "no record for a refused seat"
    assert not seat.folder.exists(), "its home removed as before"
    service.credentials()  # the next pass: nothing of the jailed seat's is judged for any pool
    assert grok_source.read_bytes() == before and read_token(source) == "fake-old", "no pool's file changed"
    log = service.log_path.read_text() if service.log_path.exists() else ""
    assert GROK_PLANTED not in log and GROK_OLD not in log and GROK_READER not in log


def test_a_source_seat_beside_a_due_usage_reader_records_its_own_pool_and_is_settled(usage_reader, monkeypatch):
    service, source, grok_source = usage_reader
    seat = retire_temporary_seat(service, source, renewed=True, monkeypatch=monkeypatch)
    record = service.ship.runtime / credential.HELD / f"{RETIRED}.json"
    assert json.loads(record.read_text()) == {"seat": RETIRED, "pool": "fake"}, "its own participant's pool"
    service.credentials()
    assert read_token(source) == RENEWED and not seat.folder.exists() and not record.exists()
    log = service.log_path.read_text()
    assert RENEWED not in log and GROK_READER not in log


def test_a_held_record_naming_an_endpoint_pool_is_unreadable_and_never_acted_on(usage_reader):
    from quarterdeck import seat as seatmod
    service, source, grok_source = usage_reader
    folder = service.ship.runtime / "seats" / RETIRED
    (folder / "home").mkdir(parents=True)
    held = service.ship.runtime / credential.HELD
    held.mkdir(parents=True)
    (held / f"{RETIRED}.json").write_text(json.dumps({"seat": RETIRED, "pool": "served"}) + "\n")
    service.credentials()
    assert folder.is_dir() and (held / f"{RETIRED}.json").is_file()
    assert seatmod.leftover_checks(service.ship) == [
        f"{RETIRED}: a held renewal's record is unreadable; inspect runtime/{credential.HELD}"]


def test_the_held_records_folder_is_one_no_jail_sees():
    assert not any(str(credential.HELD).startswith(name) for name in sandbox.RUNTIME_SHOWN)


def test_a_round_skipped_on_the_lock_is_not_a_clean_round(crew, monkeypatch):
    import errno
    service, source = crew
    service.credentials()
    renew_by_replacement(service.ship.runtime / "seats" / "desk" / "home" / source.name, fake(SENTINEL, 8 * 3600))
    real = credential.reseed
    calls = []
    phase = {"now": "fail"}

    def scripted(*args, **kwargs):
        calls.append(phase["now"])
        if phase["now"] == "fail":
            raise OSError(errno.EACCES, "denied")
        if phase["now"] == "skip":
            return [f"{credential.SKIPPED} for 2 s; the next round retries"]
        return real(*args, **kwargs)

    monkeypatch.setattr(credential, "reseed", scripted)
    monkeypatch.setattr(credential, "WATCH_SECONDS", 0.002)
    stop, thread = watching(service)

    def ticks(name, count):
        deadline = time.monotonic() + 30
        while calls.count(name) < count:
            assert time.monotonic() < deadline, calls.count(name)
            time.sleep(0.01)

    def cleared():
        text = service.log_path.read_text() if service.log_path.exists() else ""
        return [e for e in map(json.loads, text.splitlines()) if e["event"] == "credential-watch-cleared"]

    try:
        ticks("fail", 10)
        phase["now"] = "skip"
        ticks("skip", 20)
        assert cleared() == [], "a skipped round did no work: the failing set stands and no clearing is logged"
        phase["now"] = "real"
        deadline = time.monotonic() + 10
        while not cleared():
            assert time.monotonic() < deadline
            time.sleep(0.02)
    finally:
        stop.set()
        thread.join(timeout=5)
    assert len(cleared()) == 1 and cleared()[0]["causes"] == ["PermissionError EACCES"]
    assert SENTINEL not in service.log_path.read_text()
