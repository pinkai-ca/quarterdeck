"""Post: orders, reports, questions, answers and acknowledgments as immutable files.

Each message is one JSON file in ``post/store/``, the canonical copy (law: post-is-files).
Only a report from the order's addressee closes an order; an ack claims it for the Ledger
and closes nothing (law: open-until-closed). ``post/inbox/<seat>.md`` is the rendered
view: open obligations first, oldest first, then what was already delivered.
"""

from __future__ import annotations

import json
import fcntl
import hashlib
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
import os
import secrets
import stat
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Collection

from . import durable, post_dir
from .ship import ShipError

KINDS = ("order", "report", "question", "answer", "ack")
SPEEDS = ("now", "interrupt")
SHIP = "ship"  # the sender of the ship's own notices, and an inbox a seat may answer to
REPLY_VERB = {"order": "report", "question": "answer"}
POST_TIMESTAMP_TICK_NS = 2_000_000_000  # conservative filesystem tick; see docs/design/post.md
_FIXED_REPORT = ContextVar('quarterdeck_fixed_report', default=None)
_READING = ContextVar('quarterdeck_post_reading', default=None)


@contextmanager
def reading_pass():
    """One immutable message/reply snapshot shared by a read-only presentation."""
    if _READING.get() is not None:
        yield
        return
    token = _READING.set({})
    try:
        yield
    finally:
        _READING.reset(token)


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


def short_of(message_id: str) -> str:
    """``ord-7f3a`` for ``ord-20261106-2101-desk-7f3a``: the kind and the hash."""
    return f"{message_id[:3]}-{message_id.rsplit('-', 1)[1]}"


@dataclass(frozen=True)
class Message:
    id: str
    kind: str
    sender: str
    to: str
    at: datetime
    body: str
    re: str | None = None
    subject: str | None = None
    due: str | None = None
    speed: str | None = None

    @property
    def short(self) -> str:
        """``ord-7f3a``: the kind and the hash, enough to name a message in a pane."""
        return short_of(self.id)

    @property
    def obligation(self) -> bool:
        return self.kind in REPLY_VERB

    def to_json(self) -> str:
        data = asdict(self)
        data["from"] = data.pop("sender")
        data["at"] = self.at.isoformat()
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"

    @classmethod
    def from_json(cls, text: str) -> "Message":
        data = json.loads(text)
        data["sender"] = data.pop("from")
        data["at"] = datetime.fromisoformat(data["at"])
        return cls(**data)


@contextmanager
def admission(runtime: Path, seat: str):
    """Serialize publication/transition claims, never program startup; locks are not reentrant."""
    folder = runtime / 'post' / 'admission'
    name = hashlib.sha256(seat.encode()).hexdigest()
    path = folder / (name + '.lock')
    descriptor = post_dir.open_lock(path, mode=0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        opened, current = os.fstat(descriptor), path.lstat()
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise ShipError('post admission lock identity changed; retained')
        yield
    finally:
        os.close(descriptor)


class Store:
    """Every message of a ship, one immutable file each."""

    def __init__(self, runtime: Path):
        self.dir = runtime / "post" / "store"
        self._cache: tuple[tuple[int, ...], list[Message], dict[str, list[Message]]] | None = None
        self._cache_at: int | None = None

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
        return message

    def prepare(self, kind: str, sender: str, to: str, body: str, **fields) -> Message:
        """Construct an immutable intent before publication, without changing its format."""
        if kind not in KINDS:
            raise ShipError(f"no message kind {kind!r}; the kinds are {', '.join(KINDS)}")
        if fields.get("speed") not in (None, *SPEEDS):
            raise ShipError(f"no speed {fields['speed']!r}; --now or --interrupt")
        if fields.get("re"):
            fields["re"] = self.get(fields["re"]).id
        at = now()
        message = Message(self._new_id(kind, sender, at), kind, sender, to, at, body, **fields)
        return message

    def _new_id(self, kind: str, sender: str, at: datetime) -> str:
        taken = {m.short for m in self.all()}
        while True:
            candidate = f"{kind[:3]}-{at.strftime('%Y%m%d-%H%M')}-{sender}-{secrets.token_hex(2)}"
            if short_of(candidate) not in taken:
                return candidate

    def write(self, message: Message) -> Path:
        runtime = self.dir.parent.parent
        names = {message.to}
        if message.kind == 'report' and message.re:
            # A closing report is by the order's addressee, regardless of report recipient.
            names.add(message.sender)
        with ExitStack() as held:
            for name in sorted(names):
                held.enter_context(admission(runtime, name))
            from . import compaction
            compaction.protect_closure(runtime, message)
            compaction.admitting(runtime, message)
            return self._write(message)

    def _write(self, message: Message) -> Path:
        """Publish while the caller holds admission; fixed-intent recovery verifies first."""
        post_dir.ensure_directory(self.dir)
        path = self.dir / f"{message.id}.json"
        if post_dir.exists(path):
            raise ShipError(f"{message.id} exists; messages are immutable")  # law: post-is-files
        durable.replace_file(path, message.to_json())
        return path

    def _loaded(self) -> tuple[list[Message], dict[str, list[Message]]]:
        reading = _READING.get()
        if reading is not None:
            key = self.dir.absolute()
            if key not in reading:
                reading[key] = self._load_current()
            return reading[key]
        return self._load_current()

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
            stamp = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns)
            if (self._cache is None or self._cache[0] != stamp or cached_at is None
                    or not cached_at <= started <= checked):
                with os.scandir(self.dir) as entries:
                    paths = [self.dir / entry.name for entry in entries if entry.name.endswith('.json')]
                messages = sorted((Message.from_json(p.read_text()) for p in paths), key=lambda m: (m.at, m.id))
                replies: dict[str, list[Message]] = {}
                for message in messages:
                    if message.re:
                        replies.setdefault(message.re, []).append(message)
                self._cache = (stamp, messages, replies)
                finished = time.time_ns()
                if max(info.st_mtime_ns, info.st_ctime_ns) + POST_TIMESTAMP_TICK_NS < started <= checked <= finished:
                    self._cache_at = started
            else:
                self._cache_at = cached_at
            return self._cache[1], self._cache[2]
        try:
            descriptor = post_dir.open_directory(self.dir, create=False)
        except FileNotFoundError:
            self._cache = None
            return [], {}
        try:
            info = os.fstat(descriptor)
            checked = time.time_ns()
            stamp = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns)
            if (self._cache is None or self._cache[0] != stamp or cached_at is None
                    or not cached_at <= started <= checked):
                paths = post_dir.json_paths(self.dir)
                messages = []
                for path in paths:
                    try:
                        text = post_dir.read_text(path)
                    except ShipError as error:
                        if str(error) == 'post path is not a regular file':
                            continue  # unread; a special file is not a message
                        raise
                    messages.append(Message.from_json(text))
                messages.sort(key=lambda m: (m.at, m.id))
                replies: dict[str, list[Message]] = {}
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
        finally:
            os.close(descriptor)

    def all(self) -> list[Message]:
        """Every message, oldest first."""
        return list(self._loaded()[0])

    def get(self, ref: str) -> Message:
        """A message by its id or its short form; ambiguity refused, never guessed."""
        found = [m for m in self.all() if ref in (m.id, m.short)]
        if not found:
            raise ShipError(f"no message {ref!r} in {self.dir}")
        if len(found) > 1:
            raise ShipError(f"{ref!r} names {len(found)} messages; use the full id")
        return found[0]

    def for_seat(self, seat: str) -> list[Message]:
        return [m for m in self.all() if m.to == seat]

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


# -- the typed form ----------------------------------------------------------------------


SECOND = "qd > second delivery of {id}; you may have it already: act on it once"


def typed(messages: list[Message], *, second: Collection[str] = (), whole: bool = False) -> str:
    """Messages under their envelopes; whole preserves the exact thread body.

    A second delivery keeps its explicit line before the unchanged envelope. Ordinary delivery
    retains its existing trailing-space projection; thread reading can request the whole body.
    """
    parts = []
    for message in messages:
        header = f"qd > {message.kind} from {message.sender} · {message.short}"
        if message.re:
            header += f" · re {short_of(message.re)}"
        if message.subject:
            header += f" · {message.subject}"
        if message.due:
            header += f" · due {message.due}"
        footer = f"qd > inbox: post/inbox/{message.to}.md"
        if message.obligation:
            footer = f"qd > reply: qd post {REPLY_VERB[message.kind]} --re {message.short} · {footer[5:]}"
        again = SECOND.format(id=message.id) + "\n" if message.id in second else ""
        body = message.body if whole else message.body.rstrip()
        parts.append(f"{again}{header}\n{body}\n{footer}")
    return "\n".join(parts) + "\n"


def delivered_forms(message: Message) -> tuple[str, str]:
    """The two forms a message is typed in: a first delivery, and a second that says it is one."""
    return typed([message]), typed([message], second=(message.id,))


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
    def _locked(self):
        descriptor = post_dir.open_lock(self.dir / '.lock')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def open(self, message: Message, seat: str, pane: str | None, pid: int | None, launched_at: str | None) -> Attempt:
        with self._locked():
            at = now()
            prefix = f'{message.id}-'
            history = [path for path in post_dir.json_paths(self.dir) if path.name.startswith(prefix)]
            history += [a.path for name, a in self._retired().items()
                        if name.startswith(prefix) and name.endswith('.json')]
            number = max((int(p.stem.rsplit('-', 1)[1]) for p in history), default=0) + 1
            attempt = Attempt(message.id, seat, at.isoformat(timespec="microseconds"), pane, pid, launched_at,
                              path=self.dir / f"{message.id}-{number}.json")
            self._write(attempt)
        return attempt

    def close(self, attempt: Attempt, result: str, detail: str = "") -> Attempt:
        with self._locked():
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
            info = path.lstat()
            stamp = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            cached = self._retired_cache.get(path.name)
            if cached is None or cached[0] != stamp:
                data = json.loads(post_dir.read_text(path))
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
        # Attempts are mutable, including writes from another process. Directory
        # times alone miss same-path edits; stat every entry, parse only changes.
        retired = self._retired(progress)
        try:
            children = post_dir.list_children(self.dir)
        except FileNotFoundError:
            self._cache = {}
            return sorted(retired.values(), key=lambda a: (a.at, a.path.name))
        current = {}
        for index, (name, mode) in enumerate(children):
            if progress is not None and index % 200 == 0:
                progress()
            if not name.endswith('.json'):
                continue
            if stat.S_ISLNK(mode):
                post_dir._link_held(self.dir / name)
            info = (self.dir / name).lstat()
            stamp = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            cached = self._cache.get(name)
            if cached is None or cached[0] != stamp:
                path = self.dir / name
                cached = stamp, Attempt(**json.loads(post_dir.read_text(path)), path=path)
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
        skip = {m for m, r in (self.latest() if results is None else results).items()
                if r in (*ACCEPTED, "blind", None, "pending", "abandoned", "uncertain", "not-in-closed-record")
                or r == "unacted" and not for_launch}  # law: one-door
        if not for_launch:
            from .compaction import withheld
            reserved, transitioning = withheld(store.dir.parent.parent, seat)
            if transitioning:
                return []
            skip |= reserved
        return [m for m in store.for_seat(seat) if m.id not in skip and m.kind != "ack"]  # law: open-until-closed


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
    store, attempts = Store(runtime), Attempts(runtime)
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
                waiting = attempts.undelivered(store, seat)
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
    """Close a watch's attempts once their text has reached the seat."""
    attempts = Attempts(runtime)
    for attempt in opened:
        attempts.close(attempt, "watch", "printed by qd inbox --wait")
