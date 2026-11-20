"""Credentials: the one thing a seat shares with its pool, and the lessons that keep it live.

Share by link, never copy: refresh tokens are single use, and a copy dies at the first
refresh. The link alone is not enough either: a program that writes its refreshed file by
atomic rename turns the seat's link into a private file holding the only live token, and
strands the pool and every sibling. So on every tick the service looks at each seat's
credential path: one that is no longer a link has refreshed. Of the detached files on one
pool, the newest live one is put in the pool's place, one copy of the replaced file kept
under the runtime, and every detached seat relinked; a logged-out stub installs nothing.
Expiry times are read as metadata; token values are never read.

A program that writes its refresh through the link instead would put it straight into
the pool's own file, the account holder's sign-in, where two seats refreshing at once race. Its
seats hold a copy, never a link: each refresh lands in its own home, the newest live one is put
in the pool's place as above, and every copy older than the pool's is brought up to it, swapped
in one exchange so a refresh that lands meanwhile is never lost (law: one-folder).

The service's pass is not fast enough alone: a renewal retires the token every other seat still
holds, and they fail until the pool's file has it. So a dedicated watch in the service looks at
each linked seat's credential name twice a second, and a name that stopped being a link reseeds
at once. Every reseed with something to judge, the watch's, the pass's and a seat's own at down,
holds one lock file in runtime/locks, which every jail hides; the pass and the watch wait for it
at most two seconds and skip the round, a seat going down at most thirty. A temporary seat whose reseed is skipped
keeps its home while it holds a detached credential, until the pass settles it (``hold_home``, ``settle_held``).
Without an adapter freshness reader, detached credentials are kept in place for human review.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import json
import math
import os
import re
import stat
import time
from contextlib import ExitStack, contextmanager
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

from . import durable, harness as harnesses, seat_files, trusted_files
from .harness.home import credential_aliases, credential_review_adapter
from .seat import Seat
from .ship import Pool, Ship, ShipError, valid_name

WARN_AHEAD = timedelta(hours=48)
WATCH_SECONDS = 0.5  # how often the service's watch looks at the linked credential names


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


def source_refusal(ship: Ship, seat: Seat) -> str | None:
    """Why a seat's home never takes part in a pool's credential: neither a source for the pool nor a link to it.

    Its home is trusted only for a pool the ship file declares for the seat, and only outside a jail and off an
    endpoint pool. A jail can write its whole seat folder, launch record and home included, so a jailed seat, or
    a launch record naming another pool, would otherwise install a file of its choosing as that pool's credential.
    """
    from .seat import launch_pool_refusal
    if seat.pool.kind == "endpoint":
        return f"{seat.name}: on an endpoint pool"
    if _jailed(ship, seat):
        return f"{seat.name}: jailed"
    try:
        launched = seat.launched()
    except ShipError as error:
        return str(error)
    return launch_pool_refusal(seat, launched, every_seat=True)


def _jailed(ship: Ship, seat: Seat) -> bool:
    spec = getattr(seat, "spec", None)  # stand-ins may carry no declaration
    return spec is not None and ship.jailed(spec, seat.pool)


def participants(ship: Ship, seats: list[Seat]) -> list[Participant]:
    result = []
    for seat in seats:
        if source_refusal(ship, seat) is not None:
            continue
        launched = seat.launched()
        pool = launched.pool if launched is not None else seat.pool.name
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


RESEED_WAIT = 2.0  # seconds the service's pass and watch wait for the reseed lock before they skip the round
DOWN_WAIT = 30.0  # a seat going down waits longer, still bounded: its refresh is in hand
SKIPPED = "credential reseed skipped: another reseed held the lock"


class Busy(Exception):
    """The reseed lock stayed held for the whole bounded wait."""


@contextmanager
def _reseeding(ship: Ship, wait: float):
    """One reseed at a time across the service's pass, its watch and any ``qd`` process: a lock file in the
    runtime's ``locks`` folder, which every jail hides, so no jailed seat can take it. The wait is bounded."""
    from .sandbox import LOCKS

    folder = ship.runtime / LOCKS
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(folder / "credential-reseed.lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Busy() from None
                time.sleep(0.05)
        yield
    finally:
        os.close(descriptor)


def linked_names(ship: Ship, seats: list[Seat]) -> list[Path]:
    """The credential name in each home that shares its pool's file by a link: where a renewal by rename lands.

    A program whose seats hold a copy is left to the pass, since its copy is never a link.
    """
    names = []
    for seat in participants(ship, seats):
        pool = ship.pools.get(seat.pool)
        source = pool_file(pool) if pool is not None else None
        if source is None or getattr(harnesses.native_for(pool.provider), "credential_copy", False):
            continue
        names.append(seat.home / source.name)
    return names


def detached_marks(names: list[Path]) -> frozenset[tuple[str, int, int, int]]:
    """Each watched name that is a file rather than a link, by its identity; metadata only, nothing is read."""
    found = set()
    for name in names:
        try:
            info = name.lstat()
        except OSError:
            continue
        if not stat.S_ISLNK(info.st_mode):
            found.add((str(name), info.st_dev, info.st_ino, info.st_mtime_ns))
    return frozenset(found)


def reseed(ship: Ship, seats: list[Seat], *, wait: float = RESEED_WAIT) -> list[str]:
    """Reseed each pool from the newest seat that refreshed, then relink the detached seats; the proof lines.

    With the lock held past ``wait``, nothing is done and the one line says so; the next pass or tick retries.
    """
    if not _due(ship, seats):
        return []  # every link still a link and every copy as the pool's: nothing to install and no lock taken
    try:
        with _reseeding(ship, wait):
            return _reseed(ship, seats)  # judged again under the lock: another reseed may have installed it meanwhile
    except Busy:
        return [f"{SKIPPED} for {wait:g} s; the next round retries"]


def _due(ship: Ship, seats: list[Seat]) -> bool:
    """Whether a reseed has anything to judge, by metadata alone: a linked name that is no longer a link, or a
    held copy whose size or modification time differs from the pool's file (a copy is made with the pool's times)."""
    return (_due_of(ship, participants(ship, seats))
            or any(_review_due(ship, seat.name, seat.pool.name, seat.home, source, adapter)
                   for seat, source, adapter in _jailed_reviews(ship, seats)))


def _without_reader(adapter: harnesses.Harness) -> bool:
    return type(adapter).freshness is harnesses.Harness.freshness


def _jailed_reviews(ship: Ship, seats: list[Seat]):
    """Review-only homes never enter participants or take part in reseeding."""
    for seat in seats:
        if _jailed(ship, seat) and (source := pool_file(seat.pool)) is not None:
            adapter = harnesses.native_for(seat.pool.provider)
            if _without_reader(adapter):
                yield seat, source, adapter


def _names(home: Path, adapter: harnesses.Harness) -> tuple[Path, ...]:
    return (home / adapter.credential_file, *credential_aliases(adapter, home))


def _name_info(path: Path) -> tuple[os.stat_result, str | None] | None:
    """Metadata of the name itself. In a seat home, do not follow aliases in its parent directories."""
    located = None
    try:
        located = seat_files.parent(path, create=False)
        if located is None:
            info = path.lstat()
            return info, os.readlink(path) if stat.S_ISLNK(info.st_mode) else None
        directory, name = located
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        return info, os.readlink(name, dir_fd=directory) if stat.S_ISLNK(info.st_mode) else None
    except FileNotFoundError:
        return None
    finally:
        if located is not None:
            os.close(located[0])


def _needs_review(home: Path, source: Path, adapter: harnesses.Harness) -> bool:
    """Look only at credential names and link targets when the format cannot be judged."""
    for name in _names(home, adapter):
        try:
            metadata = _name_info(name)
            if metadata is None:
                continue
            info, target = metadata
            if stat.S_ISREG(info.st_mode) or (name.parent != home and target is not None
                                             and name.resolve() != source.resolve()):
                return True
        except (OSError, RuntimeError, seat_files.LinkRefused):
            return True  # a name we cannot inspect cannot be cleared safely
    return False


def _review_due(ship: Ship, name: str, pool: str, home: Path, source: Path, adapter: harnesses.Harness) -> bool:
    if not _needs_review(home, source, adapter):
        return False
    held = _held_record(ship, _hold_path(ship, name))
    if held is None:
        return True
    if held.review:
        return False
    try:
        return held.keep is None or held.pool != pool or held.keep != _snapshot(home, adapter)
    except (OSError, ShipError):
        return True


def _due_of(ship: Ship, found: list[Participant]) -> bool:
    for seat in found:
        pool = ship.pools.get(seat.pool)
        source = pool_file(pool) if pool is not None else None
        if source is None:
            continue
        adapter = harnesses.native_for(pool.provider)
        if _without_reader(adapter):
            if _review_due(ship, seat.name, seat.pool, seat.home, source, adapter):
                return True
            continue
        name = seat.home / source.name
        try:
            info = name.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(info.st_mode):
            continue
        if not adapter.credential_copy:
            return True
        try:
            held = source.stat()
        except OSError:
            return True
        if (info.st_size, info.st_mtime_ns) != (held.st_size, held.st_mtime_ns):
            return True
    return False


def _reseed(ship: Ship, seats: list[Seat]) -> list[str]:
    proofs = []
    for seat, source, adapter in _jailed_reviews(ship, seats):
        if _review_due(ship, seat.name, seat.pool.name, seat.home, source, adapter):
            _hold_record(ship, seat.name, seat.pool.name, adapter=adapter)
            proofs.append(f"{seat.name}: home held for credential review")
    return proofs + _reseed_of(ship, participants(ship, seats))


def _reseed_of(ship: Ship, found: list[Participant]) -> list[str]:
    detached: dict[str, list[tuple[Path, Participant]]] = {}
    proofs = []
    for seat in found:
        pool_name = seat.pool
        pool = ship.pools[pool_name]
        source = pool_file(pool)
        if source is None:
            continue
        adapter = harnesses.native_for(pool.provider)
        if _without_reader(adapter):
            if _review_due(ship, seat.name, seat.pool, seat.home, source, adapter):
                _hold_record(ship, seat.name, seat.pool, adapter=adapter)
                proofs.append(f"{seat.name}: home held for credential review")
            continue  # there is no reader to judge a detached file, so never relink it
        link = seat.home / source.name
        if link.exists() and not link.is_symlink():
            detached.setdefault(pool_name, []).append((link, seat))
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
        copies = getattr(harnesses.native_for(pool.provider), "credential_copy", False)
        current = freshness(pool, source)
        for link, seat in candidates:
            held = freshness(pool, link)
            if seat.usage_reader and held is None:
                proofs.append(f"{seat.name} credential unreadable; detached file retained for a human")
                continue
            if copies:
                if held is not None and current is not None and held >= current:
                    continue  # as fresh as the pool's, the winner's own among them; a newer one waits for the next pass
                proofs.append(_bring_up(source, link, seat.name, pool_name))
                continue
            seat_files.symlink(link, source)  # renamed over the name: a link the jail planted is replaced, never followed
            proofs.append(f"{seat.name} relinked to {pool_name}")
    return proofs


# A retired temporary seat's home is removed after its reseed. If the reseed skipped a detached credential, the
# service's pass settles it before removal. A review hold keeps the home for a person. Both are on the status line,
# with one record each outside the seat folder.
HELD = Path("credentials") / "held"
RECOVERY = Path("evidence") / "credential-release"
RECOVERY_STAMP = re.compile(r"[0-9]{8}T[0-9]{12}Z")


def hold_home(ship: Ship, seat: Seat, *, reseed_skipped: bool = False) -> bool:
    """Retain a temporary home for human review or for a skipped reseed of its own credential."""
    if held_for_review(ship, seat.name) or kept_after_review(ship, seat.name):
        return True
    for jailed, source, adapter in _jailed_reviews(ship, [seat]):
        if _review_due(ship, jailed.name, jailed.pool.name, jailed.home, source, adapter):
            _hold_record(ship, jailed.name, jailed.pool.name, adapter=adapter)
            return True
    own = next((found for found in participants(ship, [seat]) if found.name == seat.name and not found.usage_reader),
               None)
    pool = ship.pools.get(own.pool) if own is not None else None
    source = pool_file(pool) if pool is not None else None
    if source is None:
        return False
    adapter = harnesses.native_for(pool.provider)
    if _without_reader(adapter):
        if not _review_due(ship, own.name, own.pool, own.home, source, adapter):
            return False
        _hold_record(ship, own.name, own.pool, adapter=adapter)
    elif reseed_skipped and _due_of(ship, [own]):
        _hold_record(ship, own.name, own.pool)
    else:
        return False
    return True


@dataclass(frozen=True)
class Hold:
    name: str
    pool: str
    review: bool
    adapter: str | None
    keep: dict | None


def _hold_path(ship: Ship, name: str) -> Path:
    return ship.runtime / HELD / f"{name}.json"


def _hold_record(ship: Ship, name: str, pool: str, *, adapter: harnesses.Harness | None = None) -> None:
    path = _hold_path(ship, name)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = {"seat": name, "pool": pool}
    if adapter is not None:
        data.update(review=True, adapter=adapter.name)
    durable.replace_file(path, json.dumps(data) + "\n")


def _held_record(ship: Ship, record: Path) -> Hold | None:
    """Review records can outlive the seat and pool declarations that first named them."""
    try:
        data = json.loads(record.read_text())
        name, pool, review = data["seat"], data["pool"], data.get("review", False)
        adapter, keep = data.get("adapter"), data.get("keep")
        if (isinstance(name, str) and name == record.stem and valid_name(name)
                and isinstance(pool, str) and type(review) is bool
                and (adapter is None or isinstance(adapter, str) and adapter in harnesses.NAMES)
                and (keep is None or isinstance(keep, dict))
                and (review or keep is not None
                     or (name not in ship.seats and pool in ship.pools and ship.pools[pool].kind != "endpoint"))):
            return Hold(name, pool, review, adapter, keep)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def held_for_review(ship: Ship, name: str) -> bool:
    hold = _held_record(ship, _hold_path(ship, name))
    return hold is not None and hold.review


def kept_after_review(ship: Ship, name: str) -> bool:
    hold = _held_record(ship, _hold_path(ship, name))
    return hold is not None and not hold.review and hold.keep is not None


def _held(ship: Ship):
    folder = ship.runtime / HELD
    for record in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        yield record, _held_record(ship, record)


def held_words(ship: Ship) -> list[str]:
    """Plain words for homes held for renewal or review, for the status line."""
    words = []
    for record, hold in _held(ship):
        if hold is None:
            words.append(f"{record.stem}: a held renewal's record is unreadable; inspect runtime/{HELD}")
        elif hold.review:
            words.append(f"home held for credential review: qd credential release {hold.name} "
                         "(systems keep; operator archives)")
        elif hold.keep is None:
            words.append(f"{hold.name}: a renewal held in a retired seat's home; the service reseeds {hold.pool} "
                         "from it, then removes it")
    return words  # law: nothing-waits-silently


def settle_held(ship: Ship, *, wait: float = RESEED_WAIT) -> list[str]:
    """Settle held renewals; unreadable records remain visible."""
    lines = []
    for record, hold in _held(ship):
        if hold is None or hold.review or hold.keep is not None:
            continue  # review waits for a choice; a completed keep identifies the names already reviewed
        name, pool = hold.name, hold.pool
        seat_folder = ship.runtime / "seats" / name
        found = [Participant(name, seat_folder / "home", pool)]
        if _due_of(ship, found):
            try:
                with _reseeding(ship, wait):
                    lines += _reseed_of(ship, found)
            except Busy:
                lines.append(f"{SKIPPED} for {wait:g} s; {name}'s held renewal waits for the next round")
                continue
            if held_for_review(ship, name) or _due_of(ship, found):
                continue  # still detached (a copy not brought up): kept, still named, tried again next pass
        if seat_folder.exists():
            shutil.rmtree(seat_folder)
        if trusted_files.folder(ship.runtime, name).is_dir():
            shutil.rmtree(trusted_files.folder(ship.runtime, name))
        record.unlink()
        lines.append(f"{name}'s home removed once its held renewal was judged for {pool}")
    return lines


def _identity(info: os.stat_result) -> tuple[int, int, int]:
    return info.st_dev, info.st_ino, info.st_ctime_ns


def _snapshot(home: Path, adapter: harnesses.Harness) -> dict:
    """The home and both credential names identified by metadata, with no sign-in data read."""
    folder = _name_info(home.parent)
    root = _name_info(home)
    if root is not None and not stat.S_ISDIR(root[0].st_mode):
        raise ShipError(f"{home}: the held home is not a directory")
    names = {}
    for path in _names(home, adapter):
        label = str(path.relative_to(home))
        try:
            metadata = _name_info(path)
        except (OSError, seat_files.LinkRefused):
            names[label] = ["unreadable"]
            continue
        if metadata is None:
            names[label] = ["missing"]
            continue
        info, target = metadata
        kind = "link" if target is not None else "file" if stat.S_ISREG(info.st_mode) else "other"
        names[label] = [kind, target, info.st_mode, *_identity(info)]
    return {"folder": list(_identity(folder[0])[:2]) if folder is not None else None,
            "home": list(_identity(root[0])[:2]) if root is not None else None, "names": names}


@contextmanager
def _release_claim(ship: Ship, name: str):
    from . import lifecycle, pending

    try:
        with pending.transient_lock(ship.runtime, name) as unowned:
            if not unowned:
                raise ShipError(f"{name}: its temporary operation is still up; the review hold stays")
            with lifecycle.claim(ship.runtime, name), _reseeding(ship, RESEED_WAIT):
                yield
    except Busy:
        raise ShipError(f"{name}: credential work is in progress; retry the release") from None
    except OSError as error:
        raise ShipError(f"{name}: credential release could not access its files ({error}); inspect the review hold") from None


def _release_view(ship: Ship, tmux, name: str, record: Path, home: Path, *, systems: bool):
    from . import jail_end, seat_migration
    from .seat import UNREADABLE, _temporary, _temporary_launch, process_alive, process_start

    if systems:
        spec = ship.seats.get(name)
        if spec is not None:
            if ship.session_of(spec).human:
                raise ShipError(f"{name}: only the operator may release a home in a human's session")
        else:
            temporary = _temporary(ship, name, require_folder=False)
            if not isinstance(temporary, dict) or temporary["human"]:
                raise ShipError(f"{name}: only the operator may release a home without a known crew session")

    hold = _held_record(ship, record)
    if hold is None or not hold.review:
        raise ShipError(f"{name}: no readable credential review hold")
    trusted = trusted_files.folder(ship.runtime, name)
    launch = _temporary_launch(ship, name)
    if (launch == UNREADABLE or jail_end.held(trusted) is not None or seat_migration.unconfirmed(trusted) is not None
            or (trusted / trusted_files.RETIRING).exists()):
        raise ShipError(f"{name}: its retirement is unconfirmed; the review hold stays")
    if launch is not None:
        pane = tmux.pane(launch["pane"])
        start = launch.get("start")
        if (pane is not None and not pane.dead or process_alive(launch["pid"], start)
                or start is None and process_start(launch["pid"]) is not None):
            raise ShipError(f"{name}: its seat is still up; take it down before releasing the held home")
    temporary = _temporary(ship, name)
    if temporary == UNREADABLE:
        raise ShipError(f"{name}: its temporary seat record is unreadable; the review hold stays")
    return hold, _identity(record.lstat()), _snapshot(home, credential_review_adapter(hold.adapter)), temporary is not None


def release(ship: Ship, tmux, name: str, caller: str | None, choose) -> str:
    """Offer a choice on the inspected home, then recheck it under the same claims before acting."""
    from . import acts

    systems = caller is not None
    if systems and (reason := acts.authority(ship, caller, action="may keep a home held for credential review")):
        raise ShipError(reason)
    if not valid_name(name):
        raise ShipError(f"invalid seat name {name!r}")
    record = _hold_path(ship, name)
    home = ship.runtime / "seats" / name / "home"
    with _release_claim(ship, name):
        view = _release_view(ship, tmux, name, record, home, systems=systems)
    hold, _, snapshot, temporary = view
    words = [f"{name}'s held home at {home}"]
    for path, state in snapshot["names"].items():
        target = f" -> {ascii(state[1])}" if state[0] == "link" else ""
        words.append(f"  {path}: {state[0]}{target}")
    if temporary and not systems:
        words.append(f"Remove moves the temporary seat folder at {home.parent} into private evidence.")
    choice = choose(words)
    if systems and choice != "keep":
        reason = "only the operator may choose remove" if choice == "remove" else "choose keep"
        raise ShipError(f"{name}: {reason}; the review hold stays")
    if choice not in ("keep", "remove"):
        raise ShipError(f"{name}: choose keep or remove; the review hold stays")

    with _release_claim(ship, name):
        if _release_view(ship, tmux, name, record, home, systems=systems) != view:
            raise ShipError(f"{name}: the held home changed during review; run the command again")
        if any(state[0] == "unreadable" for state in snapshot["names"].values()):
            raise ShipError(f"{name}: an unreadable credential name needs inspection; the review hold stays")
        if snapshot["home"] is None and (choice == "keep" or not temporary):
            raise ShipError(f"{name}: the held home is missing; the review hold stays")
        seat = SimpleNamespace(trusted=trusted_files.folder(ship.runtime, name))
        actor = caller if systems else ship.operator or "the operator"
        act = dict(act=uuid.uuid4().hex[:12], verb="credential-release", at=acts._now(), caller=actor,
                   seat=name, choice=choice, home=snapshot["home"], outcome="pending")
        if choice == "remove":
            source = home.parent if temporary else home
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            target = ship.runtime / RECOVERY / f"{name}-{stamp}" / ("seat" if temporary else "home")
            act["planned_recovery"] = str(target)
        try:
            acts.write(seat, act)
        except (OSError, ShipError) as error:
            raise ShipError(f"{name}: the release could not be recorded ({error}); the review hold stays") from None
        try:
            if choice == "keep":
                previous_review = record.read_text()
                kept = dict(seat=name, pool=hold.pool, adapter=hold.adapter, review=False, keep=snapshot)
                durable.replace_file(record, json.dumps(kept) + "\n")
                detail = "home kept in place; credential review cleared"
            else:
                _move_home(ship.runtime, source, target, snapshot, temporary=temporary)
                try:
                    record.unlink(missing_ok=True)
                except OSError as error:
                    raise ShipError(f"held home moved to {target}, but its review record could not be cleared ({error})")
                detail = f"held home moved to {target}; credential review cleared"
        except (OSError, ShipError) as error:
            detail = f"release failed ({error}); the review hold stays"
            outcome = "failed"
        else:
            outcome = choice
        try:
            result = dict(act=act["act"], verb=act["verb"], at=acts._now(), outcome=outcome, detail=detail)
            if outcome == "remove":
                result["recovery"] = str(target)
            acts.write(seat, result)
        except (OSError, ShipError) as error:
            if outcome == "keep":
                try:
                    durable.replace_file(record, previous_review)
                except (OSError, ShipError) as restore_error:
                    raise ShipError(f"{name}: the keep outcome could not be recorded ({error}) and the review hold "
                                    f"could not be restored ({restore_error})") from None
                raise ShipError(f"{name}: the keep outcome could not be recorded ({error}); the review hold stays") from None
            raise ShipError(f"{name}: {detail}; the outcome could not be recorded ({error})") from None
        if outcome == "failed":
            raise ShipError(f"{name}: {detail}")
    return f"{name}: {detail}"


def _directory_at(stack: ExitStack, path: Path | str, *, parent: int | None = None) -> int:
    descriptor = os.open(path, seat_files.DIRECTORY, dir_fd=parent)
    stack.callback(os.close, descriptor)
    return descriptor


@contextmanager
def _recovery_root(runtime: Path, *, create: bool):
    """Open the evidence namespace without following links in the runtime."""
    with ExitStack() as stack:
        directory = _directory_at(stack, runtime)
        for part in RECOVERY.parts:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=directory)
                except FileExistsError:
                    pass
            directory = _directory_at(stack, part, parent=directory)
        yield _private_recovery_directory(directory)


def _private_recovery_directory(directory: int) -> int:
    info = os.fstat(directory)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ShipError("the recovery destination is not a private mode-0700 directory")
    return directory


def recovery_words(runtime: Path) -> list[str]:
    """Use the moved directory's entry as evidence even when an outcome could not be written."""
    words = []
    try:
        with _recovery_root(runtime, create=False) as root:
            for folder in sorted(os.listdir(root)):
                name, _, stamp = folder.rpartition("-")
                if not valid_name(name) or not RECOVERY_STAMP.fullmatch(stamp):
                    continue
                path = Path("runtime") / RECOVERY / folder
                try:
                    with ExitStack() as stack:
                        directory = _private_recovery_directory(_directory_at(stack, folder, parent=root))
                        for entry in ("home", "seat"):
                            try:
                                info = os.stat(entry, dir_fd=directory, follow_symlinks=False)
                            except FileNotFoundError:
                                continue
                            if not stat.S_ISDIR(info.st_mode):
                                raise ShipError("the recovery entry is not a directory")
                            words.append(f"recovery at {path / entry}")
                except FileNotFoundError:
                    continue
                except (OSError, ShipError):
                    words.append(f"{name}: recovery evidence unreadable at {path}")
    except FileNotFoundError:
        pass  # no evidence namespace yet
    except (OSError, ShipError):
        words.append(f"credential release evidence unreadable at runtime/{RECOVERY}")
    return words


def _move_home(runtime: Path, source: Path, target: Path, snapshot: dict, *, temporary: bool) -> None:
    """Rename the reviewed directory as one unit, using only directory descriptors and metadata."""
    with _recovery_root(runtime, create=True) as root, ExitStack() as stack:
        seats = _directory_at(stack, "seats", parent=_directory_at(stack, runtime))
        folder = seats if temporary else _directory_at(stack, source.parent.name, parent=seats)
        if not temporary and list(_identity(os.fstat(folder))[:2]) != snapshot["folder"]:
            raise ShipError("the held seat folder changed before the move")
        identity = snapshot["folder" if temporary else "home"]

        wrapper = target.parent.name
        os.mkdir(wrapper, mode=0o700, dir_fd=root)
        moved = False
        try:
            into = _private_recovery_directory(_directory_at(stack, wrapper, parent=root))
            try:
                os.stat(target.name, dir_fd=into, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ShipError(f"the recovery destination is occupied: {target}")
            info = os.stat(source.name, dir_fd=folder, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) or list(_identity(info)[:2]) != identity:
                raise ShipError("the held home changed before the move")
            os.rename(source.name, target.name, src_dir_fd=folder, dst_dir_fd=into)
            moved = True
            try:
                info = os.stat(target.name, dir_fd=into, follow_symlinks=False)
            except OSError as error:
                raise ShipError(f"the move could not be confirmed at {target} ({error})") from None
            if not stat.S_ISDIR(info.st_mode) or list(_identity(info)[:2]) != identity:
                raise ShipError(f"the move could not be confirmed at {target}: the directory identity differs")
        finally:
            if not moved:
                try:
                    os.rmdir(wrapper, dir_fd=root)
                except OSError:
                    pass  # leave any entry that appeared here untouched


def _bring_up(source: Path, path: Path, name: str, pool_name: str) -> str:
    """The pool's credential brought up into the seat's copy (see _bring_up_copy). A link its jail planted on the way
    is refused (seat_files.LinkRefused) and named, the copy retained, as an unwritable copy is: never raised."""
    try:
        return _bring_up_copy(source, path, name, pool_name)
    except seat_files.LinkRefused as error:
        try:
            seat_files.unlink(path.with_name(f"{path.name}.qd-copy"))
        except (OSError, seat_files.LinkRefused):
            pass  # through the same link: nothing of ours can be there to remove
        return f"{name} copy not brought up to {pool_name}: {error}; retained"


def _bring_up_copy(source: Path, path: Path, name: str, pool_name: str) -> str:
    """Bring one home's older copy up to the pool's, while its program may be running and refreshing.

    The pool's copy is swapped with the home's file in one exchange, so the program never finds the place empty. What
    came out is then the file that was judged older, or a refresh the program wrote since: that one is swapped straight
    back and kept, and the next pass puts it in the pool's place. Nothing is overwritten unseen.
    """
    try:
        judged = path.lstat()
    except OSError:
        return f"{name} copy unreadable; retained for the next pass"
    if not stat.S_ISREG(judged.st_mode):
        return f"{name} copy is not a plain file; retained for the next pass"
    tmp = path.with_name(f"{path.name}.qd-copy")
    seat_files.unlink(tmp)
    seat_files.copy(source, tmp, mode=0o600)  # made in the home without following a link, the pool's times kept
    try:
        _exchange(tmp, path)
    except OSError as error:
        seat_files.unlink(tmp)
        return f"{name} copy not brought up to {pool_name}: {os.strerror(error.errno)}; retained"
    out = tmp.lstat()
    if (out.st_ino, out.st_dev, out.st_mtime_ns, out.st_size) != (judged.st_ino, judged.st_dev, judged.st_mtime_ns,
                                                                  judged.st_size):
        _exchange(tmp, path)  # the program wrote since it was judged: its file goes straight back
        seat_files.unlink(tmp)
        return f"{name} refreshed while its copy was brought up; its refresh kept for the next pass"
    seat_files.unlink(tmp)
    return f"{name} copy brought up to {pool_name}"


RENAME_EXCHANGE = 2
_libc = None


def _exchange(first: Path, second: Path) -> None:
    """Swap two names in one directory atomically (renameat2 with RENAME_EXCHANGE): neither is ever absent."""
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(None, use_errno=True)
    at_cwd = -100
    located = seat_files.parent(second, create=False)  # in a seat folder: through its folder, never a link on the way
    folder, first_name, second_name = (at_cwd, os.fsencode(first), os.fsencode(second)) if located is None else (
        located[0], os.fsencode(first.name), os.fsencode(located[1]))
    try:
        if _libc.renameat2(folder, first_name, folder, second_name, RENAME_EXCHANGE) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code or errno.EIO))
    finally:
        if located is not None:
            os.close(located[0])


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
    # The winner is read without following a link: a home is its jail's to write, and a link there is no refresh.
    read = seat_files.read_with_times(winner)
    if read is None:
        raise OSError(errno.ELOOP, f'{winner} is not a plain file; the pool keeps its own')
    data, times = read
    seat_files.write_bytes(source, data, mode=0o600, times=times)  # law: one-folder (a temporary renamed in place)


def grant_expiry(pool: Pool) -> datetime | None:
    """The grant's expiry, when the pool's adapter can read it: a fixed grant's end, or a self-renewing one's access."""
    source = pool_file(pool)
    return harnesses.native_for(pool.provider).grant_expiry(source) if source is not None else None


RENEWAL_FAILED = "now (its renewal failed)"


def _shown(when: datetime) -> str:
    return when.astimezone().strftime("%m-%d %H:%M")


def _renewal_evidence(adapter, source: Path) -> tuple[datetime | None, bool]:
    if adapter.grant_kind == "fixed":
        return adapter.grant_expiry(source), False
    if adapter.grant_kind == "self-renewing":
        return adapter.renewal_end(source), adapter.renewal_failed(source) is True
    return None, False


def renewal_status(pool: Pool, source: Path, now: datetime) -> str:
    """Read the renewal deadline when freshness has already established the grant's presence."""
    adapter = harnesses.native_for(pool.provider)
    end, failed = _renewal_evidence(adapter, source)
    if failed or end is not None and end <= now:
        return 'expired'
    if end is not None or adapter.grant_kind == 'self-renewing':
        return 'renewable'
    return 'unknown'


def sign_in_status(pool: Pool, source: Path, now: datetime) -> str:
    """Whether recorded failure recovery has access, can renew it, or needs better evidence."""
    adapter = harnesses.native_for(pool.provider)
    if read := getattr(adapter, 'sign_in_metadata', None):
        metadata = read(source)
        if metadata is None:
            return 'unknown'
        expiry, has_refresh, end = metadata
        if not has_refresh:
            return 'expired' if expiry <= now.timestamp() else 'unknown'
        renewal = 'expired' if end is not None and end <= now else 'renewable'
    else:
        fresh = freshness(pool, source)
        if fresh is None or type(fresh[0]) not in (int, float) or not math.isfinite(fresh[0]):
            return 'unknown'
        expiry = fresh[0]
        renewal = renewal_status(pool, source, now)
    if renewal == 'expired' or expiry <= now.timestamp():
        return renewal
    return 'valid'


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
        expiry, failed = _renewal_evidence(adapter, source)
        if failed:
            found.append((pool.name, RENEWAL_FAILED))
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
