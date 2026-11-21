"""Independent ships reconcile synthetic subscription homes on Linux."""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import multiprocessing
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from quarterdeck import credential, doctor, fleet, seat_files
from quarterdeck.ship import Pool, Ship, load


def _write(path, provider, rank):
    stamp = 2_000_000_000 + rank
    if provider == "openai":
        data = {"tokens": {"refresh_token": str(rank)}}
    elif provider == "anthropic":
        data = {"claudeAiOauth": {"refreshToken": str(rank), "expiresAt": stamp * 1000}}
    else:
        data = {"synthetic": {"refresh_token": str(rank),
                              "expires_at": datetime.fromtimestamp(stamp, timezone.utc).isoformat()}}
    path.write_text(json.dumps(data))
    os.utime(path, (stamp, stamp))


def _rank(path):
    return int(path.stat().st_mtime) - 2_000_000_000


def _crew(folder, home, pool_name, provider, rank, *, name="worker"):
    pool = Pool(pool_name, provider, home)
    ship = Ship(folder, folder.name, "operator", folder, {pool.name: pool}, {}, {})
    seat_home = ship.runtime / "seats" / name / "home"
    seat_home.mkdir(parents=True)
    seat = SimpleNamespace(name=name, home=seat_home, pool=pool, launched=lambda: None)
    source = credential.pool_file(pool)
    candidate = seat_home / source.name
    _write(candidate, provider, rank)
    return ship, seat, candidate


def _receive(channel):
    assert channel.poll(30), "child did not reach the synchronization point"
    return channel.recv()


def _renew(channel, folder, path, pool, provider, rank, pause):
    ship, seat, _ = _crew(folder, path, pool, provider, rank)
    if pause == "install":
        write = seat_files.write_bytes_at

        def paused(*args, **kwargs):
            channel.send("install")
            assert _receive(channel) == "continue"
            return write(*args, **kwargs)

        seat_files.write_bytes_at = paused
    elif pause == "replace":
        replace = os.replace

        def paused(old, new, **kwargs):
            if kwargs.get("dst_dir_fd") is not None and new == Path(credential.pool_file(seat.pool).name):
                channel.send("replace")
                assert _receive(channel) == "continue"
            return replace(old, new, **kwargs)

        seat_files.os.replace = paused
    elif pause == "exchange":
        exchange = credential._exchange

        def paused(first, second):
            if not paused.reported:
                paused.reported = True
                channel.send("exchange")
                assert _receive(channel) == "continue"
            return exchange(first, second)

        paused.reported = False
        credential._exchange = paused
    elif pause == "blocked":
        flock = fcntl.flock

        def observed(fd, operation):
            try:
                return flock(fd, operation)
            except BlockingIOError:
                if stat.S_ISDIR(os.fstat(fd).st_mode) and not observed.reported:
                    observed.reported = True
                    assert fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
                    channel.send("blocked")
                raise

        observed.reported = False
        credential.fcntl.flock = observed
    channel.send(credential.reseed(ship, [seat], wait=30))


def _hold(channel, path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        channel.send("held")
        assert _receive(channel) == "continue"
    finally:
        os.close(descriptor)
    channel.send("released")


@contextmanager
def _process(target, *args):
    ctx = multiprocessing.get_context("spawn")
    channel, child = ctx.Pipe()
    process = ctx.Process(target=target, args=(child, *args))
    process.start()
    child.close()
    try:
        yield channel
        process.join(30)
        assert process.exitcode == 0
    finally:
        channel.close()
        if process.is_alive():
            process.kill()
            process.join(30)


@pytest.mark.parametrize("provider", ["openai", "anthropic", "xai"])
def test_waiting_ship_rejudges_its_candidate_after_peer_install(tmp_path, provider):
    pool = tmp_path / "subscription"
    pool.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(pool, target_is_directory=True)
    source = credential.pool_file(Pool("first", provider, pool))
    _write(source, provider, 1)
    first, second = tmp_path / "first", tmp_path / "second"
    with _process(_renew, first, pool, "a", provider, 3, "install") as writer:
        assert _receive(writer) == "install"
        with _process(_renew, second, alias, "b", provider, 2, "blocked") as waiting:
            assert _receive(waiting) == "blocked"
            assert _rank(source) == 1
            writer.send("continue")
            assert any("reseeded from worker" in line for line in _receive(writer))
            assert not any("reseeded" in line for line in _receive(waiting))
    assert _rank(source) == 3
    assert stat.S_IMODE(source.stat().st_mode) == 0o600
    assert list(pool.iterdir()) == [source]
    assert [_rank(path) for path in (first / "runtime" / "credentials").glob("*.stale")] == [1]
    assert not (second / "runtime" / "credentials").exists()
    candidate = second / "runtime" / "seats" / "worker" / "home" / source.name
    assert candidate.is_symlink() == (provider != "xai")
    assert _rank(candidate) == 3


@pytest.mark.parametrize("pause", ["install", "replace"])
def test_replaced_directory_defers_and_does_not_redirect_descriptor_install(tmp_path, pause):
    pool = tmp_path / "subscription"
    pool.mkdir()
    source = pool / "auth.json"
    _write(source, "openai", 1)
    first, second = tmp_path / "first", tmp_path / "second"
    with _process(_renew, first, pool, "a", "openai", 2, pause) as writer:
        assert _receive(writer) == pause
        old = pool.rename(tmp_path / "old")
        pool.mkdir()
        _write(source, "openai", 1)
        with _process(_renew, second, pool, "b", "openai", 3, None) as peer:
            assert any("reseeded from worker" in line for line in _receive(peer))
        writer.send("continue")
        lines = _receive(writer)
        assert credential.deferred(lines) and any("subscription home changed" in line for line in lines)
    assert _rank(source) == 3
    assert _rank(old / source.name) == (1 if pause == "install" else 2)
    assert list(old.iterdir()) == [old / source.name]
    candidate = first / "runtime" / "seats" / "worker" / "home" / source.name
    assert not candidate.is_symlink() and _rank(candidate) == 2


@pytest.mark.parametrize("provider", ["openai", "xai"])
def test_alias_retarget_during_reconciliation_restores_the_candidate(tmp_path, provider):
    old, new, alias = (tmp_path / name for name in ("old", "new", "alias"))
    old.mkdir()
    new.mkdir()
    alias.symlink_to(old, target_is_directory=True)
    _write(old / "auth.json", provider, 3)
    _write(new / "auth.json", provider, 1)
    folder = tmp_path / "ship"
    with _process(_renew, folder, alias, "a", provider, 2, "exchange") as writer:
        assert _receive(writer) == "exchange"
        alias.unlink()
        alias.symlink_to(new, target_is_directory=True)
        writer.send("continue")
        assert credential.deferred(_receive(writer))
    candidate = folder / "runtime" / "seats" / "worker" / "home" / "auth.json"
    assert not candidate.is_symlink() and _rank(candidate) == 2
    assert _rank(old / "auth.json") == 3 and _rank(new / "auth.json") == 1
    assert list(candidate.parent.iterdir()) == [candidate]


def test_busy_pool_leaves_its_candidate_and_does_not_block_a_free_pool(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    free = tmp_path / "free"
    free.mkdir()
    ship, first, candidate = _crew(tmp_path / "ship", shared, "shared", "openai", 2)
    second_ship, second, other = _crew(ship.folder, free, "free", "openai", 4, name="second")
    ship.pools.update(second_ship.pools)
    _write(shared / candidate.name, "openai", 1)
    # The free directory has no credential yet and can still receive its first refresh.
    with _process(_hold, shared) as holder:
        assert _receive(holder) == "held"
        lines = credential.reseed(ship, [first, second], wait=0)
        assert credential.deferred(lines) and any("skipped for shared" in line for line in lines)
        assert not candidate.is_symlink() and _rank(candidate) == 2
        assert other.is_symlink() and _rank(free / other.name) == 4
        holder.send("continue")
        assert _receive(holder) == "released"
    assert not credential.deferred(credential.reseed(ship, [first, second], wait=0))
    assert candidate.is_symlink() and _rank(shared / candidate.name) == 2


@pytest.mark.parametrize("provider", ["openai", "xai"])
@pytest.mark.parametrize("unlink_at", ["before staging", "after exchange"])
def test_unremovable_scratch_is_retained_and_the_next_pool_renews(tmp_path, monkeypatch, provider, unlink_at):
    blocked, free = tmp_path / "blocked", tmp_path / "free"
    blocked.mkdir()
    free.mkdir()
    ship, first, candidate = _crew(tmp_path / "ship", blocked, "blocked", provider, 2)
    second_ship, second, other = _crew(ship.folder, free, "free", "openai", 4, name="second")
    ship.pools.update(second_ship.pools)
    _write(blocked / candidate.name, provider, 3)
    scratch = candidate.with_name(f"{candidate.name}.qd-copy")
    if unlink_at == "before staging":
        scratch.mkdir()
    else:
        unlink = seat_files.unlink

        def refuse_old_file(path, **kwargs):
            if path == scratch and scratch.is_file():
                raise PermissionError()
            return unlink(path, **kwargs)

        monkeypatch.setattr(seat_files, "unlink", refuse_old_file)

    lines = credential.reseed(ship, [first, second], wait=0)

    assert not credential.deferred(lines)
    assert lines[0].startswith(first.name) and "scratch could not be removed" in lines[0]
    assert lines[0].endswith("; retained")
    if unlink_at == "before staging":
        assert scratch.is_dir() and not candidate.is_symlink() and _rank(candidate) == 2
    else:
        assert _rank(scratch) == 2 and _rank(candidate) == 3
        assert candidate.is_symlink() == (provider == "openai")
    assert _rank(blocked / candidate.name) == 3
    assert other.is_symlink() and _rank(free / other.name) == 4


def test_missing_subscription_home_defers_without_creating_it(tmp_path):
    pool = tmp_path / "absent"
    ship, seat, candidate = _crew(tmp_path / "ship", pool, "only", "openai", 2)
    lines = credential.reseed(ship, [seat], wait=0)
    assert credential.deferred(lines) and any("subscription home unavailable" in line for line in lines)
    assert not pool.exists() and _rank(candidate) == 2 and not candidate.is_symlink()
    pool.mkdir()
    assert not credential.deferred(credential.reseed(ship, [seat], wait=0))
    assert candidate.is_symlink() and _rank(pool / candidate.name) == 2


def _declaration(folder, pools, *, office=False):
    folder.mkdir(parents=True)
    (folder / "ship.toml").write_text(('[ship]\nkind = "office"\n' if office else '[ship]\n')
                                     + "".join(f"\n[pools.{name}]\nprovider = {json.dumps(provider)}\n"
                                               f"{spelling} = {json.dumps(home)}\n"
                                               for name, provider, spelling, home in pools))


def test_doctor_advises_from_fleet_declarations_without_opening_peer_homes(tmp_path, monkeypatch):
    project = tmp_path / "project"
    local, peer, office = project / "ships" / "local", project / "ships" / "peer", project / "office"
    actual = project / "synthetic-pool"
    actual.mkdir(parents=True)
    (project / "alias").symlink_to(actual, target_is_directory=True)
    _declaration(local, [("shared", "openai", "config_dir", "../../synthetic-pool")])
    _declaration(peer, [("alias", "codex", "home", "../../alias"), ("collides", "xai", "config_dir", "../../alias"),
                        ("separate", "anthropic", "home", "../../alias")])
    _declaration(office, [("office-pool", "openai", "home", "../alias")], office=True)
    _declaration(project / "ships" / "default", [("template", "openai", "home", "../../alias")])
    _declaration(tmp_path / "outside", [("outside", "openai", "home", str(actual))])
    monkeypatch.chdir(tmp_path)
    ship = load(local)
    assert ship.pool("shared").config_dir == local / "../../synthetic-pool"
    read = Path.read_text

    def declaration_only(path, *args, **kwargs):
        assert path in {peer / "ship.toml", office / "ship.toml"}
        return read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", declaration_only)
    monkeypatch.setattr(fleet, "peers", lambda *_: pytest.fail("doctor loaded peer runtime or seat configuration"))
    rows = doctor.shared_subscription_checks(ship)
    assert len(rows) == 4
    assert all(row.passed and row.name == "subscription overlap shared" and str(tmp_path) not in row.detail
               and "known fleet" in row.detail and "older qd" in row.detail and "already opened" in row.detail
               for row in rows)
    assert any("peer/alias: shared subscription credential" in row.detail for row in rows)
    assert any("peer/collides: incompatible credential formats" in row.detail for row in rows)
    assert any("peer/separate: same subscription directory with separate credential" in row.detail for row in rows)
    assert any("office/office-pool" in row.detail for row in rows)
