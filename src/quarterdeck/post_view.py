"""A jailed seat's view of the post: only what the seat is party to, written by the service outside the seat's reach.

A jail runs as the same user, so whatever of the post it can see, it can read: every inbox and the whole store, the
office's mail included. So a jailed seat is never shown the post itself. The service keeps, for each jailed seat, a
folder ``runtime/views/<seat>/post`` laid out as the post is, and the jail binds it read-only in the post's place:

- ``store/``: a copy of every message the seat sent or received, and nothing else. Messages are immutable, so each
  is copied once; a copy, never a hard link, since a link would share the store file's inode with the jail.
- ``inbox/<seat>.md``: the seat's own inbox, as the service renders it.
- the seat's own files of the post's per-seat folders (``PER_SEAT``), such as its watch offer, when they exist.
- ``drop/<seat>/``: an empty mount point; the jail binds the seat's real drop there, writable.
- ``transient/<seat>.lock`` and ``<seat>.check.json``, for a temporary check seat only: empty mount points over which
  the jail binds the seat's own real lease files, read-only. Its own jailed ``qd inbox`` proves the lease by their
  identity and lock; no other jail sees any lease, so none can hold one to make a dead check look live.
- ``shorts``: the short ids already taken (``ord-7f3a``: a kind and four hex digits, no sender, no contents), so a
  new id the seat chooses never takes a short another seat's message holds.

Nothing else of the post is there: no other seat's inbox or messages, no drop holds (whose reasons could tell a seat
whether a message it cannot read exists), no attempts, journals or groups. The view is refreshed before the seat's
launch and on each service pass before deliveries, so a message is in it before it is typed to the seat.
"""

from __future__ import annotations

from pathlib import Path

from . import durable, post, post_dir
from .post_message import short_of

FOLDER = "views"
SHORTS = "shorts"
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


def refresh(runtime: Path, seat: str, store: post.Store | None = None) -> Path:
    """Bring the seat's view up to date with the post, and return its folder. Copies only what is new or changed."""
    runtime = Path(runtime)
    store = store or post.Store(runtime)
    view = folder(runtime, seat)
    for child in (view / "store", view / "inbox", view / "drop" / seat):
        child.mkdir(parents=True, exist_ok=True)
    from .fleet_post import owner
    ship = owner(runtime)
    messages = store.all()
    for message in messages:
        if not party(message, seat, ship):
            continue
        target = view / "store" / f"{message.id}.json"
        if target.exists():
            continue  # immutable: a message once copied never changes
        text = post_dir.read_text(store.dir / f"{message.id}.json")
        durable.replace_file(target, text)
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
    shorts = "".join(f"{short}\n" for short in sorted({short_of(m.id) for m in messages}))
    _write_if_changed(view / SHORTS, shorts)
    return view


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
