"""Credentials: reseed and relink after a refresh, never from a stub; the grant's expiry read as metadata."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from quarterdeck import credential
from quarterdeck.ship import Pool, Ship


def live(token: str, expires_in: float = 3600) -> str:
    expiry = int((time.time() + expires_in) * 1000)
    return json.dumps({"claudeAiOauth": {"accessToken": token, "refreshToken": token, "expiresAt": expiry}})


def ship_with_pool(tmp_path: Path, text: str) -> tuple[Ship, Pool, Path]:
    pool_dir = tmp_path / "pool"
    pool_dir.mkdir(parents=True)
    pool = Pool("claude-secondary", "anthropic", config_dir=pool_dir)
    source = pool_dir / ".credentials.json"
    source.write_text(text)
    ship = Ship(tmp_path, "t", "operator", tmp_path, {pool.name: pool}, {}, {})
    return ship, pool, source


def seat(tmp_path: Path, name: str, pool: Pool, launched_pool: str | None = None):
    home = tmp_path / "runtime" / "seats" / name / "home"
    home.mkdir(parents=True)
    launched = SimpleNamespace(pool=launched_pool or pool.name)
    return SimpleNamespace(name=name, pool=pool, home=home, launched=lambda: launched)


def test_a_refreshed_seat_reseeds_the_pool_and_is_relinked(tmp_path: Path):
    ship, pool, source = ship_with_pool(tmp_path, live("old", 60))
    chair = seat(tmp_path, "chair", pool)
    link = chair.home / ".credentials.json"
    link.symlink_to(source)
    assert credential.reseed(ship, [chair]) == [], "a link means no refresh happened"
    link.unlink()
    link.write_text(live("new", 3600))  # the program refreshed by atomic rename: a private file with the only live token
    proofs = credential.reseed(ship, [chair])
    assert proofs[0].startswith("claude-secondary reseeded from chair's refresh") and proofs[1] == "chair relinked to claude-secondary"
    assert link.is_symlink() and link.resolve() == source.resolve()
    assert json.loads(source.read_text())["claudeAiOauth"]["refreshToken"] == "new", "the pool holds the live token"
    assert proofs[0].endswith("the replaced file kept as runtime/credentials/claude-secondary-.credentials.json.stale")
    kept = tmp_path / "runtime" / "credentials" / "claude-secondary-.credentials.json.stale"
    assert json.loads(kept.read_text())["claudeAiOauth"]["refreshToken"] == "old"
    assert [p.name for p in source.parent.iterdir()] == [".credentials.json"], "the user's folder holds only the live file"  # law: one-folder
    assert credential.reseed(ship, [chair]) == []


def test_a_logged_out_stub_never_replaces_a_live_pool_but_is_relinked_to_it(tmp_path: Path):
    ship, pool, source = ship_with_pool(tmp_path, live("live"))
    desk = seat(tmp_path, "desk", pool)
    (desk.home / ".credentials.json").write_text("{}")  # the seat lost its login and wrote a stub
    proofs = credential.reseed(ship, [desk])
    assert proofs == ["desk relinked to claude-secondary"]
    assert json.loads(source.read_text())["claudeAiOauth"]["refreshToken"] == "live", "the pool is untouched"
    assert not (tmp_path / "runtime" / "credentials").exists(), "nothing replaced, nothing kept"


def test_the_newest_live_refresh_wins_when_two_seats_detached_in_one_tick(tmp_path: Path):
    ship, pool, source = ship_with_pool(tmp_path, live("old", 60))
    chair, maker, desk = seat(tmp_path, "chair", pool), seat(tmp_path, "maker", pool), seat(tmp_path, "desk", pool)
    (chair.home / ".credentials.json").write_text(live("newer", 3600))
    (maker.home / ".credentials.json").write_text(live("older", 1800))
    (desk.home / ".credentials.json").write_text("{}")
    proofs = credential.reseed(ship, [maker, desk, chair])
    assert proofs[0].startswith("claude-secondary reseeded from chair's refresh")
    assert json.loads(source.read_text())["claudeAiOauth"]["refreshToken"] == "newer"
    assert all((s.home / ".credentials.json").is_symlink() for s in (chair, maker, desk))
    assert len(list((tmp_path / "runtime" / "credentials").iterdir())) == 1, "the replaced file kept once"


def test_an_older_refresh_than_the_pool_holds_installs_nothing(tmp_path: Path):
    ship, pool, source = ship_with_pool(tmp_path, live("current", 3600))
    chair = seat(tmp_path, "chair", pool)
    (chair.home / ".credentials.json").write_text(live("stale", 60))
    assert credential.reseed(ship, [chair]) == ["chair relinked to claude-secondary"]
    assert json.loads(source.read_text())["claudeAiOauth"]["refreshToken"] == "current"


def test_the_pool_is_the_one_the_seat_was_launched_on(tmp_path: Path):
    ship, pool_a, source_a = ship_with_pool(tmp_path, live("a", 60))
    pool_b_dir = tmp_path / "pool-b"
    pool_b_dir.mkdir()
    pool_b = Pool("claude-primary", "anthropic", config_dir=pool_b_dir)
    (pool_b_dir / ".credentials.json").write_text(live("b", 3600))
    ship = Ship(tmp_path, "t", "operator", tmp_path, {pool_a.name: pool_a, pool_b.name: pool_b}, {}, {})
    chair = seat(tmp_path, "chair", pool_b, launched_pool="claude-secondary")  # declared B since, still running on A
    (chair.home / ".credentials.json").write_text(live("a-refreshed", 7200))
    proofs = credential.reseed(ship, [chair])
    assert proofs[0].startswith("claude-secondary reseeded")
    assert json.loads(source_a.read_text())["claudeAiOauth"]["refreshToken"] == "a-refreshed"
    assert json.loads((pool_b_dir / ".credentials.json").read_text())["claudeAiOauth"]["refreshToken"] == "b", "B untouched"


def test_the_grant_expiry_is_read_as_metadata_and_warned_two_days_ahead(tmp_path: Path):
    soon = datetime.now(timezone.utc) + timedelta(hours=30)
    ship, pool, _ = ship_with_pool(tmp_path, json.dumps({"claudeAiOauth": {"refreshToken": "x", "refreshTokenExpiresAt": int(soon.timestamp() * 1000)}}))
    assert abs((credential.grant_expiry(pool) - soon).total_seconds()) < 1
    assert credential.login_needed(ship) == [("claude-secondary", soon.astimezone().strftime("%m-%d %H:%M"))]
    later = datetime.now(timezone.utc) + timedelta(days=20)
    ship, pool, _ = ship_with_pool(tmp_path / "later", json.dumps({"claudeAiOauth": {"refreshToken": "x", "refreshTokenExpiresAt": int(later.timestamp() * 1000)}}))
    assert credential.login_needed(ship) == []
    assert credential.grant_expiry(Pool("codex", "openai", config_dir=tmp_path)) is None, "no file, no claim"
    assert credential.pool_file(Pool("api", "anthropic", api_key="env:K")) is None
    assert credential.freshness(pool, tmp_path / "pool" / ".credentials.json") is not None
