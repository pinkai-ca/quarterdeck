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
import os
import secrets
import stat
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

from . import durable
from .ship import ShipError

KINDS = ("order", "report", "question", "answer", "ack")
SPEEDS = ("now", "interrupt")
SHIP = "ship"  # the sender of the ship's own notices, and an inbox a seat may answer to
REPLY_VERB = {"order": "report", "question": "answer"}
POST_TIMESTAMP_TICK_NS = 2_000_000_000  # conservative filesystem tick; see docs/DESIGN.md


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
    folder.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(seat.encode()).hexdigest()
    descriptor = os.open(folder / (name + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        opened, current = os.fstat(descriptor), (folder / (name + '.lock')).lstat()
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
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"{message.id}.json"
        if path.exists():
            raise ShipError(f"{message.id} exists; messages are immutable")  # law: post-is-files
        durable.replace_file(path, message.to_json())
        return path

    def _loaded(self) -> tuple[list[Message], dict[str, list[Message]]]:
        """Immutable messages and replies; recent directory listings stay untrusted."""
        cached_at = self._cache_at
        self._cache_at = None  # a failed refresh cannot certify the previous listing
        started = time.time_ns()
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
            # Certify only a listing begun after the entire timestamp bucket.
            # An unsafe listing must be refreshed even when its stamp has aged.
            if max(info.st_mtime_ns, info.st_ctime_ns) + POST_TIMESTAMP_TICK_NS < started <= checked <= finished:
                self._cache_at = started
        else:
            self._cache_at = cached_at
        return self._cache[1], self._cache[2]

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

    def is_open(self, message: Message) -> bool:
        if message.kind == "order":
            return self.closing_report(message) is None
        if message.kind == "question":
            return not any(m.kind == "answer" for m in self.replies(message))
        return False

    def open_for(self, seat: str) -> list[Message]:
        return [m for m in self.for_seat(seat) if self.is_open(m)]

    def claimed(self, seat: str, before: datetime) -> str | None:
        """The order the seat last claimed with an ack and still had open at ``before``, for the Ledger."""
        messages = self.all()
        by_id = {m.id: m for m in messages}
        for ack in reversed([m for m in messages if m.kind == "ack" and m.sender == seat and m.re and m.at <= before]):
            order = by_id.get(ack.re)
            closed = self.closing_report(order) if order else None
            if order and (closed is None or closed.at > before):
                return order.id
        return None


# -- the typed form ----------------------------------------------------------------------


def typed(messages: list[Message]) -> str:
    """What the door types: each message in full, under a header and over a footer."""
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
        parts.append(f"{header}\n{message.body.rstrip()}\n{footer}")
    return "\n".join(parts) + "\n"


def briefed_header(seat: str, role: str, open_messages: list[Message]) -> str:
    orders = sum(m.kind == "order" for m in open_messages)
    questions = len(open_messages) - orders
    what = ", ".join(filter(None, [
        f"{orders} order{'s' if orders != 1 else ''} open" if orders else "",
        f"{questions} question{'s' if questions != 1 else ''} open" if questions else "",
    ])) or "nothing open"
    return f"qd > briefed as {seat}, {role} · {what}"


# -- the inbox ---------------------------------------------------------------------------


def inbox_reading(store: Store, seat: str) -> dict:
    messages = [_shown(m, withheld) for withheld in [_withheld(store, seat)] for m in store.for_seat(seat)]
    return {"seat": seat, "open": [m for m in messages if store.is_open(m)],
            "received": list(reversed([m for m in messages if not store.is_open(m)])),
            "closed_by": {m.id: closed.id for m in messages if m.kind == "order"
                          and (closed := store.closing_report(m))}}


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
    return render_inbox_reading(inbox_reading(store, seat))


def render_inbox_reading(data: dict) -> str:
    lines = [f"# Inbox: {data['seat']}", ""]
    for title, key in (("Open", "open"), ("Received", "received")):
        lines += [f"## {title} ({len(data[key])})", ""]
        for message in data[key]:
            lines += _entry(message, data["closed_by"].get(message.id))
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
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{seat}.md"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(inbox_text(store, seat))
    os.replace(tmp, path)
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


class Attempts:
    def __init__(self, runtime: Path):
        self.dir = runtime / "post" / "attempts"
        self._cache = {}

    @contextmanager
    def _locked(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        with (self.dir / ".lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def open(self, message: Message, seat: str, pane: str | None, pid: int | None, launched_at: str | None) -> Attempt:
        with self._locked():
            at = now()
            number = len(list(self.dir.glob(f"{message.id}-*.json"))) + 1
            attempt = Attempt(message.id, seat, at.isoformat(timespec="microseconds"), pane, pid, launched_at,
                              path=self.dir / f"{message.id}-{number}.json")
            self._write(attempt)
        return attempt

    def close(self, attempt: Attempt, result: str, detail: str = "") -> Attempt:
        with self._locked():
            current = Attempt(**json.loads(attempt.path.read_text()), path=attempt.path)
            if current.result in ("abandoned", "not-in-closed-record", *UNSETTLED):
                return current  # terminal even if an old door/watch finishes after cleanup or a receipt
            done = replace(attempt, result=result, detail=detail)
            self._write(done)
            return done

    def judge(self, attempt: Attempt, result: str, detail: str = "") -> bool:
        """Judge only an unfinished attempt; an intervening door/watch verdict stands."""
        with self._locked():
            current = Attempt(**json.loads(attempt.path.read_text()), path=attempt.path)
            if current.result is not None:
                return False
            self._write(replace(current, result=result, detail=detail))
            return True

    def abandon(self, seat: str, detail: str) -> list[Attempt]:
        """Retire uncertainty, never a confirmed receipt; keep every historical file."""
        changed = []
        with self._locked():
            for attempt in self.all():
                if attempt.seat != seat or attempt.result in ("delivered", "launch", "watch", "abandoned", "not-in-closed-record"):
                    continue
                done = replace(attempt, result="abandoned",
                               detail=f"{detail}; previous result: {attempt.result or 'unfinished'}")
                self._write(done)
                changed.append(done)
        return changed

    def _write(self, attempt: Attempt) -> None:
        data = {k: v for k, v in asdict(attempt).items() if k != "path"}
        durable.replace_file(attempt.path, json.dumps(data, indent=2) + "\n")

    def all(self) -> list[Attempt]:
        # Attempts are mutable, including writes from another process. Directory
        # times alone miss same-path edits; stat every entry, parse only changes.
        try:
            entries = os.scandir(self.dir)
        except FileNotFoundError:
            self._cache = {}
            return []
        current = {}
        with entries:
            for entry in entries:
                if not entry.name.endswith('.json'):
                    continue
                info = entry.stat()
                stamp = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                cached = self._cache.get(entry.name)
                if cached is None or cached[0] != stamp:
                    path = self.dir / entry.name
                    cached = stamp, Attempt(**json.loads(path.read_text()), path=path)
                current[entry.name] = cached
        # A failed scan/read must propagate, never certify a partial snapshot.
        self._cache = current
        found = [value[1] for value in current.values()]
        return sorted(found, key=lambda a: (a.at, a.path.name))

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


def watch_armed(runtime: Path, seat: str) -> bool:
    path = watch_file(runtime, seat)
    try:
        pid = int(path.read_text().split()[0])
        os.kill(pid, 0)
    except (FileNotFoundError, ValueError, IndexError, OSError):
        return False
    return True


def wait_batch(runtime: Path, seat: str, interval: float = 1.0, stop_after: float | None = None) -> tuple[list[Message], list[Attempt]] | None:
    """Block until something new is addressed to ``seat``; return it in full, with its open attempts.

    The caller prints the text and then closes the attempts with ``printed``, so a wait
    that dies between the two leaves them open to be judged, never silently taken. The
    wait registers itself while it runs, so the service and the status line know a watch
    is armed. Nothing is typed into the seat's pane: this is the Office's one path.
    """
    store, attempts = Store(runtime), Attempts(runtime)
    path = watch_file(runtime, seat)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{os.getpid()} {now().isoformat(timespec='seconds')}\n")
    deadline = None if stop_after is None else datetime.now().timestamp() + stop_after
    try:
        while True:
            with admission(runtime, seat):
                waiting = attempts.undelivered(store, seat)
                if waiting:
                    opened = [attempts.open(message, seat, None, None, None) for message in waiting]
                    return waiting, opened
            if deadline is not None and datetime.now().timestamp() > deadline:
                return None
            time.sleep(interval)
    finally:
        path.unlink(missing_ok=True)


def wait(runtime: Path, seat: str, interval: float = 1.0, stop_after: float | None = None) -> tuple[str, list[Attempt]] | None:
    batch = wait_batch(runtime, seat, interval, stop_after)
    return (typed(batch[0]), batch[1]) if batch is not None else None


def printed(runtime: Path, opened: list[Attempt]) -> None:
    """Close a watch's attempts once their text has reached the seat."""
    attempts = Attempts(runtime)
    for attempt in opened:
        attempts.close(attempt, "watch", "printed by qd inbox --wait")
