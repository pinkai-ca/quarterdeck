"""Post: orders, reports, questions, answers and acknowledgments as immutable files.

Each message is one JSON file in ``post/store/``, the canonical copy (law: post-is-files).
Only a report from the order's addressee closes an order; an ack claims it for the Ledger
and closes nothing (law: open-until-closed). ``post/inbox/<seat>.md`` is the rendered
view: open obligations first, oldest first, then what was already delivered.
"""

from __future__ import annotations

import json
import hashlib
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
import os
import secrets
import stat
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

from . import durable, post_dir, ship_locks
from .ship import ShipError
from .post_message import KINDS, REPLY_VERB, SECOND, Message, delivered_forms, short_of, typed

SPEEDS = ("now", "interrupt")
SHIP = "ship"  # the sender of the ship's own notices, and an inbox a seat may answer to
POST_TIMESTAMP_TICK_NS = 2_000_000_000  # conservative filesystem tick; see docs/design/post.md
_FIXED_REPORT = ContextVar('quarterdeck_fixed_report', default=None)
_READING = ContextVar('quarterdeck_post_reading', default=None)
_READERS = ContextVar('quarterdeck_post_readers', default=None)


@dataclass
class _Reading:
    stores: dict = field(default_factory=dict)
    attempts: dict = field(default_factory=dict)
    values: dict = field(default_factory=dict)


@contextmanager
def reader_scope(*, store=None, attempts=None):
    """Reuse validated readers, never their snapshots, across separate readings."""
    if _READERS.get() is not None:
        yield
        return
    readers = _Reading()
    if store is not None and isinstance(getattr(store, 'dir', None), Path) and callable(getattr(store, '_load_current', None)):
        readers.stores[store.dir.absolute()] = store
    if attempts is not None and isinstance(getattr(attempts, 'dir', None), Path) and callable(getattr(attempts, '_all_current', None)):
        readers.attempts[attempts.dir.absolute()] = attempts
    token = _READERS.set(readers)
    try:
        yield
    finally:
        _READERS.reset(token)


@contextmanager
def reading_pass(*, store=None, attempts=None):
    """One post/attempt view for a read-only presentation, discarded when it ends."""
    if _READING.get() is not None:
        yield
        return
    reading = _Reading()
    readers = _READERS.get()
    if readers is not None:
        reading.stores = readers.stores
        reading.attempts = readers.attempts
    if store is not None and isinstance(getattr(store, 'dir', None), Path) and callable(getattr(store, '_load_current', None)):
        reading.stores[store.dir.absolute()] = store
    if attempts is not None and isinstance(getattr(attempts, 'dir', None), Path) and callable(getattr(attempts, '_all_current', None)):
        reading.attempts[attempts.dir.absolute()] = attempts
    token = _READING.set(reading)
    try:
        yield
    finally:
        _READING.reset(token)


def reading_value(kind, key, read):
    """Share successful observations only inside the current presentation."""
    reading = _READING.get()
    if reading is None:
        return read()
    key = kind, key
    if key not in reading.values:
        reading.values[key] = read()
    return reading.values[key]


def reading_names(folder):
    """A presentation's directory inventory; None outside a read-only view."""
    if _READING.get() is None:
        return None
    def read():
        try:
            return frozenset(name for name, _ in post_dir.list_children(folder))
        except FileNotFoundError:
            return frozenset()
    return reading_value('names', folder.absolute(), read)


def reading_missing(folder, name):
    """A valid absent leaf in this view; other names keep the ordinary reader."""
    if not post_dir._plain_name(name):
        return False
    names = reading_names(folder)
    return names is not None and name not in names


@contextmanager
def fixed_report(store, message):
    """Retain the four-field notification sink while publishing a saved identity.

    The caller owns admission and the durable intent. Small injected sinks keep
    their ordinary ``new`` interface; the real Store writes these exact bytes.
    """
    token = _FIXED_REPORT.set((store, message))
    try:
        yield
    finally:
        _FIXED_REPORT.reset(token)


def report_sink(message, default):
    fixed = _FIXED_REPORT.get()
    return fixed[0] if fixed is not None and fixed[1] == message else default


def now() -> datetime:
    return datetime.now().astimezone()


def __getattr__(name):
    if name == 'LockBusy':  # the bounded wait's refusal, by the name the jailed wait uses
        from .ship_locks import Busy
        return Busy
    raise AttributeError(name)


@contextmanager
def admission(runtime: Path, seat: str, *, wait: float | None = None):
    """Serialize publication/transition claims, never program startup; locks are not reentrant.

    The lock lives under runtime/locks, which every jail hides, and the wait for it is bounded."""
    from . import ship_locks
    name = hashlib.sha256(seat.encode()).hexdigest()
    with ship_locks.held(ship_locks.path(runtime, 'admission', name + '.lock'), f'{seat}: post admission',
                         wait=ship_locks.WAIT if wait is None else wait):
        yield


class Store:
    """Every message of a ship, one immutable file each."""

    def __init__(self, runtime: Path):
        self.dir = runtime / "post" / "store"
        self._cache: tuple[tuple[object, ...], list[Message], dict[str, list[Message]]] | None = None
        self._cache_at: int | None = None
        self._validated: dict[str, tuple[tuple[int, ...], object, Message, int | None]] = {}
        self._validated_context: tuple[object, ...] | None = None
        self._reference_messages: list[Message] | None = None
        self._references: dict[str, list[Message]] = {}
        self._short_messages: list[Message] | None = None
        self._short_set: frozenset[str] = frozenset()

    def new(self, kind: str, sender: str, to: str, body: str, **fields) -> Message:
        fixed = _FIXED_REPORT.get()
        if fixed is not None and fixed[0] is self:
            message = fixed[1]
            if (kind, sender, to, body) != (message.kind, message.sender, message.to, message.body) or fields:
                raise ShipError('fixed notification differs from its saved intent')
            self._write(message)  # its publisher already owns admission
            return message
        message = self.prepare(kind, sender, to, body, **fields)
        self.write(message)
        from .fleet_post import local_view
        from .forward_post import projected
        return projected(self.dir.parent.parent, local_view(self.dir.parent.parent, message),
                         open_obligation=message.kind in ("order", "question"))

    def prepare(self, kind: str, sender: str, to: str, body: str, *, taken: set[str] | None = None,
                **fields) -> Message:
        """Construct an immutable intent before publication, without changing its format.

        ``taken`` is a filing round's set of shorts (see ``_new_id``); the short chosen here is added to it."""
        if kind not in KINDS:
            raise ShipError(f"no message kind {kind!r}; the kinds are {', '.join(KINDS)}")
        if fields.get("speed") not in (None, *SPEEDS):
            raise ShipError(f"no speed {fields['speed']!r}; --now or --interrupt")
        if fields.get("re"):
            fields["re"] = self.get(fields["re"]).id
        from .fleet_post import wire_parties
        sender, to = wire_parties(self.dir.parent.parent, sender, to)
        at = now()
        message = Message(self._new_id(kind, sender, at, taken=taken), kind, sender, to, at, body, **fields)
        return message

    def _new_id(self, kind: str, sender: str, at: datetime, *, taken: set[str] | None = None) -> str:
        """A fresh id whose short no stored message and no listed drop holds, in a bounded number of draws.

        Avoided: the store's shorts, and either a filing round's ``taken`` (the shorts it has prepared and the
        drop windows it has listed, to which the chosen short is added) or, outside a round, each drop folder's
        window of DROP_MAX_FILES names. No drop folder is listed past its window: a queued short beyond it may
        be drawn again, and Store.get then refuses that short as ambiguous, never guesses. The sender's own
        drop is asked by name for the exact candidate, which no window can miss. After ID_DRAWS draws it
        refuses with a fixed reason; it never spins.
        """
        avoid = set(self._shorts())
        avoid.update(window_shorts(self.dir.parent.parent) if taken is None else taken)
        from .post_view import taken_shorts
        avoid.update(taken_shorts(self.dir.parent))  # inside a jail's view, the shorts other seats' messages hold
        try:
            own = drop_dir(self.dir.parent.parent, sender)
        except ShipError:
            own = None
        for _ in range(ID_DRAWS):
            candidate = f"{kind[:3]}-{at.strftime('%Y%m%d-%H%M')}-{sender}-{secrets.token_hex(2)}"
            short = short_of(candidate)
            if short in avoid or (own is not None and not _absent(own / f"{candidate}.json")):
                continue
            if taken is not None:
                taken.add(short)
            return candidate
        raise IdSpaceExhausted(ID_SPACE_REASON)

    def write(self, message: Message) -> Path:
        return self._admitted(message, lambda: self._write(message))

    def publish_text(self, message: Message, text: str) -> Path:
        """File a drop's exact text. Admission is taken here; the bytes are not rewritten."""
        if Message.from_json(text) != message:
            raise ShipError("drop bytes are not this message")
        return self._admitted(message, lambda: self._write_text(message, text))

    def _admitted(self, message: Message, publish) -> Path:
        """Take admission for the message's parties, then publish it through the given writer."""
        runtime = self.dir.parent.parent
        from .fleet_post import local_view
        viewed = local_view(runtime, message)
        names = {viewed.to}
        if message.kind == 'report' and message.re:
            # A closing report is by the order's addressee, regardless of report recipient.
            names.add(viewed.sender)
        with ExitStack() as held:
            for name in sorted(names):
                held.enter_context(admission(runtime, name))
            from . import compaction
            compaction.protect_closure(runtime, viewed)
            compaction.admitting(runtime, viewed)
            return publish()

    def _write(self, message: Message) -> Path:
        """Publish while the caller holds admission; fixed-intent recovery verifies first."""
        return self._write_text(message, message.to_json())

    def _write_text(self, message: Message, text: str) -> Path:
        """Publish while the caller holds admission. A name already there is refused, link or not."""
        post_dir.ensure_directory(self.dir)
        if '@' in message.to:
            from .fleet_post import prepare_outbound
            prepare_outbound(self.dir.parent.parent)
        from .forward_post import prepare
        prepare(self.dir.parent.parent, message)
        path = self.dir / f"{message.id}.json"
        if post_dir.exists(path):
            raise ShipError(f"{message.id} exists; messages are immutable")  # law: post-is-files
        durable.replace_file(path, text)
        self._cache = None
        self._cache_at = None
        return path

    def _loaded(self) -> tuple[list[Message], dict[str, list[Message]]]:
        reading = _READING.get()
        if reading is not None:
            key = self.dir.absolute()
            reader = reading.stores.setdefault(key, self)
            return reading_value('store', key, reader._load_current)
        return self._load_current()

    def _refresh_messages(self, children, context, started):
        """Reuse immutable files only after a complete, unchanged identity inventory."""
        from .fleet_post import local_view, provenance, stable_bytes
        runtime = self.dir.parent.parent
        previous = self._validated
        self._validated = {}  # a failed refresh cannot leave a partial certificate
        self._validated_context, old_context = context, self._validated_context
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_mode)
        inventory = []
        reread = context != old_context
        for name, info in sorted(children):
            if not name.endswith('.json'):
                continue
            path = self.dir / name
            if stat.S_ISLNK(info.st_mode):
                if post_dir.gone(path):
                    continue
                post_dir._link_held(path)
            if not stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                continue
            record = provenance(runtime, path.stem)
            key = identity(info)
            cached = previous.get(path.name)
            if cached is not None and (cached[0] != key or cached[1] != record):
                reread = True
            inventory.append((path, info, key, record, cached))
        if previous.keys() - {path.name for path, *_ in inventory}:
            reread = True
        messages, validated = [], {}
        for path, info, key, record, cached in inventory:
            if cached is not None and cached[3] is not None and cached[3] <= started and not reread:
                message = cached[2]
                validated[path.name] = cached
            else:
                try:
                    if record is None:
                        message = local_view(runtime, Message.from_json(post_dir.read_text(path)))
                    else:
                        data = stable_bytes(path)
                        message = local_view(runtime, Message.from_json(data.decode('utf-8')), data=data)
                except post_dir.NotRegular:
                    continue
                except FileNotFoundError:
                    if not post_dir.gone(path):
                        raise
                    continue
                try:
                    after = path.lstat()
                except FileNotFoundError:
                    after = None
                checked = time.time_ns()
                # A recent read cannot certify a coarse or racing file timestamp.
                if after is not None and identity(after) == key and stat.S_ISREG(info.st_mode):
                    certified = (started if max(info.st_mtime_ns, info.st_ctime_ns)
                                 + POST_TIMESTAMP_TICK_NS < started <= checked else None)
                    validated[path.name] = (key, record, message, certified)
            messages.append(message)
        self._validated = validated
        return messages

    def _load_current(self) -> tuple[list[Message], dict[str, list[Message]]]:
        """Immutable messages and replies; recent directory listings stay untrusted."""
        cached_at = self._cache_at
        self._cache_at = None  # a failed refresh cannot certify the previous listing
        started = time.time_ns()
        if not post_dir.under_post(self.dir):
            try:
                info = self.dir.stat()
            except FileNotFoundError:
                self._cache = None
                return [], {}
            checked = time.time_ns()
            from .forward_post import stamp as forwarding_stamp
            stamp = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns,
                     forwarding_stamp(self.dir.parent.parent))
            if (self._cache is None or self._cache[0] != stamp or cached_at is None
                    or not cached_at <= started <= checked):
                with os.scandir(self.dir) as entries:
                    paths = [self.dir / entry.name for entry in entries if entry.name.endswith('.json')]
                from .fleet_post import local_view, provenance, stable_bytes

                def read(path):
                    if provenance(self.dir.parent.parent, path.stem) is None:
                        return local_view(self.dir.parent.parent, Message.from_json(path.read_text()))
                    data = stable_bytes(path)
                    return local_view(self.dir.parent.parent, Message.from_json(data.decode('utf-8')), data=data)

                messages = []
                for path in paths:
                    try:
                        messages.append(read(path))
                    except FileNotFoundError:
                        if not post_dir.gone(path):
                            raise
                messages.sort(key=lambda m: (m.at, m.id))
                replies: dict[str, list[Message]] = {}
                for message in messages:
                    if message.re:
                        replies.setdefault(message.re, []).append(message)
                # Decide openness before projecting declarations: an old closing report
                # must keep its order closed, and settled history keeps its old address.
                from .forward_post import declared, projected
                routes = declared(self.dir.parent.parent)
                if routes:
                    messages = [projected(self.dir.parent.parent, m, open_obligation=(
                        m.kind in ("order", "question") and not any(
                            r.kind == ("report" if m.kind == "order" else "answer")
                            and (m.kind != "order" or r.sender == m.to)
                            for r in replies.get(m.id, ())))) if m.to in routes else m for m in messages]
                    replies = {}
                    for message in messages:
                        if message.re:
                            replies.setdefault(message.re, []).append(message)
                self._cache = (stamp, messages, replies)
                finished = time.time_ns()
                # Certify only a listing begun after the entire timestamp bucket.
                # An unsafe listing must be refreshed even when its stamp has aged.
                if max(info.st_mtime_ns, info.st_ctime_ns) + POST_TIMESTAMP_TICK_NS < started <= checked <= finished:
                    self._cache_at = started
            else:
                self._cache_at = cached_at
            return self._cache[1], self._cache[2]
        try:
            descriptor = post_dir.open_directory(self.dir, create=False)
        except FileNotFoundError:
            self._cache = None
            self._validated = {}
            return [], {}
        try:
            info = os.fstat(descriptor)
            checked = time.time_ns()
            from .forward_post import stamp as forwarding_stamp
            stamp = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns,
                     forwarding_stamp(self.dir.parent.parent))
            if (self._cache is None or self._cache[0] != stamp or cached_at is None
                    or not cached_at <= started <= checked):
                try:
                    children = post_dir.list_children(self.dir, with_stat=True)
                except FileNotFoundError:
                    children = []
                messages = self._refresh_messages(children, (stamp[0], stamp[1], stamp[4]), started)
                messages.sort(key=lambda m: (m.at, m.id))
                replies: dict[str, list[Message]] = {}
                for message in messages:
                    if message.re:
                        replies.setdefault(message.re, []).append(message)
                # Decide openness before projecting declarations: an old closing report
                # must keep its order closed, and settled history keeps its old address.
                from .forward_post import declared, projected
                routes = declared(self.dir.parent.parent)
                if routes:
                    messages = [projected(self.dir.parent.parent, m, open_obligation=(
                        m.kind in ("order", "question") and not any(
                            r.kind == ("report" if m.kind == "order" else "answer")
                            and (m.kind != "order" or r.sender == m.to)
                            for r in replies.get(m.id, ())))) if m.to in routes else m for m in messages]
                    replies = {}
                    for message in messages:
                        if message.re:
                            replies.setdefault(message.re, []).append(message)
                self._cache = (stamp, messages, replies)
                finished = time.time_ns()
                # Certify only a listing begun after the entire timestamp bucket.
                # An unsafe listing must be refreshed even when its stamp has aged.
                if (all(entry[3] is not None for entry in self._validated.values())
                        and max(info.st_mtime_ns, info.st_ctime_ns) + POST_TIMESTAMP_TICK_NS
                        < started <= checked <= finished):
                    self._cache_at = started
            else:
                self._cache_at = cached_at
            return self._cache[1], self._cache[2]
        finally:
            os.close(descriptor)

    def all(self) -> list[Message]:
        """Every message, oldest first."""
        return list(self._loaded()[0])

    def _shorts(self) -> frozenset[str]:
        """The stored shorts, built once for each load of the store as get's index is, not once for each id."""
        messages = self._loaded()[0]
        if messages is not self._short_messages:
            self._short_messages, self._short_set = messages, frozenset(m.short for m in messages)
        return self._short_set

    def get(self, ref: str) -> Message:
        """A message by its id or its short form; ambiguity refused, never guessed."""
        messages = self._loaded()[0]
        if messages is not self._reference_messages:
            references: dict[str, list[Message]] = {}
            for message in messages:
                for name in {message.id, message.short}:
                    references.setdefault(name, []).append(message)
            self._reference_messages, self._references = messages, references
        found = self._references.get(ref, ())
        if not found:
            raise ShipError(f"no message {ref!r} in {self.dir}")
        if len(found) > 1:
            raise ShipError(f"{ref!r} names {len(found)} messages; use the full id")
        return found[0]

    def for_seat(self, seat: str) -> list[Message]:
        from .fleet_post import context_only
        runtime = self.dir.parent.parent
        def read():
            return [m for m in self.all() if m.to == seat and not context_only(runtime, m.id)]
        return list(reading_value('seat', (self.dir.absolute(), seat), read))

    def replies(self, message: Message) -> list[Message]:
        return list(self._loaded()[1].get(message.id, []))

    def closing_report(self, order: Message) -> Message | None:
        """The report from the addressee that closes the order, if any."""
        return next((m for m in self.replies(order) if m.kind == "report" and m.sender == order.to), None)  # law: open-until-closed

    def closing(self, message: Message) -> Message | None:
        """What closed a message, by the one rule of its kind (law: open-until-closed).

        An order is closed by its addressee's report, a question by an answer, the first in time.
        Any other kind is never open: it stands closed by itself.
        """
        if message.kind == "order":
            return self.closing_report(message)
        if message.kind == "question":
            return min((m for m in self.replies(message) if m.kind == "answer"), key=lambda m: m.at, default=None)
        return message

    def is_open(self, message: Message) -> bool:
        return self.closing(message) is None

    def open_for(self, seat: str) -> list[Message]:
        return [m for m in self.for_seat(seat) if self.is_open(m)]

    def claimed(self, seat: str, before: datetime) -> str | None:
        """The message the seat last claimed with an ack and still had open at ``before``, for the Ledger.

        A claim ends when its message closes, by the same rule as ``is_open``, read at ``before``:
        an order by its addressee's report, a question by an answer. A message closed later was
        still claimed at an earlier ``before``.
        """
        messages = self.all()
        by_id = {m.id: m for m in messages}
        for ack in reversed([m for m in messages if m.kind == "ack" and m.sender == seat and m.re and m.at <= before]):
            claim = by_id.get(ack.re)
            closed = self.closing(claim) if claim else None
            if claim and (closed is None or closed.at > before):
                return claim.id
        return None


# -- drop folders ------------------------------------------------------------------------

# A seat writes a new message only here. The service files it and is the one that sets the sender.
DROP_MAX_FILES = 128
ID_DRAWS = 64  # a short's suffix has 65,536 values per kind; a draw this many times taken refuses, never spins
ID_SPACE_REASON = "no free short id"
DROP_COUNT_REASON = "drop filing budget reached repeatedly"
DROP_STALLED_REASON = "drop filing window made no progress"
DROP_BACKLOG_ROUNDS = 3
DROP_MAX_BYTES = 1024 * 1024
DROP_SIZE_REASON = "drop exceeds size limit"
DROP_HOLD_REASONS = (
    "from names another seat",
    "not a regular file",
    "id already stored",
    "not a message",
    "drop folder is not a directory",
    "drop unreadable",
    # a staged group intent's own reasons, one for each way it is held (group_post.INTENT_REASONS)
    "group intent unreadable",
    "group intent malformed",
    "group intent names another root",
    "group not declared",
    "group root not found",
    "group root id malformed",
    "group root not an order to the lead",
    "forward of an order not open in the store",
    "link held in the post folder",
    "group publication refused",
    "group publication I/O error",
    "group journal malformed",
    DROP_SIZE_REASON,
    DROP_COUNT_REASON,
    DROP_STALLED_REASON,
    ID_SPACE_REASON,
)


class IdSpaceExhausted(ShipError):
    """No free short drawn in ID_DRAWS tries: refused with the one fixed reason."""


def drop_dir(runtime: Path, seat: str) -> Path:
    if not seat or seat in {".", ".."} or "/" in seat or "\\" in seat:
        raise ShipError(f"no drop folder for sender {seat!r}")
    return runtime / "post" / "drop" / seat


def id_sender(message_id: str) -> str | None:
    """The seat named by ``ord-<date>-<time>-<seat>-<suffix>``, or None when the id has another shape.

    The seat is everything between the minute and the four-hex suffix, so a name may contain hyphens.
    """
    parts = message_id.split("-")
    if len(parts) < 5:
        return None
    kind, date, minute, suffix = parts[0], parts[1], parts[2], parts[-1]
    sender = "-".join(parts[3:-1])
    if kind not in {name[:3] for name in KINDS}:
        return None
    if len(date) != 8 or not date.isdigit() or len(minute) != 4 or not minute.isdigit():
        return None
    if len(suffix) != 4 or any(character not in "0123456789abcdef" for character in suffix):
        return None
    if not sender or sender in {".", ".."} or "/" in sender or "\\" in sender:
        return None
    return sender


def write_drop(runtime: Path, message: Message, *, seat: str | None = None) -> Path:
    """The seat's write: one regular file in its own drop folder, returned before the service files it.

    The bytes are the message's own JSON. The service copies them, or holds them (law: can-post).
    The walk is the post folder's own: a link on the way is a hold, never followed. ``seat`` names
    the folder when the sender is written in its ship-qualified form, for a post to another ship.
    """
    folder = drop_dir(runtime, seat or message.sender)
    post_dir.ensure_directory(folder)
    path = folder / f"{message.id}.json"
    if _mode(path) is not None:
        raise ShipError(f"{message.id} exists; messages are immutable")  # law: post-is-files
    durable.replace_file(path, message.to_json())
    return path


REPEATS_REFUSED = ("report", "answer", "ack")  # an order or a question is never deduplicated


def already_posted(store: Store, message: Message, *, seat: str | None = None) -> Message | None:
    """The message this seat already posted with the same kind, re and byte-identical body, or None.

    Only a report, an answer or an ack that names a message is checked: posting the same one twice
    says nothing new, and a seat that took the command's answer for a failure would otherwise file
    it again and again. A post with no re is never refused, so a standing report may recur. An ack is
    refused only while the identical one is still the seat's latest ack: the Ledger reads the latest
    claim, so acking A, then B, then A again moves the claim back and is posted.

    The search reads only what the poster can see: its own drop folder, then the replies to that re
    in the post store as it is shown here (a jailed seat's view of it), from the reply map the store's
    load already builds, never a pass over every message. An ack also looks at the messages stored
    after the one it matches, newest first, and stops there. The drop is read first, so a drop the
    service files between the two readings is found in the store by the second. Nothing is written or
    widened.
    """
    if message.kind not in REPEATS_REFUSED or not message.re:
        return None
    runtime = store.dir.parent.parent
    folder = seat or message.sender
    senders = _drop_senders(runtime, folder) | {message.sender}

    def mine(found: Message) -> bool:
        return found.id != message.id and found.kind == message.kind and found.sender in senders

    drops = [found for found in _own_drops(runtime, folder) if mine(found)]
    stored, replies = store._loaded()
    same = [found for found in (*drops, *replies.get(message.re, ()))
            if mine(found) and found.re == message.re and found.body == message.body]
    if not same:
        return None
    order = lambda found: (found.at, found.id)
    if message.kind != "ack":
        return min(same, key=order)
    latest = max(same, key=order)
    if any(order(found) > order(latest) for found in drops):
        return None  # a later claim waits in the drop
    for index in range(len(stored) - 1, -1, -1):  # newest first, stopping at the matching ack
        found = stored[index]
        if order(found) <= order(latest):
            break
        if mine(found):
            return None  # a later claim moved the seat on: this ack moves it back
    return latest


def _own_drops(runtime: Path, seat: str) -> list[Message]:
    """The messages still waiting in one seat's drop folder, by the no-follow walk; a name that is no message is skipped."""
    folder = drop_dir(runtime, seat)
    try:
        files = post_dir.list_children(folder)
    except (OSError, ShipError):
        return []
    found = []
    for name, kind in sorted(files):
        if not stat.S_ISREG(kind) or not name.endswith(".json") or name.endswith(".group.json"):
            continue
        try:
            text = _read_regular(folder / name)
            if text is not None:
                found.append(Message.from_json(text))
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    return found


def _mode(path: Path) -> int | None:
    """The name's own mode, never following it; None when missing. A link on the way is a hold."""
    return post_dir.mode(path)


def _regular(path: Path) -> bool:
    try:
        found = _mode(path)
    except (OSError, ShipError):
        return False
    return found is not None and stat.S_ISREG(found)


def _read_regular(path: Path, *, limit: int | None = None) -> str | None:
    """The text of a regular file, or None when the name is missing, a link or not a regular file.

    The post folder's no-follow read: a link is never read through, and a name swapped during
    the read is refused. A caller's size-limit refusal propagates as ReadLimit so the drop
    filing door can record its fixed folder hold.
    """
    try:
        return post_dir.read_text(path, limit=limit)
    except post_dir.ReadLimit:
        raise
    except (OSError, ShipError):
        return None


def _absent(path: Path) -> bool:
    """True only when the name is certainly missing; a link, an unreadable way or any other name is taken."""
    try:
        return _mode(path) is None
    except (OSError, ShipError):
        return False


def window_shorts(runtime: Path) -> set[str]:
    """Short ids queued in each drop folder's window: at most DROP_MAX_FILES names listed per folder.

    Nothing here raises for a link: a seat's planted link must not stop another writer's id.
    """
    root = runtime / "post" / "drop"
    try:
        seats = post_dir.list_children(root)
    except (OSError, ShipError):
        return set()
    found: set[str] = set()
    for name, mode in seats:
        if not stat.S_ISDIR(mode):
            continue
        try:
            found |= queued_shorts(post_dir.list_children(root / name, limit=DROP_MAX_FILES))
        except (OSError, ShipError):
            continue
    return found


def queued_shorts(files) -> set[str]:
    """The shorts of the queued messages in one listed window of (name, mode) pairs."""
    return {short_of(file[:-5]) for file, kind in files
            if stat.S_ISREG(kind) and file.endswith(".json") and not file.endswith(".group.json")
            and "-" in file[:-5]}


DROP_WAIT_SECONDS = 120.0


def drop_waits(runtime: Path, *, at: float | None = None) -> list[str]:
    """Aged drop folders from no-follow metadata only; no message bytes or names are displayed."""
    root = runtime / 'post' / 'drop'
    cutoff = (time.time() if at is None else at) - DROP_WAIT_SECONDS
    try:
        children = post_dir.list_children(root)
    except FileNotFoundError:
        return []
    except (OSError, ShipError):
        return ['post drop waiting: drop (age unknown)']
    lines = []
    for name, mode in sorted(children):
        if not stat.S_ISDIR(mode):
            continue  # the existing hold reader names non-directory entries
        folder = f'drop/{name}'
        descriptor = None
        try:
            files = post_dir.list_children(root / name, limit=DROP_MAX_FILES)
            descriptor = post_dir.open_directory(root / name, create=False)
            for file, _ in files:
                try:
                    info = os.stat(file, dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(info.st_mode) and info.st_mtime <= cutoff:
                    lines.append(f'post drop waiting: {folder} (over 2m)')
                    break
        except FileNotFoundError:
            if not post_dir.gone(root / name):
                lines.append(f'post drop waiting: {folder} (age unknown)')
        except (OSError, ShipError):
            lines.append(f'post drop waiting: {folder} (age unknown)')
        finally:
            if descriptor is not None:
                os.close(descriptor)
    return lines


def _hold_dir(runtime: Path) -> Path:
    return runtime / "post" / "drop-holds"


def _hold_name(folder: str, reason: str) -> str:
    return hashlib.sha256(f"{folder}\n{reason}".encode()).hexdigest()[:16]


def drop_alarms(runtime: Path) -> list[str]:
    """Status-line lines for drops the service refused. Each names the folder."""
    folder = _hold_dir(runtime)
    try:
        children = post_dir.list_children(folder)
    except FileNotFoundError:
        return []
    except (OSError, ShipError):
        return ["post drop held: drop-holds"]
    lines = []
    for name, mode in sorted(children):
        if not name.endswith(".json"):
            continue
        if not stat.S_ISREG(mode):
            lines.append("post drop held: drop-holds")
            continue
        try:
            data = json.loads(post_dir.read_text(folder / name))
            if data.get("format") == 1 and data.get("reason") in DROP_HOLD_REASONS and isinstance(data.get("folder"), str):
                if data['reason'] == DROP_COUNT_REASON:
                    rounds = data.get('rounds')
                    if type(rounds) is not int or not 1 <= rounds <= DROP_BACKLOG_ROUNDS:
                        lines.append("post drop held: drop-holds")
                        continue
                    if rounds >= DROP_BACKLOG_ROUNDS:
                        lines.append(f"post drop waiting: {data['folder']}: {data['reason']}")
                    continue
                lines.append(f"post drop held: {data['folder']}: {data['reason']}")
                continue
        except FileNotFoundError:
            if post_dir.gone(folder / name):
                continue
        except (OSError, ShipError, ValueError, TypeError, AttributeError):
            pass
        lines.append("post drop held: drop-holds")
    return lines


def _remember(runtime: Path, current: set[tuple[str, str]], fresh: list[tuple[str, str]], folder: str, reason: str) -> None:
    current.add((folder, reason))
    directory = _hold_dir(runtime)
    post_dir.ensure_directory(directory)
    path = directory / f"{_hold_name(folder, reason)}.json"
    payload = json.dumps({"format": 1, "folder": folder, "reason": reason}) + "\n"
    if _read_regular(path) == payload:
        return
    durable.replace_file(path, payload)
    fresh.append((folder, reason))



def _note_drop_round(runtime: Path, current, fresh, folder: str, *, full: bool) -> None:
    """Capped consecutive budget use; no exact count beyond the listed window is inferred."""
    if not full:
        return  # the completed scan sweeps this folder's old counter/alert
    current.add((folder, DROP_COUNT_REASON))
    directory = _hold_dir(runtime)
    post_dir.ensure_directory(directory)
    path = directory / f"{_hold_name(folder, DROP_COUNT_REASON)}.json"
    rounds = 0
    try:
        old = json.loads(post_dir.read_text(path))
        if (old.get('format') == 1 and old.get('folder') == folder
                and old.get('reason') == DROP_COUNT_REASON
                and type(old.get('rounds')) is int and 1 <= old['rounds'] <= DROP_BACKLOG_ROUNDS):
            rounds = old['rounds']
    except (OSError, ShipError, ValueError, TypeError, AttributeError):
        pass
    if rounds == DROP_BACKLOG_ROUNDS:
        return
    rounds += 1
    payload = dict(format=1, folder=folder, reason=DROP_COUNT_REASON, rounds=rounds)
    durable.replace_file(path, json.dumps(payload) + '\n')
    if rounds == DROP_BACKLOG_ROUNDS:
        fresh.append((folder, DROP_COUNT_REASON))

def _sweep_holds(runtime: Path, current: set[tuple[str, str]], *, folders: frozenset[str] | None = None) -> None:
    folder = _hold_dir(runtime)
    try:
        children = post_dir.list_children(folder)
    except (OSError, ShipError):
        return
    for name, mode in children:
        if not name.endswith(".json") or not stat.S_ISREG(mode):
            continue
        try:
            data = json.loads(post_dir.read_text(folder / name))
            key = (data.get("folder"), data.get("reason"))
        except (OSError, ShipError, ValueError, TypeError, AttributeError):
            continue
        if folders is not None and key[0] not in folders:
            continue
        if key not in current:
            try:
                post_dir.remove(folder / name)
            except (OSError, ShipError):
                pass


def _same_stored(store: Store, message: Message, text: str) -> str | None:
    """'same' when the store already has these exact bytes, 'differs' when the id is taken, None when free.

    A link at the store name is 'differs': it is not read and not replaced.
    """
    path = store.dir / f"{message.id}.json"
    found = _mode(path)
    if found is None:
        return None
    if not stat.S_ISREG(found):
        return "differs"
    existing = _read_regular(path)
    if existing is None:
        return "differs"
    return "same" if existing == text else "differs"


FILING_BUSY_PASSES = 3
FILING_BUSY_REASON = 'post filing waits: filing lock busy'


def _filing_folder(runtime: Path) -> Path:
    return ship_locks.folder(runtime)


def _filing_misses(runtime: Path) -> int | None:
    try:
        data = json.loads((_filing_folder(runtime) / 'filing-wait.json').read_text())
        passes = data['passes']
        if (data.get('format') == 1 and type(passes) is int
                and 1 <= passes <= FILING_BUSY_PASSES and data.get('reason') == FILING_BUSY_REASON):
            return passes
    except FileNotFoundError:
        return 0
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return None


def filing_waits(runtime: Path) -> list[str]:
    """Disposable lock-contention evidence, visible independently of the service."""
    misses = _filing_misses(runtime)
    if misses is None:
        return ['post filing wait unknown']
    return [FILING_BUSY_REASON] if misses >= FILING_BUSY_PASSES else []


def _note_filing_busy(runtime: Path) -> None:
    misses = _filing_misses(runtime)
    if misses == FILING_BUSY_PASSES:
        return  # bounded evidence: a held lock does not cause a write on every pass
    data = dict(format=1, passes=min((misses if misses is not None else FILING_BUSY_PASSES) + 1,
                                     FILING_BUSY_PASSES), reason=FILING_BUSY_REASON)
    durable.replace_file(_filing_folder(runtime) / 'filing-wait.json', json.dumps(data) + '\n')


def file_drops(store: Store, ship=None, *, seats: frozenset[str] | None = None,
               service_absent: bool = False, filing: FilingSlice | None = None) -> list[tuple[str, str]]:
    """One filing door, serialized for the service and a check's scoped fallback.

    The fallback checks service ownership under the filing lock. A service starting
    afterward uses this same lock, so it cannot race that pass. Busy filers yield
    to the next ordinary pass; neither caller waits while holding another lock.
    """
    runtime = store.dir.parent.parent
    with ExitStack() as locks:
        try:
            descriptor = locks.enter_context(ship_locks.held(ship_locks.path(runtime, 'filing.lock'), 'post filing', wait=0))
        except ship_locks.Busy:
            _note_filing_busy(runtime)
            return []
        (_filing_folder(runtime) / 'filing-wait.json').unlink(missing_ok=True)
        if service_absent:
            from . import servicectl
            if servicectl.alive(runtime):
                return []
        revision = os.pread(descriptor, 8, 0)
        if filing is not None:
            return filing.advance(store, ship, seats=seats, revision=revision)
        # A whole host pass can change holds/drops while the service scan is paused.
        # Its next advance starts afresh rather than sweeping from an older snapshot.
        os.pwrite(descriptor, (int.from_bytes(revision, 'big') + 1).to_bytes(8, 'big'), 0)
        os.ftruncate(descriptor, 8)
        return _file_drops(store, ship, seats=seats)


class FilingSlice:
    """A resumable scan; its caller holds the filing lock only during advance().

    The budget is cooperative: directory operations and one message/group intent
    finish before yielding. No descriptor or admission lock spans two advances.
    Immutable publication and the ordinary checks remain in the same scan.
    """
    def __init__(self, *, seconds: float = 0.02, operations: int = 128):
        self.seconds, self.operations = seconds, operations
        self.steps = None
        self.scope = None
        self.fresh: list[tuple[str, str]] = []

    def advance(self, store: Store, ship=None, *, seats=None, revision=b'') -> list[tuple[str, str]]:
        scope = (store.dir, seats, ship, revision)
        if self.steps is not None and self.scope != scope:
            self.steps.close()
            self.steps = None
        if self.steps is None:
            self.scope = scope
            self.steps = _drop_steps(store, ship, seats=seats, fresh=self.fresh)
        deadline = time.monotonic() + self.seconds
        try:
            for _ in range(self.operations):
                next(self.steps)
                if time.monotonic() >= deadline:
                    break
        except StopIteration:
            self.steps = None
        except BaseException:
            self.steps = None
            raise
        result = list(self.fresh)
        self.fresh.clear()
        return result


def _file_drops(store: Store, ship=None, *, seats: frozenset[str] | None = None) -> list[tuple[str, str]]:
    fresh: list[tuple[str, str]] = []
    for _ in _drop_steps(store, ship, seats=seats, fresh=fresh):
        pass
    return fresh


def _drop_steps(store: Store, ship=None, *, seats: frozenset[str] | None = None, fresh):
    """File each seat's drops into the store. A refusal is held, named, and never deleted.

    Append newly recorded holds for the caller to log once, and yield only outside admission.
    Group intents are published first, when the ship is known; a root that still has an
    intent beside it is left for that pass. Every name is walked by the post folder's
    no-follow helpers: a link in a drop is held as not a regular file, never followed.
    """
    runtime = store.dir.parent.parent
    root = store.dir.parent / "drop"
    current: set[tuple[str, str]] = set()
    folders = None if seats is None else frozenset({"drop", *(f"drop/{name}" for name in seats)})

    def hold(folder: str, reason: str) -> None:
        _remember(runtime, current, fresh, folder, reason)

    try:
        found = _mode(root)
    except (OSError, ShipError):
        found = None
        hold("drop", "drop unreadable")
    if found is None:
        _sweep_holds(runtime, current, folders=folders)
        return
    if not stat.S_ISDIR(found):
        hold("drop", "drop folder is not a directory")
        _sweep_holds(runtime, current, folders=folders)
        return
    try:
        children = sorted(post_dir.list_children(root))
    except (OSError, ShipError):
        hold("drop", "drop unreadable")
        return
    windows = []
    for name, mode in children:
        yield  # a folder listing is one cooperative operation
        if seats is not None and name not in seats:
            continue
        folder = f"drop/{name}"
        if not stat.S_ISDIR(mode):
            hold(folder, "drop folder is not a directory")
            continue
        try:
            files = sorted(post_dir.list_children(root / name, limit=DROP_MAX_FILES))
        except FileNotFoundError:
            if not post_dir.gone(root / name):
                hold(folder, "drop unreadable")
            continue
        except (OSError, ShipError):
            hold(folder, "drop unreadable")
            continue
        windows.append((name, files))
    # Group and ordinary passes share exactly these windows; no second folder listing. The round's ids avoid
    # the shorts queued in them and the shorts this round prepares.
    if ship is not None and seats is None:
        from . import group_post
        taken = set().union(*(queued_shorts(files) for _, files in windows))
        for name, files in windows:
            for held in group_post.staged_folder_steps(ship, store, name, files, taken=taken):
                if held is not None:
                    hold(*held)
                yield
    for name, files in windows:
        folder = f"drop/{name}"
        for file, kind in files:
            yield  # finish a whole filing operation before the next slice may yield
            if file.endswith(".group.json"):
                continue
            path = root / name / file
            if not stat.S_ISREG(kind):
                hold(folder, "not a regular file")
                continue
            try:
                intent = _mode(path.with_name(f"{path.stem}.group.json"))
            except (OSError, ShipError):
                hold(folder, "drop unreadable")
                continue
            if intent is not None:
                continue  # the group pass owns this root, even beyond the bounded window
            try:
                text = _read_regular(path, limit=DROP_MAX_BYTES)
            except post_dir.ReadLimit:
                hold(folder, DROP_SIZE_REASON)
                continue
            except UnicodeDecodeError:
                hold(folder, "not a message")
                continue
            if text is None:
                if post_dir.gone(path):
                    continue
                hold(folder, "not a regular file")
                continue
            try:
                message = Message.from_json(text)
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                hold(folder, "not a message")
                continue
            if message.id != path.stem or "/" in message.id:
                hold(folder, "not a message")
                continue
            named = id_sender(message.id)
            if named is None or message.id[:3] != message.kind[:3]:
                hold(folder, "not a message")  # the id does not name its sender in today's form
                continue
            if named != message.sender or message.sender not in _drop_senders(runtime, name):
                hold(folder, "from names another seat")  # id, from and folder are one seat, or the drop is held
                continue
            try:
                stored = _same_stored(store, message, text)
            except (OSError, ShipError):
                hold(folder, "drop unreadable")
                continue
            if stored == "same":
                _remove_drop(path)
                continue
            if stored == "differs":
                hold(folder, "id already stored")  # law: post-is-files
                continue
            try:
                store.publish_text(message, text)
            except (OSError, ShipError, ValueError, TypeError):
                hold(folder, "not a message")
                continue
            _remove_drop(path)
        full = len(files) == DROP_MAX_FILES
        _note_drop_round(runtime, current, fresh, folder, full=full)
        if full and not any(post_dir.gone(root / name / file) for file, _ in files):
            hold(folder, DROP_STALLED_REASON)
    _sweep_holds(runtime, current, folders=folders)


def _drop_senders(runtime: Path, seat: str) -> set[str]:
    """The senders a seat's folder may file: the seat, and the seat qualified by this ship to another ship."""
    from .fleet_post import owner
    here = owner(runtime)
    return {seat} if here is None else {seat, f"{seat}@{here}"}


def _remove_drop(path: Path) -> None:
    """Remove a filed drop; a name that is no longer a regular file is left for the next pass to hold."""
    if _regular(path):
        try:
            post_dir.remove(path)
        except (OSError, ShipError):
            pass


# -- the typed form ----------------------------------------------------------------------


def briefed_header(seat: str, role: str, open_messages: list[Message]) -> str:
    orders = sum(m.kind == "order" for m in open_messages)
    questions = len(open_messages) - orders
    what = ", ".join(filter(None, [
        f"{orders} order{'s' if orders != 1 else ''} open" if orders else "",
        f"{questions} question{'s' if questions != 1 else ''} open" if questions else "",
    ])) or "nothing open"
    return f"qd > briefed as {seat}, {role} · {what}"


# -- the inbox ---------------------------------------------------------------------------


INBOX_TAIL_AGE = timedelta(hours=24)
INBOX_TAIL_MESSAGES = 200
INBOX_TAIL_BYTES = 256 * 1024


def inbox_reading(store: Store, seat: str, *, recent: bool = False) -> dict:
    messages = [_shown(m, withheld) for withheld in [_withheld(store, seat)] for m in store.for_seat(seat)]
    data = {"seat": seat, "open": [m for m in messages if store.is_open(m)],
            "received": list(reversed([m for m in messages if not store.is_open(m)])),
            "closed_by": {m.id: closed.id for m in messages if m.kind == "order"
                          and (closed := store.closing_report(m))}}
    if recent:
        received = data["received"]
        cutoff = now() - INBOX_TAIL_AGE
        # Include headings and the closing line in the received tail's byte budget.
        size = len((f"## Received ({INBOX_TAIL_MESSAGES})\n\n" + _older_line(len(received)) + "\n").encode("utf-8"))
        tail = []
        for message in received:
            entry_size = len(("\n".join(_entry(message, data["closed_by"].get(message.id))) + "\n").encode("utf-8"))
            if (message.at < cutoff or len(tail) >= INBOX_TAIL_MESSAGES
                    or size + entry_size > INBOX_TAIL_BYTES):
                break
            tail.append(message)
            size += entry_size
        data["received"] = tail
        data["older_received"] = len(received) - len(tail)
    return data


def _older_line(count: int) -> str:
    return (f"Older received messages: {count}. Read the whole history with `qd inbox --all`. "
            "`qd thread <id>` prints a thread whole, as far as the seat sent or received it.")


def _withheld(store: Store, seat: str) -> frozenset[str] | None:
    """What this seat's provider refused, never shown it again; None when that cannot be read."""
    from . import refusal
    try:
        return refusal.excluded(store.dir.parent.parent, seat)
    except refusal.JournalError:
        return None


def _shown(message: Message, withheld: frozenset[str] | None) -> Message:
    """The refused message as a notice without its subject or body (law: context-is-cache).

    An unreadable refusal journal withholds every body: the store keeps the text, nothing is lost.
    """
    from .refusal import WITHHELD
    if withheld is None:
        return replace(message, subject=None, body='withheld: refusal journal unreadable; inspect runtime/seats')
    return replace(message, subject=None, body=WITHHELD) if message.id in withheld else message


def inbox_text(store: Store, seat: str) -> str:
    return render_inbox_reading(inbox_reading(store, seat, recent=True))


def render_inbox_reading(data: dict) -> str:
    lines = [f"# Inbox: {data['seat']}", ""]
    for title, key in (("Open", "open"), ("Received", "received")):
        lines += [f"## {title} ({len(data[key])})", ""]
        for message in data[key]:
            lines += _entry(message, data["closed_by"].get(message.id))
    if "older_received" in data:
        lines += [_older_line(data["older_received"])]
    return "\n".join(lines).rstrip() + "\n"


def _entry(message: Message, closed: str | None) -> list[str]:
    head = f"### {message.kind} {message.short} from {message.sender} · {message.at.strftime('%m-%d %H:%M')}"
    if message.subject:
        head += f" · {message.subject}"
    details = []
    if message.re:
        details.append(f"re {message.re}")
    if message.due:
        details.append(f"due {message.due}")
    if message.kind == "order":
        details.append(f"closed by {short_of(closed)}" if closed else f"open · reply: qd post report --re {message.short}")
    return [head, *([" · ".join(details)] if details else []), "", message.body.rstrip(), ""]


def render_inbox(runtime: Path, store: Store, seat: str) -> Path:
    folder = runtime / "post" / "inbox"
    post_dir.ensure_directory(folder)
    path = folder / f"{seat}.md"
    text = inbox_text(store, seat)
    try:
        current = post_dir.read_text(path)
    except FileNotFoundError:
        current = None
    if current == text:
        return path
    durable.replace_file(path, text)
    return path


# -- delivery attempts -----------------------------------------------------------------------

ACCEPTED = ("delivered", "queued", "launch", "watch")
# Typed and taken, but the turn ended in a recorded error: unacted proved no model output, so the
# next start carries it; uncertain could not tell, so it waits for a hand (law: one-door).
UNSETTLED = ("unacted", "uncertain")


@dataclass(frozen=True)
class Attempt:
    """One delivery attempt, written before the door types; its result written after."""

    message: str
    seat: str
    at: str
    pane: str | None = None
    pid: int | None = None
    launched_at: str | None = None
    result: str | None = None
    detail: str = ""
    path: Path | None = None

    @property
    def accepted(self) -> bool:
        return self.result in ACCEPTED

    @property
    def when(self) -> datetime:
        return datetime.fromisoformat(self.at)

    @property
    def receipt_since(self) -> datetime:
        """Old second-only attempts cannot distinguish inputs within that same second."""
        return self.when if "." in self.at else self.when + timedelta(seconds=1)


def attempt_path(runtime: Path, name: str) -> Path:
    """Resolve an original attempt filename, including a retired cited receipt."""
    if Path(name).name != name or name in ('.', '..'):
        raise ShipError('invalid attempt filename')
    from .post_retirement import resolve
    return resolve(runtime / 'post' / 'attempts' / name, runtime)


class Attempts:
    def __init__(self, runtime: Path):
        self.dir = runtime / "post" / "attempts"
        self._cache = {}
        self._retired_cache = {}

    @contextmanager
    def _locked(self, wait: float | None = None):
        from . import ship_locks
        post_dir.ensure_directory(self.dir)  # the attempts' own folder, which the lock once made beside it
        with ship_locks.held(ship_locks.path(self.dir.parent.parent, 'attempts.lock'), 'delivery attempts',
                             wait=ship_locks.WAIT if wait is None else wait):
            yield

    def open(self, message: Message, seat: str, pane: str | None, pid: int | None, launched_at: str | None, *,
             wait: float | None = None) -> Attempt:
        with self._locked(wait):
            at = now()
            prefix = f'{message.id}-'
            history = [path for path in post_dir.json_paths(self.dir) if path.name.startswith(prefix)]
            history += [a.path for name, a in self._retired().items()
                        if name.startswith(prefix) and name.endswith('.json')]
            number = max((int(p.stem.rsplit('-', 1)[1]) for p in history), default=0) + 1
            attempt = Attempt(message.id, seat, at.isoformat(timespec="microseconds"), pane, pid, launched_at,
                              path=self.dir / f"{message.id}-{number}.json")
            data = {k: v for k, v in asdict(attempt).items() if k != "path"}
            # law: one-door -- retain the initial attempt before typing, never type twice.
            durable._replace_law_record(attempt.path, json.dumps(data, indent=2) + "\n")
            from . import shutdown
            shutdown.opened(self.dir.parent.parent, attempt)
        return attempt

    def close(self, attempt: Attempt, result: str, detail: str = "", *, wait: float | None = None) -> Attempt:
        with self._locked(wait):
            source = attempt_path(self.dir.parent.parent, attempt.path.name)
            current = Attempt(**json.loads(post_dir.read_text(source)), path=source)
            if source.parent != self.dir:
                return current  # retired finished evidence is immutable
            if current.result in ("abandoned", "not-in-closed-record", *UNSETTLED):
                return current  # terminal even if an old door/watch finishes after cleanup or a receipt
            done = replace(attempt, result=result, detail=detail)
            if done == current:
                return current  # current disk data only; no durability/repair assertion
            self._write(done)
            return done

    def judge(self, attempt: Attempt, result: str, detail: str = "") -> bool:
        """Judge only an unfinished attempt; an intervening door/watch verdict stands."""
        with self._locked():
            source = attempt_path(self.dir.parent.parent, attempt.path.name)
            current = Attempt(**json.loads(post_dir.read_text(source)), path=source)
            if source.parent != self.dir:
                return False
            if current.result is not None:
                return False
            self._write(replace(current, result=result, detail=detail))
            return True

    def settle_missing_identity(self, expected: list[Attempt], detail: str) -> list[Attempt] | None:
        """Retire only matching legacy uncertainty; another verdict is never overwritten."""
        return self.settle_unresolved(expected, detail)

    def settle_unresolved(self, expected: list[Attempt], detail: str) -> list[Attempt] | None:
        """Abandon matching unresolved attempts, preserving every terminal verdict."""
        unresolved = {None, 'pending', 'blind', 'typed', 'queued', 'withheld', 'cancelled'}
        terminal = {'delivered', 'watch', 'launch', 'abandoned', 'not-in-closed-record', *UNSETTLED}
        with self._locked():
            updated, writes = [], []
            for original in expected:
                if original.path.parent != self.dir:
                    return None
                current = Attempt(**json.loads(post_dir.read_text(original.path)), path=original.path)
                if (current.message, current.seat, current.at, current.pane, current.pid, current.launched_at) != (
                        original.message, original.seat, original.at, original.pane, original.pid, original.launched_at):
                    return None
                if current.result in terminal:
                    updated.append(current)
                elif current == original and current.result in unresolved:
                    target = replace(current, result='abandoned', detail=detail)
                    updated.append(target)
                    writes.append(target)
                else:
                    return None
            for current in writes:
                self._write(current)
        return updated

    def settle_closed_receipt(self, expected: list[Attempt], intent=None) -> str:
        """Complete only a saved positive closed-record decision, never generic close.

        The service holds seat admission first. All snapshots must still match,
        or already be this decision's delivered write after an interrupted pass.
        The intent callback publishes journal ownership before any attempt changes.
        """
        detail = 'exact payload found in saved closed record after framework refresh'
        eligible = {None, 'pending', 'blind', 'queued', 'not-in-closed-record',
                    'launch', 'uncertain', 'unsettled'}
        if not expected or any(a.result not in eligible or a.path.parent != self.dir for a in expected):
            return 'superseded'
        with self._locked():
            updates = []
            for original in expected:
                current = Attempt(**json.loads(post_dir.read_text(original.path)), path=original.path)
                target = replace(original, result='delivered', detail=detail)
                if current != original and current != target:
                    if (current.message, current.seat, current.at, current.pane, current.pid, current.launched_at) != (
                            original.message, original.seat, original.at, original.pane, original.pid, original.launched_at):
                        return 'superseded'
                    if current.result in {'delivered', 'withheld', 'cancelled', 'abandoned',
                                          'unacted', 'carried', 'launch', 'uncertain', 'unsettled'}:
                        return 'superseded'
                    return 'retry'  # still-unresolved progress made this snapshot stale
                updates.append((current, target))
            owned = {old.path for old in expected}
            for other in self.all():
                if other.path not in owned and other.result in {'delivered', 'watch', 'abandoned', 'unacted'} and any(
                        other.message == old.message and other.seat == old.seat
                        and other.when >= old.when
                        for old in expected):
                    return 'superseded'  # another attempt already settled the message
                # A distinct "launch; not confirmed read" stays latest and unchanged;
                # it does not forbid a true receipt for an older carried submission.
            if intent is not None:
                intent()
            for current, target in updates:
                if current != target:
                    self._write(target)
        return 'settled'

    def abandon(self, seat: str, detail: str) -> list[Attempt]:
        """Retire uncertainty, never a confirmed receipt; keep every historical file."""
        changed = []
        with self._locked():
            for attempt in self.all():
                if attempt.path.parent != self.dir or attempt.seat != seat or attempt.result in ("delivered", "launch", "watch", "abandoned", "not-in-closed-record"):
                    continue
                done = replace(attempt, result="abandoned",
                               detail=f"{detail}; previous result: {attempt.result or 'unfinished'}")
                self._write(done)
                changed.append(done)
        return changed

    def _write(self, attempt: Attempt) -> None:
        data = {k: v for k, v in asdict(attempt).items() if k != "path"}
        durable.replace_file(attempt.path, json.dumps(data, indent=2) + "\n")

    def _retired(self, progress=None) -> dict[str, Attempt]:
        """Immutable batch metadata: stat batches, never every retained receipt."""
        folder = self.dir.parent / 'retired' / 'index'
        found, cache = {}, {}
        for path in post_dir.json_paths(folder):
            if progress is not None:
                progress()
            try:
                info = path.lstat()
            except FileNotFoundError:
                self._retired_cache.pop(path.name, None)
                continue
            stamp = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            cached = self._retired_cache.get(path.name)
            if cached is None or cached[0] != stamp:
                try:
                    data = json.loads(post_dir.read_text(path))
                except FileNotFoundError:
                    if not post_dir.gone(path):
                        raise
                    self._retired_cache.pop(path.name, None)
                    continue
                if (not isinstance(data, dict) or data.get('format') != 1
                        or not isinstance(data.get('attempts'), dict)):
                    raise ShipError('retired attempt index unreadable; delivery held')
                rows = {}
                for name, fields in data['attempts'].items():
                    if Path(name).name != name or not name.endswith('.json'):
                        raise ShipError('retired attempt index filename invalid; delivery held')
                    rows[name] = Attempt(**fields, path=self.dir.parent / 'retired' / 'attempts' / name)
                cached = stamp, rows
            cache[path.name] = cached
            for name, attempt in cached[1].items():
                if name in found and found[name] != attempt:
                    raise ShipError('retired attempt indices disagree; delivery held')
                found[name] = attempt
        self._retired_cache = cache
        return found

    def all(self, *, progress=None) -> list[Attempt]:
        reading = _READING.get()
        if reading is not None:
            key = self.dir.absolute()
            reader = reading.attempts.setdefault(key, self)
            return list(reading_value('attempts', key, lambda: reader._all_current(progress=progress)))
        return self._all_current(progress=progress)

    def _all_current(self, *, progress=None) -> list[Attempt]:
        # Attempts are mutable, including writes from another process. Directory
        # times alone miss same-path edits; stat every entry, parse only changes.
        retired = self._retired(progress)
        try:
            children = post_dir.list_children(self.dir, with_stat=True)
        except FileNotFoundError:
            self._cache = {}
            return sorted(retired.values(), key=lambda a: (a.at, a.path.name))
        current = {}
        for index, (name, info) in enumerate(children):
            if progress is not None and index % 200 == 0:
                progress()
            if not name.endswith('.json'):
                continue
            if stat.S_ISLNK(info.st_mode):
                if post_dir.gone(self.dir / name):
                    self._cache.pop(name, None)
                    continue
                post_dir._link_held(self.dir / name)
            path = self.dir / name
            stamp = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode)
            cached = self._cache.get(name)
            if cached is None or cached[0] != stamp:
                try:
                    cached = stamp, Attempt(**json.loads(post_dir.read_text(path)), path=path)
                except FileNotFoundError:
                    if not post_dir.gone(path):
                        raise
                    self._cache.pop(name, None)
                    continue
                # Retain parsed work even if a retirement slice ends mid-scan.
                # Re-list and re-stat on every call; this cache is no snapshot.
                self._cache[name] = cached
            current[name] = cached
        # A failed scan/read must propagate, never certify a partial snapshot.
        self._cache = current
        retired.update((name, value[1]) for name, value in current.items())
        return sorted(retired.values(), key=lambda a: (a.at, a.path.name))

    def for_message(self, message_id: str) -> list[Attempt]:
        return [a for a in self.all() if a.message == message_id]

    def latest(self, seat: str | None = None, *, attempts=None) -> dict[str, str | None]:
        """Each message's latest attempt result, at one seat or at any: the latest attempt is the one that speaks."""
        results: dict[str, str | None] = {}
        for attempt in self.all() if attempts is None else attempts:
            if seat is None or attempt.seat == seat:
                results[attempt.message] = attempt.result
        return results

    def last_results(self, seat: str) -> dict[str, str | None]:
        return self.latest(seat)

    def accepted_ids(self) -> set[str]:
        return {m for m, result in self.latest().items() if result in ACCEPTED}

    def blind_ids(self) -> set[str]:
        """Messages whose latest attempt is blind: typed, unconfirmed, waiting for a hand (``qd post resend``)."""
        return {m for m, result in self.latest().items() if result == "blind"}

    def queued_ids(self) -> set[str]:
        """Messages whose latest attempt says queued: taken by the program, not yet read."""
        return {m for m, result in self.latest().items() if result == "queued"}

    def unfinished(self) -> list[Attempt]:
        """Attempts with no result: the service died between typing and confirming."""
        return [a for a in self.all() if a.result is None]

    def undelivered(self, store: Store, seat: str, *, results=None, for_launch=False) -> list[Message]:
        """What is addressed to the seat and never reached it, oldest first.

        A blind message waits for a hand, one with an attempt still open is being judged, so
        neither is sent again by the service (law: one-door); an acknowledgment claims and
        never wakes, so it is never delivered at all (law: open-until-closed).
        """
        results = self.latest() if results is None else results
        held = (*ACCEPTED, "blind", None, "pending", "abandoned", "uncertain", "not-in-closed-record")

        def skipped(identifier: str) -> bool:
            # Only a message with an attempt is judged by its latest result; one never attempted is not held.
            if identifier not in results:
                return False
            result = results[identifier]
            return result in held or result == "unacted" and not for_launch  # law: one-door

        reserved = set()
        if not for_launch:
            from .compaction import withheld
            reserved, transitioning = withheld(store.dir.parent.parent, seat)
            if transitioning:
                return []
        # Only the seat's own messages are judged, never every message's latest result.
        return [m for m in store.for_seat(seat)
                if m.id not in reserved and not skipped(m.id) and m.kind != "ack"]  # law: open-until-closed


WAIT_RESCAN_SECONDS = 60.0  # a waiting watch reads every attempt in full at least this often


class AttemptsLook:
    """A waiting watch's reading of each message's latest attempt result, read in full only when it may have changed.

    Every attempt the framework writes is renamed into ``post/attempts`` (``post_dir.replace_bytes``), and
    retirement publishes its batch into ``post/retired/index`` before it moves an attempt out of that folder, so
    any change moves one of the two folders' times. A matching stamp of both is trusted only under the store's
    racy-timestamp rule (docs/design/post.md): the reading in hand began more than a tick after both folders'
    times. Anything else, and at least every WAIT_RESCAN_SECONDS, reads every attempt again as ``Attempts.all``
    does, so an edit made in place is still seen. The caller looks under the seat's admission, as before: a look
    skipped can only delay a delivery, and what is delivered is decided from a reading equal to a full one.
    """

    def __init__(self, attempts: 'Attempts', *, rescan: float = WAIT_RESCAN_SECONDS):
        self.attempts, self.rescan = attempts, rescan
        self._results: dict[str, str | None] | None = None
        self._stamp = None
        self._certified_at: int | None = None
        self._read_at = 0.0
        self.full_reads = 0

    def _folders_stamp(self):
        stamp = []
        for folder in (self.attempts.dir, self.attempts.dir.parent / 'retired' / 'index'):
            try:
                info = os.lstat(folder)
            except FileNotFoundError:
                stamp.append(None)
                continue
            if not stat.S_ISDIR(info.st_mode):
                return None  # a link or anything else is never trusted; the full read judges it
            stamp.append((info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns))
        return tuple(stamp)

    def latest(self) -> dict[str, str | None]:
        certified_at, self._certified_at = self._certified_at, None  # a failed read certifies nothing
        started = time.time_ns()
        stamp = self._folders_stamp()
        checked = time.time_ns()
        if (self._results is not None and stamp is not None and stamp == self._stamp and certified_at is not None
                and certified_at <= started <= checked and time.monotonic() - self._read_at < self.rescan):
            self._certified_at = certified_at
            return self._results
        self._results, self._stamp = None, None
        read_at = time.monotonic()
        results = self.attempts.latest()
        self.full_reads += 1
        self._results, self._stamp, self._read_at = results, stamp, read_at
        times = [max(part[2], part[3]) for part in stamp or () if part is not None]
        if stamp is not None and (not times or max(times) + POST_TIMESTAMP_TICK_NS < started) and started <= time.time_ns():
            self._certified_at = started
        return results


# -- the watch: how a seat in a human session is reached -----------------------------------------


def watch_file(runtime: Path, seat: str) -> Path:
    return runtime / "seats" / seat / "watch"


@dataclass(frozen=True)
class WatchReading:
    armed: bool | None
    generation: str | None = None
    reason: str = ""


def watch_reading(runtime: Path, seat: str) -> WatchReading:
    """Old PID/time watch files remain valid; unreadable evidence is not absence."""
    from .turn_failure import metadata

    path = watch_file(runtime, seat)
    try:
        generation = metadata(path.parent / 'watch-generation').strip()
        if len(generation) != 32 or any(c not in '0123456789abcdef' for c in generation):
            raise ValueError('invalid watch generation')
    except FileNotFoundError:
        generation = None  # old watches have no durable arm-episode marker
    except (OSError, ValueError, UnicodeError):
        return WatchReading(None, reason='inbox watch generation unreadable; no waking input')
    try:
        retired = json.loads(metadata(path.parent / 'watch-retirement.json'))
        if (retired.get('format') != 1 or not isinstance(retired.get('generation'), str)
                or len(retired['generation']) != 32
                or any(c not in '0123456789abcdef' for c in retired['generation'])
                or retired.get('reason') != 'inbox watch retirement unconfirmed; inspect watch metadata'):
            raise ValueError('invalid watch retirement evidence')
        if generation is None or generation == retired['generation']:
            return WatchReading(None, generation, retired['reason'])
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError, AttributeError):
        return WatchReading(None, generation, 'inbox watch retirement unconfirmed; evidence unreadable')
    try:
        fields = metadata(path).split()
        if len(fields) != 2 or int(fields[0]) <= 0 or datetime.fromisoformat(fields[1]).tzinfo is None:
            raise ValueError('invalid watch evidence')
        pid = int(fields[0])
    except FileNotFoundError:
        return WatchReading(False, generation)
    except (OSError, ValueError, UnicodeError):
        return WatchReading(None, generation, 'inbox watch evidence unreadable; no waking input')
    from . import watch_broker
    held = watch_broker.lease_held(runtime, seat, generation)
    if held is not None:
        # A jailed watch's process id is its jail's own; its lease in its seat folder says whether it lives.
        return WatchReading(held, generation)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return WatchReading(False, generation)
    except OSError:
        return WatchReading(None, generation, 'inbox watch process unknown; no waking input')
    return WatchReading(True, generation)


def watch_armed(runtime: Path, seat: str) -> bool:
    return watch_reading(runtime, seat).armed is True


def watch_retirement_warning(path: Path, generation: str) -> None:
    """Retain an operational warning without replacing a wait result or primary error."""
    reason = 'inbox watch retirement unconfirmed; inspect watch metadata'
    try:
        durable.replace_file(path.parent / 'watch-retirement.json',
                             json.dumps(dict(format=1, generation=generation, reason=reason)) + '\n')
    except Exception:
        reason += '; operational warning could not be saved'
    try:
        import sys
        print(reason, file=sys.stderr)
    except Exception:
        pass  # preserving the primary return/error takes precedence if stderr also fails


def wait_batch(runtime: Path, seat: str, interval: float = 1.0, stop_after: float | None = None, *, validate=None) -> tuple[list[Message], list[Attempt]] | None:
    """Block until something new is addressed to ``seat``; return it in full, with its open attempts.

    The caller prints the text and then closes the attempts with ``printed``, so a wait
    that dies between the two leaves them open to be judged, never silently taken. The
    wait registers itself while it runs, so the service and the status line know a watch
    is armed. Nothing is typed into the seat's pane: this is the Office's one path.
    """
    from . import watch_broker
    if watch_broker.needed(runtime):
        # Inside a jail the post folder is read-only: the service opens and closes this watch's attempts.
        return watch_broker.wait(runtime, seat, interval, stop_after, validate)
    store, attempts = Store(runtime), Attempts(runtime)
    look = AttemptsLook(attempts)
    if validate is not None:
        validate()
    from . import inbox_watch
    registration = None

    def retain_registration(value):
        nonlocal registration
        registration = value

    try:
        # The helper retains this token before restoring SIGINT or returning to this assignment.
        registration = inbox_watch.register(runtime, seat, validate=validate,
                                             on_registered=retain_registration)
        deadline = None if stop_after is None else datetime.now().timestamp() + stop_after
        while True:
            with admission(runtime, seat):
                if validate is not None:
                    validate()
                # Read under the admission, as before; the attempts are read in full only when they may have changed.
                # One reading pass per check: imports and forwarded are listed once, after the store is loaded,
                # not walked three times for every message the seat ever received. It ends before anything opens.
                with reading_pass(store=store, attempts=attempts):
                    waiting = attempts.undelivered(store, seat, results=look.latest())
                if waiting:
                    opened = [attempts.open(message, seat, None, None, None) for message in waiting]
                    return waiting, opened
            if deadline is not None and datetime.now().timestamp() > deadline:
                return None
            time.sleep(interval)
    finally:
        if registration is not None:
            try:
                inbox_watch.retire(runtime, seat, registration)
            except Exception:
                # Retain the waking journal warning without replacing the primary outcome.
                watch_retirement_warning(watch_file(runtime, seat), registration['generation'])


def wait(runtime: Path, seat: str, interval: float = 1.0, stop_after: float | None = None) -> tuple[str, list[Attempt]] | None:
    batch = wait_batch(runtime, seat, interval, stop_after)
    return (typed(batch[0]), batch[1]) if batch is not None else None


def printed(runtime: Path, opened: list[Attempt]) -> None:
    """Close a watch's attempts once their text has reached the seat.

    A jailed watch cannot write them: its receipts go in its own seat folder, and the service closes them.
    """
    from . import watch_broker
    if watch_broker.needed(runtime):
        watch_broker.receipt(runtime, opened)
        return
    attempts = Attempts(runtime)
    for attempt in opened:
        attempts.close(attempt, "watch", "printed by qd inbox --wait")
