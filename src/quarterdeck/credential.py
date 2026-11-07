"""Credentials: the one thing a seat shares with its pool, and the lessons that keep it live.

Share by link, never copy: refresh tokens are single use, and a copy dies at the first
refresh. The link alone is not enough either: a program that writes its refreshed file by
atomic rename turns the seat's link into a private file holding the only live token, and
strands the pool and every sibling. So on every tick the service looks at each seat's
credential path: one that is no longer a link has refreshed. Of the detached files on one
pool, the newest live one is put in the pool's place, one copy of the replaced file kept
under the runtime, and every detached seat relinked; a logged-out stub installs nothing.
Expiry times are read as metadata; token values are never read.
"""

from __future__ import annotations

import json
import os
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


def freshness(path: Path) -> tuple[float, float] | None:
    """How live a credential file is: (its access token's expiry, its mtime), or None for a stub.

    Only the shape of the file is read, and the expiry as a number; never a token's value.
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    tokens = data.get("tokens") if isinstance(data, dict) else None
    if isinstance(oauth, dict) and oauth.get("refreshToken"):
        expiry = oauth.get("expiresAt") or 0
        return (float(expiry) / 1000 if expiry > 1e11 else float(expiry), path.stat().st_mtime)
    if isinstance(tokens, dict) and tokens.get("refresh_token"):
        return (0.0, path.stat().st_mtime)
    return None


def reseed(ship: Ship, seats: list[Seat]) -> list[str]:
    """Reseed each pool from the newest seat that refreshed, then relink the detached seats; the proof lines."""
    detached: dict[str, list[tuple[Path, Seat]]] = {}
    for seat in seats:
        launched = seat.launched()
        pool_name = launched.pool if launched and launched.pool in ship.pools else seat.pool.name
        source = pool_file(ship.pools[pool_name])
        if source is None:
            continue
        link = seat.home / source.name
        if link.exists() and not link.is_symlink():
            detached.setdefault(pool_name, []).append((link, seat))
    proofs = []
    for pool_name, candidates in detached.items():
        source = pool_file(ship.pools[pool_name])
        current = freshness(source) if source.exists() else None
        live = [(freshness(link), link, seat) for link, seat in candidates if freshness(link) is not None]
        newer = [c for c in live if current is None or c[0] > current]
        if newer:
            _, winner, from_seat = max(newer, key=lambda c: c[0])
            parked = _park(source, ship.runtime / "credentials" / f"{pool_name}-{source.name}.stale")
            _install(winner, source)
            proofs.append(f"{pool_name} reseeded from {from_seat.name}'s refresh · the replaced file kept as "
                          f"{parked.relative_to(ship.folder) if parked else 'nothing'}")
        if not source.exists():
            continue  # a pool with no credential yet and only stubs to offer: nothing to link to
        for link, seat in candidates:
            link.unlink()
            link.symlink_to(source)
            proofs.append(f"{seat.name} relinked to {pool_name}")
    return proofs


def _park(source: Path, parked: Path) -> Path | None:
    """One copy of the pool's replaced file, kept under the runtime for a look back; each replaces the last."""
    if not source.exists():
        return None
    parked.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, parked)
    return parked


def _install(winner: Path, source: Path) -> None:
    """The refreshed file put in the pool's place in one step, so no seat finds the place empty: the one
    write outside the project folder, and the law names it."""
    tmp = source.with_name(f"{source.name}.qd-tmp")
    shutil.copy2(winner, tmp)
    os.replace(tmp, source)  # law: one-folder


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
