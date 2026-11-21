"""Two Grok seats on one pool refreshing at once: synthetic grants on throwaway homes, a stand-in identity provider whose
refresh tokens are single use, no real sign-in and no program run.

Grok writes its refreshed auth.json at the path it was given, resolving a link to its target (measured 13 November on
1.0.77: the account holder's file was replaced through a seat's link). With links, both seats write the pool's own file,
the account holder's sign-in, and the last writer wins, even a seat whose refresh was refused. With copies, each refresh
lands in its own home; the pool's file is written only by the service, which puts the newest live grant there and brings
every older copy up to it, without losing a refresh that lands while it does.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import threading
from types import SimpleNamespace

import pytest

from quarterdeck import credential
from quarterdeck.harness.grok import GrokHarness
from quarterdeck.harness.home import credential as attach
from quarterdeck.ship import Pool, Session, Ship

START = "2040-01-01T00:00:00Z"


def grant(token: str, expires: str) -> str:
    return json.dumps({"synthetic-grant": {"refresh_token": token, "expires_at": expires}})


class Provider:
    """A stand-in identity provider: each refresh token works once, and its use issues the next."""

    def __init__(self):
        self.used, self.issued, self.lock = set(), 0, threading.Lock()

    def refresh(self, token):
        with self.lock:
            if token in self.used:
                return None  # refused: rotated away by a sibling's refresh
            self.used.add(token)
            self.issued += 1
            return f"synthetic-{self.issued}", f"2040-0{1 + self.issued}-01T00:00:00Z"


def program_refresh(path: Path, provider: Provider, barrier: threading.Barrier, written: threading.Event, wrote: list):
    """What Grok does with the path it was given: read the grant, refresh it, write the result where the path leads
    (a link's target), by one rename. A refused refresh leaves a signed-out file, and is written after the issued one,
    the order in which the account holder loses."""
    target = Path(os.path.realpath(path))
    token = json.loads(target.read_text())["synthetic-grant"]["refresh_token"]
    barrier.wait()  # both read before either writes: the two refreshes are at once
    issued = provider.refresh(token)
    text = grant(*issued) if issued else "{}"
    if not issued:
        assert written.wait(10)
    tmp = target.with_name(f"{target.name}.{threading.get_ident()}")
    tmp.write_text(text)
    os.replace(tmp, target)
    wrote.append(target)
    if issued:
        written.set()
    return issued


def two_seat_ship(tmp_path, *, copy: bool):
    pool_dir = tmp_path / "synthetic-pool"
    pool_dir.mkdir()
    source = pool_dir / "auth.json"
    source.write_text(grant("synthetic-0", START))
    os.chmod(source, 0o600)
    pool = Pool("synthetic-plan", "xai", config_dir=pool_dir)
    ship = Ship(tmp_path, "synthetic", "operator", tmp_path, {pool.name: pool}, {"crew": Session("crew", "crew")}, {})
    seats = []
    for name in ("worker-a", "worker-b"):
        home = tmp_path / "runtime" / "seats" / name / "home"
        home.mkdir(parents=True)
        attach(pool, home / "auth.json", "XAI_API_KEY", copy=copy)
        seats.append(SimpleNamespace(name=name, pool=pool, home=home, launched=lambda: SimpleNamespace(pool=pool.name)))
    return ship, pool, source, seats


def refresh_at_once(seats):
    provider, barrier, results = Provider(), threading.Barrier(len(seats)), {}
    written, wrote = threading.Event(), []

    def run(seat):
        results[seat.name] = program_refresh(seat.home / "auth.json", provider, barrier, written, wrote)
    threads = [threading.Thread(target=run, args=(seat,)) for seat in seats]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert sorted(bool(v) for v in results.values()) == [False, True], results  # one refresh issued, one refused
    return results, wrote


def token(path: Path):
    return json.loads(path.read_text()).get("synthetic-grant", {}).get("refresh_token")


def test_with_links_both_seats_write_the_account_holders_own_file(tmp_path):
    """The reproduction: the race lands on the pool's file itself, and the account holder can be left signed out."""
    ship, pool, source, seats = two_seat_ship(tmp_path, copy=False)
    results, wrote = refresh_at_once(seats)
    assert wrote == [source, source], "both seats wrote the account holder's own file"
    assert all((seat.home / "auth.json").is_symlink() for seat in seats)
    assert source.read_text() == "{}", "the refused seat wrote last: the account holder and every seat are signed out"


def test_with_copies_no_seat_writes_the_pool_and_the_service_installs_the_live_refresh(tmp_path):
    ship, pool, source, seats = two_seat_ship(tmp_path, copy=True)
    before = (source.stat().st_ino, source.read_bytes())
    for seat in seats:
        copy = seat.home / "auth.json"
        assert not copy.is_symlink() and copy.read_bytes() == before[1] and (copy.stat().st_mode & 0o777) == 0o600

    results, wrote = refresh_at_once(seats)
    assert sorted(wrote) == sorted(seat.home / "auth.json" for seat in seats), "each wrote only its own home"
    winner = next(name for name, issued in results.items() if issued)
    loser = next(name for name, issued in results.items() if not issued)
    assert (source.stat().st_ino, source.read_bytes()) == before, "no seat wrote the account holder's file"
    assert token(seats[0].home / "auth.json") != token(seats[1].home / "auth.json")

    proofs = credential.reseed(ship, seats)
    assert proofs[0].startswith(f"synthetic-plan reseeded from {winner}'s refresh")
    assert proofs[1:] == [f"{loser} copy brought up to synthetic-plan"]
    assert token(source) == "synthetic-1", "the pool holds the one live grant, never the refused seat's sign-out"
    assert all((seat.home / "auth.json").read_bytes() == source.read_bytes() for seat in seats)
    assert token(ship.runtime / "credentials" / "synthetic-plan-auth.json.stale") == "synthetic-0"
    assert credential.reseed(ship, seats) == [], "settled: a later pass changes nothing"


@pytest.mark.parametrize("cleanup_refused", [False, True])
def test_a_refresh_that_lands_while_a_copy_is_brought_up_is_kept_and_installed_next(
        tmp_path, monkeypatch, cleanup_refused):
    ship, pool, source, seats = two_seat_ship(tmp_path, copy=True)
    (seats[0].home / "auth.json").write_text(grant("synthetic-old", "2039-01-01T00:00:00Z"))  # older than the pool's
    copy = seats[0].home / "auth.json"
    scratch = copy.with_name("auth.json.qd-copy")
    real = credential.seat_files.copy  # the copy into the home, made without following a link
    unlink = credential.seat_files.unlink

    def copy_while_the_program_refreshes(src, dst, **kwargs):
        real(src, dst, **kwargs)
        if dst == scratch:
            fresh = copy.with_name("auth.json.program")
            fresh.write_text(grant("synthetic-fresh", "2041-01-01T00:00:00Z"))
            os.replace(fresh, copy)  # the program's own refresh, after the copy was judged older

    def refuse_cleanup(path, **kwargs):
        if path == scratch and scratch.is_file():
            raise PermissionError()
        return unlink(path, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(credential.seat_files, "copy", copy_while_the_program_refreshes)
        if cleanup_refused:
            patch.setattr(credential.seat_files, "unlink", refuse_cleanup)
        lines = credential.reseed(ship, seats[:1])
    assert lines == ["worker-a refreshed while its copy was brought up; its refresh kept for the next pass"]
    assert credential.deferred(lines)
    assert token(copy) == "synthetic-fresh" and scratch.exists() == cleanup_refused
    assert token(source) == "synthetic-0"

    proofs = credential.reseed(ship, seats)
    assert proofs[0].startswith("synthetic-plan reseeded from worker-a's refresh")
    assert proofs[1:] == ["worker-b copy brought up to synthetic-plan"]
    assert token(source) == token(seats[1].home / "auth.json") == "synthetic-fresh"


@pytest.mark.parametrize("state,kept", [("link", False), ("older", False), ("equal", True), ("newer", True),
                                        ("signed-out", False)])
def test_building_a_home_makes_a_copy_and_keeps_its_own_refresh(tmp_path, state, kept):
    ship, pool, source, seats = two_seat_ship(tmp_path, copy=True)
    path = seats[0].home / "auth.json"
    path.unlink()
    if state == "link":
        path.symlink_to(source)
    elif state == "equal":
        shutil.copy2(source, path)
    else:
        path.write_text({"older": grant("synthetic-old", "2039-01-01T00:00:00Z"),
                         "newer": grant("synthetic-new", "2041-01-01T00:00:00Z"), "signed-out": "{}"}[state])
    before = path.read_bytes() if not path.is_symlink() else None
    inode = path.lstat().st_ino
    attach(pool, path, "XAI_API_KEY", copy=True)
    assert not path.is_symlink()
    if kept:
        assert path.read_bytes() == before and path.lstat().st_ino == inode
    else:
        assert path.read_bytes() == source.read_bytes() and (path.stat().st_mode & 0o777) == 0o600
    assert token(source) == "synthetic-0", "building a home never writes the pool"


def test_grok_holds_a_copy_and_the_other_programs_keep_their_link():
    from quarterdeck import harness
    assert GrokHarness.credential_copy
    assert [name for name in harness.NAMES if getattr(harness.get(name), "credential_copy", False)] == ["grok"]
