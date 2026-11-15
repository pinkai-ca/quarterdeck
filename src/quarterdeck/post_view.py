"""A jailed seat's view of the post: only what the seat is party to, written by the service outside the seat's reach.

A jail runs as the same user, so whatever of the post it can see, it can read: every inbox and the whole store, the
office's mail included. So a jailed seat is never shown the post itself. The service keeps, for each jailed seat, a
folder ``runtime/views/<seat>/post`` laid out as the post is, and the jail binds it read-only in the post's place:

- ``store/``: a copy of every message the seat sent or received, and nothing else. Messages are immutable, so each
  is copied once; a copy, never a hard link, since a link would share the store file's inode with the jail.
- ``inbox/<seat>.md``: the seat's own inbox, as the service renders it.
- the seat's own files of the post's per-seat folders (``PER_SEAT``), such as its watch offer, when they exist.
- ``attempts/``, only while the seat's watch offer stands: the seat's own delivery attempts of the messages the offer
  names, which its jailed wait and ``qd inbox`` read.
- ``drop/<seat>/``: an empty mount point; the jail binds the seat's real drop there, writable.
- ``transient/<seat>.lock`` and ``<seat>.check.json``, for a temporary check seat only: empty mount points over which
  the jail binds the seat's own real lease files, read-only. Its own jailed ``qd inbox`` proves the lease by their
  identity and lock; no other jail sees any lease, so none can hold one to make a dead check look live.
- ``shorts``: the short ids already taken (a kind and its complete hex suffix, no sender or contents), so a
  new id the seat chooses never takes a short another seat's message holds.

Nothing else of the post is there: no other seat's inbox or messages, no drop holds (whose reasons could tell a seat
whether a message it cannot read exists), no other attempts, no journals or groups. The view is refreshed before the seat's
launch, on each service pass before deliveries, and in the door, so a message is in it before it is typed to the seat;
and in the service's filing step as soon as a message leaves the seat's drop, so what the seat posted is never in neither.

A refresh opens only what is new. Beside the views, in ``runtime/views``, which no jail is shown, the index
``.parties`` keeps each store message's sender and addressee as the store reads them, one JSON line appended per
message. A refresh lists the store, reads only the messages the index lacks, and copies only those the seat is party
to and its view lacks; the views of one pass share one listing (``Snapshot``). The service removes a view no seat
should have.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path

from . import durable, post, post_dir
from .post_message import short_of
from .ship import ShipError

FOLDER = "views"
SHORTS = "shorts"
INDEX = ".parties"  # under runtime/views: each message's sender and addressee, for the service alone
# The post's per-seat folders a seat's own file may be shown from: <folder>/<seat><suffix>.
PER_SEAT = (("watch-offers", ".json"),)


def folder(runtime: Path, seat: str) -> Path:
    """Where the seat's view of the post lies: under the runtime, outside the seat's own writable folder."""
    return Path(runtime) / FOLDER / seat / "post"


def party(message: post.Message, seat: str, ship: str | None = None) -> bool:
    """Whether the seat sent or received the message: by its plain name, or qualified by its own ship. A name
    qualified by another ship (``worker-1@beta``) is another ship's seat, never this one."""
    names = {seat} | ({f"{seat}@{ship}"} if ship else set())
    return message.sender in names or message.to in names


class Snapshot:
    """One reading of the store for any number of views: its message names, who sent and received each, and the
    shorts taken. Only a message the index has never seen is opened, so a refresh costs a listing, not a reading
    of every message; the views of one pass share a single snapshot."""

    def __init__(self, runtime: Path):
        from .fleet_post import owner
        self.runtime = Path(runtime)
        self.ship = owner(self.runtime)
        self.dir = self.runtime / "post" / "store"
        try:
            listed = post_dir.list_children(self.dir)
        except FileNotFoundError:
            listed = []
        # A link in the store is a hold the store itself reports; a view never reads through one.
        self.names = sorted(name[:-5] for name, mode in listed if name.endswith(".json") and stat.S_ISREG(mode))
        self.parties = _index(self.runtime, self.dir, self.names)
        self.shorts = "".join(f"{short}\n" for short in sorted({short_of(name) for name in self.names}))

    def party(self, seat: str) -> list[str]:
        """The names of the messages the seat sent or received, of those the index could read."""
        names = {seat} | ({f"{seat}@{self.ship}"} if self.ship else set())
        return [name for name in self.names
                if (found := self.parties.get(name)) is not None and (found[0] in names or found[1] in names)]


def refresh(runtime: Path, seat: str, snapshot: Snapshot | None = None) -> Path:
    """Bring the seat's view up to date with the post, and return its folder. Copies only what is new or changed."""
    runtime = Path(runtime)
    snapshot = snapshot if snapshot is not None else Snapshot(runtime)
    view = folder(runtime, seat)
    for child in (view / "store", view / "inbox", view / "drop" / seat):
        child.mkdir(parents=True, exist_ok=True)
    copied = {name for name in os.listdir(view / "store") if name.endswith(".json")}
    for name in snapshot.party(seat):
        if f"{name}.json" in copied:
            continue  # immutable: a message once copied never changes
        text = post_dir.read_text(snapshot.dir / f"{name}.json")
        durable.replace_file(view / "store" / f"{name}.json", text)
    _mirror(runtime / "post" / "inbox" / f"{seat}.md", view / "inbox" / f"{seat}.md")
    for name, suffix in PER_SEAT:
        _mirror(runtime / "post" / name / f"{seat}{suffix}", view / name / f"{seat}{suffix}")
    for name in lease_names(seat):
        # A check seat's own lease: the jail binds the real file over this placeholder, so its own jailed qd inbox
        # proves the lease by the file's identity and its lock. No other seat's lease is ever in a view.
        placeholder = view / "transient" / name
        if (runtime / "post" / "transient" / name).is_file():
            placeholder.parent.mkdir(parents=True, exist_ok=True)
            if not placeholder.exists():
                placeholder.touch()
        elif placeholder.exists():
            placeholder.unlink()
    _offered_attempts(runtime, seat, view)
    _write_if_changed(view / SHORTS, snapshot.shorts)
    return view


def _offered_attempts(runtime: Path, seat: str, view: Path) -> None:
    """A brokered watch's own delivery attempts, while its offer stands: the seat's attempts of the messages the
    service's offer names, and nothing else of ``attempts/``. The jailed wait reads them to know its offer is still
    open, and ``qd inbox`` to say a message given before is a second delivery. No offer, no attempts folder."""
    from . import watch_broker
    target = view / "attempts"
    wanted: dict[str, str] = {}
    offer = watch_broker.read_offer(runtime, seat)
    if offer is not None:
        messages = set(offer["messages"])
        prefixes = tuple(f"{message}-" for message in messages)
        source = runtime / "post" / "attempts"
        try:
            listed = post_dir.list_children(source) if prefixes else []
        except FileNotFoundError:
            listed = []
        for name, mode in listed:
            if not (name.endswith(".json") and name.startswith(prefixes) and stat.S_ISREG(mode)):
                continue
            try:
                text = post_dir.read_text(source / name)
                data = json.loads(text)
            except (OSError, ValueError, ShipError):
                continue
            if isinstance(data, dict) and data.get("seat") == seat and data.get("message") in messages:
                wanted[name] = text
    if wanted:
        target.mkdir(exist_ok=True)
        for name, text in wanted.items():
            _write_if_changed(target / name, text)
    if target.is_dir():
        for name in os.listdir(target):
            if name not in wanted:
                (target / name).unlink()
        if not wanted:
            target.rmdir()


def seats(runtime: Path) -> list[str]:
    """The seats that have a view now."""
    try:
        with os.scandir(Path(runtime) / FOLDER) as entries:
            return sorted(entry.name for entry in entries
                          if entry.is_dir(follow_symlinks=False) and not entry.name.startswith("."))
    except FileNotFoundError:
        return []


def remove(runtime: Path, seat: str) -> None:
    """Remove a seat's view whole: the service's own copies, never anything they name. A link is not followed."""
    if not seat or seat.startswith(".") or "/" in seat:
        raise ShipError(f"no view for seat {seat!r}")
    shutil.rmtree(Path(runtime) / FOLDER / seat)


# The index: for each store message its sender and its addressee, as the store reads them, one JSON line each,
# appended as messages arrive. It lies beside the views, under runtime/views, which no jail sees. A process keeps
# what it has read of it and reads on only from where it stopped.
_INDEXES: dict[Path, tuple[tuple[int, int], int, dict[str, tuple[str, str]]]] = {}


def _index(runtime: Path, store: Path, names: list[str]) -> dict[str, tuple[str, str]]:
    path = runtime / FOLDER / INDEX
    parties = _read_index(path)
    missing = [name for name in names if name not in parties]
    lines = []
    for name in missing:
        found = _parties_of(runtime, store / f"{name}.json")
        if found is not None:
            parties[name] = found
            lines.append(json.dumps([name, *found]) + "\n")
    if lines:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            os.write(descriptor, "".join(lines).encode("utf-8"))  # whole lines, in one write
        finally:
            os.close(descriptor)
    return parties


def _read_index(path: Path) -> dict[str, tuple[str, str]]:
    key = path.absolute()
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        _INDEXES.pop(key, None)
        return {}
    try:
        info = os.fstat(descriptor)
        identity = (info.st_dev, info.st_ino)
        known = _INDEXES.get(key)
        offset, parties = (known[1], known[2]) if known and known[0] == identity and known[1] <= info.st_size else (0, {})
        os.lseek(descriptor, offset, os.SEEK_SET)
        chunks = []
        while chunk := os.read(descriptor, 1 << 20):
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    tail = b"".join(chunks)
    whole = tail[:tail.rfind(b"\n") + 1]  # a line still being appended is read on the next refresh
    for line in whole.splitlines():
        try:
            name, sender, to = json.loads(line)
        except (ValueError, TypeError):
            continue  # never trusted half: the message is read again from the store
        if all(isinstance(value, str) for value in (name, sender, to)):
            parties[name] = (sender, to)
    _INDEXES[key] = (identity, offset + len(whole), parties)
    return parties


def _parties_of(runtime: Path, path: Path) -> tuple[str, str] | None:
    """A message's sender and addressee, read as the store reads them; None for one that cannot be read now."""
    from .fleet_post import local_view, provenance, stable_bytes
    try:
        if provenance(runtime, path.stem) is None:
            message = local_view(runtime, post.Message.from_json(post_dir.read_text(path)))
        else:
            data = stable_bytes(path)
            message = local_view(runtime, post.Message.from_json(data.decode("utf-8")), data=data)
    except (OSError, ValueError, TypeError, KeyError, ShipError):
        return None
    return message.sender, message.to


def lease_names(seat: str) -> tuple[str, str]:
    """A temporary check seat's lease files under post/transient: its lock and its registration."""
    return f"{seat}.lock", f"{seat}.check.json"


def taken_shorts(post_folder: Path) -> set[str]:
    """The short ids a view says are taken; empty outside a view, where the store itself is whole."""
    try:
        return {line for line in post_dir.read_text(Path(post_folder) / SHORTS).splitlines() if line}
    except (OSError, ValueError):
        return set()


def _mirror(source: Path, target: Path) -> None:
    try:
        text = post_dir.read_text(source)
    except FileNotFoundError:
        if target.exists():
            target.unlink()
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_if_changed(target, text)


def _write_if_changed(path: Path, text: str) -> None:
    try:
        if path.read_text() == text:
            return
    except FileNotFoundError:
        pass
    durable.replace_file(path, text)
