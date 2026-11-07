"""Credentials: the one thing a seat shares with its pool, and the lessons that keep it live.

Share by link, never copy: refresh tokens are single use, and a copy dies at the first
refresh. The link alone is not enough either: a program that writes its refreshed file by
atomic rename turns the seat's link into a private file holding the only live token, and
strands the pool and every sibling. So on every tick the service looks at each seat's
credential path: one that is no longer a link has refreshed, and its file is installed over
the pool's, the stale one parked beside it, the seat relinked. Expiry times are read as
metadata; token values are never read.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .seat import Seat
from .ship import Pool, Ship

WARN_AHEAD = timedelta(hours=48)
CREDENTIAL_FILES = {"claude": ".credentials.json", "codex": "auth.json"}


def pool_file(pool: Pool) -> Path | None:
    name = CREDENTIAL_FILES.get(pool.provider)
    return pool.config_dir / name if pool.config_dir and name else None


def reseed(ship: Ship, seats: list[Seat]) -> list[str]:
    """Reseed each pool from a seat that refreshed, then relink the seat; the proof lines."""
    proofs = []
    for seat in seats:
        source = pool_file(seat.pool)
        if source is None:
            continue
        link = seat.home / source.name
        if not link.exists() or link.is_symlink():
            continue  # still a link, or no credential here: nothing happened
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        parked = source.with_name(f"{source.name}.stale-{stamp}")
        if source.exists():
            shutil.move(source, parked)
        shutil.copy2(link, source)
        link.unlink()
        link.symlink_to(source)
        proofs.append(f"{seat.pool.name} reseeded from {seat.name}'s refresh · stale file parked as {parked.name} · {seat.name} relinked")
    return proofs


def grant_expiry(pool: Pool) -> datetime | None:
    """When the pool's grant itself expires, read as metadata; None where the provider does not say."""
    source = pool_file(pool)
    if source is None or not source.exists():
        return None
    try:
        data = json.loads(source.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    millis = (data.get("claudeAiOauth") or {}).get("refreshTokenExpiresAt")
    if isinstance(millis, (int, float)):
        return datetime.fromtimestamp(millis / 1000, timezone.utc)
    return None


def login_needed(ship: Ship, now: datetime | None = None) -> list[tuple[str, str]]:
    """Pools whose grant expires within two days, with the local time it does: only a login cures it."""
    now = now or datetime.now(timezone.utc)
    found = []
    for pool in ship.pools.values():
        expiry = grant_expiry(pool)
        if expiry is not None and expiry - now <= WARN_AHEAD:
            found.append((pool.name, expiry.astimezone().strftime("%m-%d %H:%M")))
    return found
