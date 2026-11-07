"""Credentials: reseed and relink after a refresh; the grant's expiry read as metadata."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from quarterdeck import credential
from quarterdeck.ship import Pool, Ship


def ship_with_pool(tmp_path: Path, **fields) -> tuple[Ship, Pool, Path]:
    pool_dir = tmp_path / "pool"
    pool_dir.mkdir(parents=True)
    pool = Pool("claude-secondary", "claude", config_dir=pool_dir)
    source = pool_dir / ".credentials.json"
    source.write_text(json.dumps({"claudeAiOauth": {"accessToken": "old", "refreshToken": "old", **fields}}))
    ship = Ship(tmp_path, "t", "operator", tmp_path, {pool.name: pool}, {}, {})
    return ship, pool, source


def test_a_refreshed_seat_reseeds_the_pool_and_is_relinked(tmp_path: Path):
    ship, pool, source = ship_with_pool(tmp_path)
    home = tmp_path / "runtime" / "seats" / "pike" / "home"
    home.mkdir(parents=True)
    link = home / ".credentials.json"
    link.symlink_to(source)
    seat = SimpleNamespace(name="pike", pool=pool, home=home)
    assert credential.reseed(ship, [seat]) == [], "a link means no refresh happened"
    # the program refreshes by atomic rename: the link becomes a private file with the only live token
    link.unlink()
    link.write_text(json.dumps({"claudeAiOauth": {"accessToken": "new", "refreshToken": "new"}}))
    proofs = credential.reseed(ship, [seat])
    assert len(proofs) == 1 and "reseeded from pike's refresh" in proofs[0] and "relinked" in proofs[0]
    assert link.is_symlink() and link.resolve() == source.resolve()
    assert json.loads(source.read_text())["claudeAiOauth"]["refreshToken"] == "new", "the pool holds the live token"
    parked = [p for p in source.parent.iterdir() if ".stale-" in p.name]
    assert len(parked) == 1 and json.loads(parked[0].read_text())["claudeAiOauth"]["refreshToken"] == "old"
    assert credential.reseed(ship, [seat]) == []


def test_the_grant_expiry_is_read_as_metadata_and_warned_two_days_ahead(tmp_path: Path):
    soon = datetime.now(timezone.utc) + timedelta(hours=30)
    ship, pool, _ = ship_with_pool(tmp_path, refreshTokenExpiresAt=int(soon.timestamp() * 1000))
    assert abs((credential.grant_expiry(pool) - soon).total_seconds()) < 1
    assert credential.login_needed(ship) == [("claude-secondary", soon.astimezone().strftime("%m-%d %H:%M"))]
    later = datetime.now(timezone.utc) + timedelta(days=20)
    ship, pool, _ = ship_with_pool(tmp_path / "later", refreshTokenExpiresAt=int(later.timestamp() * 1000))
    assert credential.login_needed(ship) == []
    assert credential.grant_expiry(Pool("codex", "codex", config_dir=tmp_path)) is None, "no file, no claim"
    assert credential.pool_file(Pool("api", "claude", api_key="env:K")) is None
