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
takes the ship's lock in runtime/locks, then an exclusive flock on the existing subscription directory.
Ships that resolve the same directory share that second lock. A reseed has one bounded acquisition budget
for both locks. The directory descriptor pins the target of the atomic install even if the path is replaced.
A retired seat whose reseed is deferred keeps its source binding for a later pass.
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


def participants(ship: Ship, seats: list[Seat], *, include_usage: bool = True) -> list[Participant]:
    result = []
    for seat in seats:
        if source_refusal(ship, seat) is not None:
            continue
        launched = seat.launched()
        pool = launched.pool if launched is not None else seat.pool.name
        result.append(Participant(seat.name, seat.home, pool))
    for pool in ship.pools.values() if include_usage else ():
        if pool.kind == "subscription" and getattr(harnesses.native_for(pool.provider), "private_usage_home", False):
            home = usage_home(ship, pool)
            if not home.is_symlink() and not home.parent.is_symlink():
                result.append(Participant(f"usage reader for {pool.name}", home, pool.name, True))
    return result


def pool_file(pool: Pool) -> Path | None:
    if pool.kind != "subscription":
        return None
    name = harnesses.native_for(pool.provider).credential_file
    return pool.config_dir / name if pool.config_dir and name else None


def drop_plain_copy(ship: Ship, adapter, home: Path) -> None:
    """A home moving onto a key leaves the copy of a subscription's sign-in its program kept, where that copy is byte
    for byte a declared pool's own file, so nothing is lost. A copy that differs may hold the only live token: it
    stays, and the build that follows refuses and says so."""
    if not getattr(adapter, 'credential_copy', False) or not adapter.credential_file:
        return
    held = seat_files.read_with_times(home / adapter.credential_file)
    if held is None:
        return
    for pool in ship.pools.values():
        if pool.kind != 'subscription' or harnesses.native_for(pool.provider).name != adapter.name:
            continue
        source = pool_file(pool)
        try:
            if source is not None and source.read_bytes() == held[0]:
                seat_files.unlink(home / adapter.credential_file)
                return
        except OSError:
            continue


def freshness(pool: Pool, path: Path) -> tuple[float, float] | None:
    """The pool adapter judges a detached credential; endpoint labels name no credential reader."""
    if pool.kind == "endpoint":
        return None
    return harnesses.native_for(pool.provider).freshness(path)


RESEED_WAIT = 2.0  # seconds the service's pass and watch wait for the reseed lock before they skip the round
DOWN_WAIT = 30.0  # a seat going down waits longer, still bounded: its refresh is in hand
SKIPPED = "credential reseed skipped: another reseed held the lock"
COPY_REFRESHED = "refreshed while its copy was brought up; its refresh kept for the next pass"


class Busy(Exception):
    """A reseed lock stayed held for the whole bounded wait."""


class Changed(Exception):
    """The directory or a judged credential changed during this reseed."""


class CandidateChanged(Changed):
    """Only this home needs a retry. Other candidates can still reconcile."""


def _lock(descriptor: int, deadline: float) -> None:
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Busy() from None
            time.sleep(min(0.05, remaining))


@contextmanager
def _reseeding(ship: Ship, wait: float):
    """Keep local review and home reconciliation in one process, before taking any pool lock."""
    from .sandbox import LOCKS

    deadline = time.monotonic() + wait
    folder = ship.runtime / LOCKS
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(folder / "credential-reseed.lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        _lock(descriptor, deadline)
        yield deadline
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class PoolHome:
    descriptor: int
    identity: os.stat_result

    def check(self, path: Path) -> None:
        try:
            same = os.path.samestat(self.identity, path.stat())
        except OSError:
            same = False
        if not same:
            raise Changed("subscription home changed") from None

    def file(self, name: str) -> Path:
        # The native freshness readers take a Path. Read relative to the pinned directory on Linux.
        return Path("/proc/self/fd") / str(self.descriptor) / name


@contextmanager
def _pool_home(path: Path, deadline: float):
    """Acquire a lock on the directory itself. Never create a lock file or a missing subscription home."""
    try:
        canonical = path.resolve(strict=True)
        descriptor = os.open(canonical, seat_files.DIRECTORY)
    except (OSError, RuntimeError):
        raise Changed("subscription home unavailable") from None
    try:
        home = PoolHome(descriptor, os.fstat(descriptor))
        try:
            _lock(descriptor, deadline)
        except OSError:
            raise Changed("subscription home could not be locked") from None
        home.check(path)
        yield home
    finally:
        os.close(descriptor)


def _deferred(pool: str, reason: str) -> str:
    return f"credential reseed skipped for {pool}: {reason}; the next round retries"


def deferred(lines: list[str] | None) -> bool:
    """Whether a round left work for retry, including the legacy ship-lock proof."""
    return any(line.startswith("credential reseed skipped") or line.endswith(COPY_REFRESHED)
               or " copy not brought up to " in line for line in (lines or ()))


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


def reseed(ship: Ship, seats: list[Seat], *, wait: float = RESEED_WAIT, include_usage: bool = True) -> list[str]:
    """Install from the newest live candidate, then reconcile local homes. The preflight reads metadata only."""
    def discover():
        return participants(ship, seats, include_usage=include_usage)

    if not _due(ship, seats, include_usage=include_usage):
        return []
    try:
        with _reseeding(ship, wait) as deadline:
            proofs = []
            for seat, source, adapter in _jailed_reviews(ship, seats):
                if _review_due(ship, seat.name, seat.pool.name, seat.home, source, adapter):
                    _hold_record(ship, seat.name, seat.pool.name, adapter=adapter)
                    proofs.append(f"{seat.name}: home held for credential review")
            return proofs + _reseed_of(ship, discover, deadline)
    except Busy:
        return [f"{SKIPPED} for {wait:g} s; the next round retries"]


def _due(ship: Ship, seats: list[Seat], *, include_usage: bool = True) -> bool:
    return (_due_of(ship, participants(ship, seats, include_usage=include_usage))
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


def _reseed_of(ship: Ship, discover, deadline: float) -> list[str]:
    proofs = []
    by_pool: dict[str, list[Participant]] = {}
    for participant in discover():
        by_pool.setdefault(participant.pool, []).append(participant)
    directories: dict[tuple[int, int], list[Pool]] = {}
    for name, found in by_pool.items():
        pool = ship.pools[name]
        source = pool_file(pool)
        if source is None or not _due_of(ship, found):
            continue
        adapter = harnesses.native_for(pool.provider)
        if _without_reader(adapter):
            for seat in found:
                if _review_due(ship, seat.name, name, seat.home, source, adapter):
                    _hold_record(ship, seat.name, name, adapter=adapter)
                    proofs.append(f"{seat.name}: home held for credential review")
            continue
        try:
            info = pool.config_dir.stat()
        except OSError:
            proofs.append(_deferred(name, "subscription home unavailable"))
            continue
        directories.setdefault((info.st_dev, info.st_ino), []).append(pool)
    for pools in directories.values():
        try:
            with _pool_home(pools[0].config_dir, deadline) as home:
                for pool in pools:
                    try:
                        found = [seat for seat in discover() if seat.pool == pool.name]
                        _reseed_pool(ship, pool, home, found, proofs)
                    except Changed as reason:
                        proofs.append(_deferred(pool.name, str(reason)))
        except Busy:
            proofs.extend(_deferred(pool.name, "another reseed held the subscription home lock") for pool in pools)
        except Changed as reason:
            proofs.extend(_deferred(pool.name, str(reason)) for pool in pools)
    return proofs


def _file_state(path: Path, *, follow: bool = False):
    try:
        info = path.stat() if follow else path.lstat()
    except FileNotFoundError:
        return None
    return (*_identity(info), info.st_mtime_ns, info.st_size, info.st_mode)


def _source_state(path: Path):
    return _file_state(path), _file_state(path, follow=True)


def _reseed_pool(ship: Ship, pool: Pool, home: PoolHome, found: list[Participant], proofs: list[str]) -> None:
    target = pool_file(pool)
    source = home.file(target.name)
    adapter = harnesses.native_for(pool.provider)
    home.check(target.parent)
    state = _source_state(source)
    current = freshness(pool, source) if state[1] is not None else None
    candidates = []
    for seat in found:
        path = seat.home / source.name
        judged = _file_state(path)
        if judged is not None and stat.S_ISREG(judged[-1]):
            candidates.append((freshness(pool, path), path, seat, judged))

    def check():
        home.check(target.parent)
        if _source_state(source) != state:
            raise Changed("pool credential changed")

    newer = [row for row in candidates if row[0] is not None and (current is None or row[0] > current)]
    if newer:
        _, winner, participant, judged = max(newer, key=lambda row: row[0])

        def check_install():
            check()
            if _file_state(winner) != judged:
                raise Changed("refresh candidate changed")

        check_install()
        data = seat_files.read_with_times(winner)
        if data is None:
            raise Changed("refresh candidate changed")
        content, times = data
        stale = ship.runtime / "credentials" / f"{quote(pool.name, safe='')}-{source.name}.stale"
        with _park(source, stale) as parked:
            # law: one-folder (the temporary and rename are both relative to this open directory)
            seat_files.write_bytes_at(home.descriptor, source.name, content, mode=0o600, times=times,
                                      before_replace=check_install)
        home.check(target.parent)
        state = _source_state(source)
        current = freshness(pool, source)
        proofs.append(f"{pool.name} reseeded from {participant.name}'s refresh · the replaced file kept as "
                      f"{parked.relative_to(ship.folder) if parked else 'nothing'}")
    check()
    if state[1] is None:
        return  # no pool credential yet and only stubs to offer
    copies = getattr(adapter, "credential_copy", False)
    for held, path, seat, judged in candidates:
        if seat.usage_reader and held is None:
            proofs.append(f"{seat.name} credential unreadable; detached file retained for a human")
        elif copies and held is not None and current is not None and held >= current:
            continue  # as fresh as the pool's; a newer one waits for the next pass
        elif held is not None and (current is None or held > current):
            raise Changed("pool credential no longer matches the judged refresh")
        else:
            try:
                if copies:
                    proof = _bring_up(source, path, seat.name, pool.name, judged=judged, check=check)
                else:
                    _reconcile(source, target, path, judged, seat.name, check, copy=False)
                    proof = f"{seat.name} relinked to {pool.name}"
            except CandidateChanged as reason:
                proof = _deferred(pool.name, str(reason))
            proofs.append(proof)


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


def _remove_home(ship: Ship, seat: Participant) -> None:
    for folder in (seat.home.parent, trusted_files.folder(ship.runtime, seat.name)):
        if folder.is_dir():
            shutil.rmtree(folder)


def _settle_home(ship: Ship, seat: Participant, deadline: float, lines: list[str], *, held: bool) -> bool:
    pool = ship.pools[seat.pool]
    source = pool_file(pool)
    if source is None or _without_reader(harnesses.native_for(pool.provider)):
        if held:
            return False  # an earlier hold cannot be retired once its subscription or reader is gone
        _remove_home(ship, seat)
        return True
    candidate = _file_state(seat.home / source.name)
    if candidate is None or not stat.S_ISREG(candidate[-1]):
        _remove_home(ship, seat)
        return True  # there is no detached plain file to reconcile
    with _pool_home(source.parent, deadline) as home:
        proofs = []
        _reseed_pool(ship, pool, home, [seat], proofs)
        lines.extend(proofs)
        if deferred(proofs) or (held and _due_of(ship, [seat])):
            return False
        home.check(source.parent)
        _remove_home(ship, seat)
        return True


def retire_home(ship: Ship, seat: Seat) -> None:
    """Temporary teardown owns the seat before claiming its one pool or recording a renewal hold."""
    record = _hold_path(ship, seat.name)
    if record.exists() or hold_home(ship, seat):
        return
    own = next(iter(participants(ship, [seat], include_usage=False)), None)
    if own is None:
        _remove_home(ship, Participant(seat.name, seat.home, seat.pool.name))
        return
    source = pool_file(ship.pools[own.pool])
    candidate = _file_state(seat.home / source.name) if source is not None else None
    if candidate is None or not stat.S_ISREG(candidate[-1]):
        _remove_home(ship, own)
        return
    try:
        with _reseeding(ship, DOWN_WAIT) as deadline:
            if _settle_home(ship, own, deadline, [], held=False):
                return
    except (Busy, Changed):
        pass
    _hold_record(ship, own.name, own.pool)


def settle_held(ship: Ship, *, wait: float = RESEED_WAIT) -> list[str]:
    """Finish held renewals while owning their temporary seat, ship lock and one pool lock."""
    from . import lifecycle, pending

    lines = []
    for record, hold in _held(ship):
        if hold is None or hold.review or hold.keep is not None:
            continue
        name, pool = hold.name, hold.pool
        found = Participant(name, ship.runtime / "seats" / name / "home", pool)
        try:
            with pending.transient_lock(ship.runtime, name) as unowned:
                if not unowned:
                    continue
                with lifecycle.claim(ship.runtime, name), _reseeding(ship, wait) as deadline:
                    if _held_record(ship, record) != hold:
                        continue
                    if not _settle_home(ship, found, deadline, lines, held=True):
                        continue
                    record.unlink()
                    lines.append(f"{name}'s home removed once its held renewal was judged for {pool}")
        except lifecycle.Busy:
            continue
        except Busy:
            lines.append(_deferred(pool, f"{name}'s held renewal could not acquire its lock"))
        except Changed as reason:
            lines.append(_deferred(pool, str(reason)))
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
    temporary = _temporary(ship, name, require_folder=False)
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
    hold, review_identity, snapshot, temporary = view
    source = home.parent if temporary else home
    source_identity = snapshot["folder" if temporary else "home"]
    words = [f"{name}'s held home at {home}"]
    for path, state in snapshot["names"].items():
        target = f" -> {ascii(state[1])}" if state[0] == "link" else ""
        words.append(f"  {path}: {state[0]}{target}")
    if not systems and source_identity is None:
        words.append("The source directory is missing. Remove can finish a verified earlier move.")
    elif temporary and not systems:
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
        if choice == "keep":
            if any(state[0] == "unreadable" for state in snapshot["names"].values()):
                raise ShipError(f"{name}: an unreadable credential name needs inspection; the review hold stays")
            if snapshot["home"] is None:
                raise ShipError(f"{name}: the held home is missing; the review hold stays")
        seat = SimpleNamespace(trusted=trusted_files.folder(ship.runtime, name))
        if choice == "remove" and source_identity is None:
            entry = "seat" if temporary else "home"
            return _finish_prior_move(ship.runtime, name, seat, record, review_identity, entry)
        actor = caller if systems else ship.operator or "the operator"
        act = dict(act=uuid.uuid4().hex[:12], verb="credential-release", at=acts._now(), caller=actor,
                   seat=name, choice=choice, home=snapshot["home"], outcome="pending")
        if choice == "remove":
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            target = ship.runtime / RECOVERY / f"{name}-{stamp}" / ("seat" if temporary else "home")
            act.update(planned_recovery=str(target), source_identity=source_identity, review_identity=review_identity)
        recovery = None
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
                recovery = target
                detail = _clear_moved_hold(record, review_identity, target)
        except (OSError, ShipError) as error:
            detail = f"release failed ({error}); the review hold stays"
            outcome = "failed"
        else:
            outcome = choice
        try:
            _release_outcome(seat, act, outcome, detail, recovery)
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


def _clear_moved_hold(record: Path, review: tuple[int, int, int], target: Path) -> str:
    try:
        if _identity(record.lstat()) != review:
            raise ShipError("the review record changed")
        record.unlink()
    except (OSError, ShipError) as error:
        raise ShipError(f"held home moved to {target}, but its review record could not be cleared ({error})") from None
    return f"held home moved to {target}; credential review cleared"


def _release_outcome(seat, act: dict, outcome: str, detail: str, recovery: Path | None) -> None:
    from . import acts

    result = dict(act=act["act"], verb=act["verb"], at=acts._now(), outcome=outcome, detail=detail)
    if recovery is not None:
        result["recovery"] = str(recovery)
    acts.write(seat, result)


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


def _release_attempts(runtime: Path, name: str) -> list[list[dict]]:
    """Read the service's own act record without following links to a seat's writable files."""
    attempts: dict[str, list[dict]] = {}
    with ExitStack() as stack:
        root = _directory_at(stack, runtime)
        trusted = _directory_at(stack, trusted_files.TRUSTED, parent=root)
        folder = _directory_at(stack, name, parent=trusted)
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW
        with os.fdopen(os.open(trusted_files.ACTS, flags, dir_fd=folder), encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                raise ShipError("the act record is not a trusted regular file")
            for line in stream:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("invalid act record")
                if row.get("verb") == "credential-release":
                    key = row.get("act")
                    if not isinstance(key, str) or not key:
                        raise ValueError("missing credential release act id")
                    attempts.setdefault(key, []).append(row)
    return list(attempts.values())


def _move_plan(runtime: Path, name: str, review: tuple[int, int, int], entry: str,
               attempt: list[dict]) -> tuple[dict, Path, list[int]] | None:
    """Return an unfinished remove's exact path and recorded identity if it belongs to this hold."""
    start, *results = attempt
    if start.get("choice") != "remove" or start.get("seat") != name:
        return None
    outcomes = [result.get("outcome") for result in results]
    if start.get("outcome") != "pending" or outcomes not in ([], ["failed"], ["remove"], ["failed", "remove"]):
        raise ShipError("the release has an ambiguous act history")
    if outcomes and outcomes[-1] == "remove":
        return None

    result = results[-1] if results else None
    planned = start.get("planned_recovery")
    if result is not None and "recovery" not in result:
        detail = result.get("detail")
        prefix = f"release failed (held home moved to {planned}, but its review record could not be cleared ("
        if not isinstance(detail, str) or not detail.startswith(prefix):
            return None  # the previous attempt failed before it confirmed a move

    bound = "review_identity" in start or "source_identity" in start
    if bound and start.get("review_identity") != list(review):
        return None
    target = Path(planned) if isinstance(planned, str) else Path()
    seat, _, stamp = target.parent.name.rpartition("-")
    if (seat != name or not RECOVERY_STAMP.fullmatch(stamp)
            or planned != str(runtime / RECOVERY / target.parent.name / entry)):
        if bound:
            raise ShipError("the act names a different recovery entry")
        return None
    if bound:
        identity = start.get("source_identity")
    else:
        # Earlier acts recorded the home identity, including when the whole temporary seat moved.
        seconds, ns = divmod(review[2], 10**9)
        held_at = datetime.fromtimestamp(seconds, timezone.utc).replace(microsecond=ns // 1000)
        if stamp < held_at.strftime("%Y%m%dT%H%M%S%fZ"):
            return None
        identity = start.get("home")
    if not isinstance(identity, list) or len(identity) != 2 or any(type(part) is not int for part in identity):
        raise ShipError("the act has no source directory identity")
    if result is not None and result.get("recovery", planned) != planned:
        raise ShipError("the act outcome names a different recovery entry")
    return start, target, identity


def _finish_prior_move(runtime: Path, name: str, seat, record: Path, review: tuple[int, int, int], entry: str) -> str:
    """Clear only the still-held review after proving the earlier operator move."""
    try:
        plans = [plan for attempt in _release_attempts(runtime, name)
                 if (plan := _move_plan(runtime, name, review, entry, attempt)) is not None]
        if len(plans) != 1:
            raise ShipError("no matching recorded move" if not plans else "more than one recorded move could match")
        act, target, identity = plans[0]
        with _recovery_root(runtime, create=False) as root, ExitStack() as stack:
            folder = _private_recovery_directory(_directory_at(stack, target.parent.name, parent=root))
            identity_entry = entry
            if entry == "seat" and "source_identity" not in act:
                folder = _directory_at(stack, entry, parent=folder)
                identity_entry = "home"
            info = os.stat(identity_entry, dir_fd=folder, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) or list(_identity(info)[:2]) != identity:
                raise ShipError("the recorded recovery directory identity differs")
            _clear_moved_hold(record, review, target)
            detail = f"earlier move verified at {target} and credential review cleared"
    except (OSError, ShipError, ValueError) as error:
        raise ShipError(f"{name}: could not finish the earlier move ({error}). The review hold stays.") from None
    try:
        _release_outcome(seat, act, "remove", detail, target)
    except (OSError, ShipError) as error:
        raise ShipError(f"{name}: {detail}, but the outcome could not be recorded ({error})") from None
    return f"{name}: {detail}"


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


def _bring_up(source: Path, path: Path, name: str, pool_name: str, *, judged=None, check=lambda: None) -> str:
    """Bring up a private credential copy and report any retry."""
    try:
        if judged is None:
            judged = _file_state(path)
        if judged is None or not stat.S_ISREG(judged[-1]):
            raise CandidateChanged("copy no longer a plain file")
        _reconcile(source, source, path, judged, name, check, copy=True)
    except (CandidateChanged, seat_files.LinkRefused) as error:
        reason = str(error)
        if reason.endswith(COPY_REFRESHED):
            return reason
        return f"{name} copy not brought up to {pool_name}: {reason}; retained"
    return f"{name} copy brought up to {pool_name}"


def _reconcile(source: Path, target: Path, path: Path, judged: tuple, name: str, check, *, copy: bool) -> None:
    """Exchange with the judged file. If a native write or rebinding intervened, put that file back."""
    tmp = path.with_name(f"{path.name}.qd-copy")
    seat_files.unlink(tmp)
    exchanged = False
    refreshed = f"{name} {COPY_REFRESHED}" if copy else f"{name} refreshed while relinking"
    try:
        if copy:
            seat_files.copy(source, tmp, mode=0o600)
        else:
            seat_files.symlink(tmp, target)
        check()
        if _file_state(path) != judged:
            raise CandidateChanged(refreshed)
        _exchange(tmp, path)
        exchanged = True
        try:
            check()
            out = _file_state(tmp)
            # The exchange itself can update ctime.
            if out is None or out[:2] != judged[:2] or out[3:] != judged[3:]:
                raise CandidateChanged(refreshed)
        except (Changed, OSError):
            _exchange(tmp, path)
            exchanged = False
            raise
        exchanged = False  # the old credential was verified and can now be removed
    except (OSError, seat_files.LinkRefused) as error:
        raise CandidateChanged(f"{name}'s credential could not be reconciled ({type(error).__name__})") from None
    finally:
        if not exchanged:
            seat_files.unlink(tmp)


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


@contextmanager
def _park(source: Path, parked: Path):
    """Keep one stale copy in this runtime, publishing it only after a successful install."""
    if not source.exists():
        yield None
        return
    parked.parent.mkdir(parents=True, exist_ok=True)
    temporary = parked.with_name(f".{parked.name}.{uuid.uuid4().hex}.tmp")
    try:
        seat_files.copy(source, temporary, mode=0o600)
        yield parked
        temporary.replace(parked)
    finally:
        temporary.unlink(missing_ok=True)


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
