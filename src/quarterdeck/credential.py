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

import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from . import harness as harnesses
from .seat import Seat
from .ship import Pool, Ship

WARN_AHEAD = timedelta(hours=48)


@dataclass(frozen=True)
class Participant:
    """A real seat or a declared native usage reader sharing one pool's sign-in."""

    name: str
    home: Path
    pool: str
    usage_reader: bool = False


def usage_home(ship: Ship, pool: Pool) -> Path:
    # Encode a declared identity into one path component, including dots and slashes.
    component = quote(pool.name, safe="").replace(".", "%2E")
    return ship.runtime / "pool-usage" / component


def participants(ship: Ship, seats: list[Seat]) -> list[Participant]:
    result = []
    for seat in seats:
        launched = seat.launched()
        pool = launched.pool if launched and launched.pool in ship.pools else seat.pool.name
        result.append(Participant(seat.name, seat.home, pool))
    for pool in ship.pools.values():
        if pool.kind == "subscription" and getattr(harnesses.native_for(pool.provider), "private_usage_home", False):
            home = usage_home(ship, pool)
            if not home.is_symlink() and not home.parent.is_symlink():
                result.append(Participant(f"usage reader for {pool.name}", home, pool.name, True))
    return result


def pool_file(pool: Pool) -> Path | None:
    if pool.kind == "endpoint":
        return None
    name = harnesses.native_for(pool.provider).credential_file
    return pool.config_dir / name if pool.config_dir and name else None


def freshness(pool: Pool, path: Path) -> tuple[float, float] | None:
    """The pool adapter judges a detached credential; endpoint labels name no credential reader."""
    if pool.kind == "endpoint":
        return None
    return harnesses.native_for(pool.provider).freshness(path)


def reseed(ship: Ship, seats: list[Seat]) -> list[str]:
    """Reseed each pool from the newest seat that refreshed, then relink the detached seats; the proof lines."""
    detached: dict[str, list[tuple[Path, Participant]]] = {}
    for seat in participants(ship, seats):
        pool_name = seat.pool
        source = pool_file(ship.pools[pool_name])
        if source is None:
            continue
        link = seat.home / source.name
        if link.exists() and not link.is_symlink():
            detached.setdefault(pool_name, []).append((link, seat))
    proofs = []
    for pool_name, candidates in detached.items():
        source = pool_file(ship.pools[pool_name])
        pool = ship.pools[pool_name]
        current = freshness(pool, source) if source.exists() else None
        live = [(freshness(pool, link), link, seat) for link, seat in candidates if freshness(pool, link) is not None]
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
            if seat.usage_reader and freshness(pool, link) is None:
                proofs.append(f"{seat.name} credential unreadable; detached file retained for a human")
                continue
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
    """The grant's expiry, when the pool's adapter can read it: a fixed grant's end, or a self-renewing one's access."""
    source = pool_file(pool)
    return harnesses.native_for(pool.provider).grant_expiry(source) if source is not None else None


RENEWAL_FAILED = "now (its renewal failed)"


def _shown(when: datetime) -> str:
    return when.astimezone().strftime("%m-%d %H:%M")


def login_needed(ship: Ship, now: datetime | None = None) -> list[tuple[str, str]]:
    """Pools that only a login cures, with the local time it is needed by.

    The adapter declares its grant's kind. A fixed grant counts within two days of its end. A
    self-renewing grant counts only when its renewal has visibly failed, or when the grant that renews
    it is readable and within two days of its end; its own short access expiry never counts. An
    unknown kind never counts: doctor shows it as unknown.
    """
    now = now or datetime.now(timezone.utc)
    found = []
    for pool in ship.pools.values():
        source = pool_file(pool)
        if source is None:
            continue
        adapter = harnesses.native_for(pool.provider)
        if adapter.grant_kind == "fixed":
            expiry = adapter.grant_expiry(source)
        elif adapter.grant_kind == "self-renewing":
            if adapter.renewal_failed(source) is True:
                found.append((pool.name, RENEWAL_FAILED))
                continue
            expiry = adapter.renewal_end(source)
        else:
            continue
        if expiry is not None and expiry - now <= WARN_AHEAD:
            found.append((pool.name, _shown(expiry)))
    return found


def grant_detail(pool: Pool) -> str:
    """Doctor's reading of a present credential's grant, by the kind its adapter declares; metadata only."""
    source = pool_file(pool)
    if source is None:
        return ""
    adapter = harnesses.native_for(pool.provider)
    if adapter.grant_kind == "fixed":
        expiry = adapter.grant_expiry(source)
        return f"; its grant expires {_shown(expiry)}" if expiry else ""
    if adapter.grant_kind == "self-renewing":
        access = adapter.grant_expiry(source)
        end = adapter.renewal_end(source)
        detail = "; its access renews on use" + (f" (current access until {_shown(access)})" if access else "")
        detail += f"; the grant that renews it ends {_shown(end)}" if end else "; when the grant that renews it ends is unknown"
        return detail + ("; its renewal failed: log in" if adapter.renewal_failed(source) is True else "")
    return "; grant kind unknown"
