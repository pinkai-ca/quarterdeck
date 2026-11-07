"""Post: orders, reports, questions, answers and acknowledgments as immutable files.

Each message is one JSON file in ``post/store/``, the canonical copy (law: post-is-files).
Only a report from the order's addressee closes an order; an ack claims it for the Ledger
and closes nothing (law: open-until-closed). ``post/inbox/<seat>.md`` is the rendered
view: open obligations first, oldest first, then what was already delivered.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from .ship import ShipError

KINDS = ("order", "report", "question", "answer", "ack")
SPEEDS = ("now", "interrupt")
REPLY_VERB = {"order": "report", "question": "answer"}


def now() -> datetime:
    return datetime.now().astimezone().replace(microsecond=0)


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
        return f"{self.id[:3]}-{self.id.rsplit('-', 1)[1]}"

    @property
    def obligation(self) -> bool:
        return self.kind in REPLY_VERB

    def to_json(self) -> str:
        data = asdict(self)
        data["from"] = data.pop("sender")
        data["at"] = self.at.isoformat(timespec="seconds")
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"

    @classmethod
    def from_json(cls, text: str) -> "Message":
        data = json.loads(text)
        data["sender"] = data.pop("from")
        data["at"] = datetime.fromisoformat(data["at"])
        return cls(**data)


class Store:
    """Every message of a ship, one immutable file each."""

    def __init__(self, runtime: Path):
        self.dir = runtime / "post" / "store"

    def new(self, kind: str, sender: str, to: str, body: str, **fields) -> Message:
        if kind not in KINDS:
            raise ShipError(f"no message kind {kind!r}; the kinds are {', '.join(KINDS)}")
        if fields.get("speed") not in (None, *SPEEDS):
            raise ShipError(f"no speed {fields['speed']!r}; --now or --interrupt")
        if fields.get("re"):
            fields["re"] = self.get(fields["re"]).id
        at = now()
        message = Message(self._new_id(kind, sender, at), kind, sender, to, at, body, **fields)
        self.write(message)
        return message

    def _new_id(self, kind: str, sender: str, at: datetime) -> str:
        taken = {m.short for m in self.all()}
        while True:
            candidate = f"{kind[:3]}-{at.strftime('%Y%m%d-%H%M')}-{sender}-{secrets.token_hex(2)}"
            if f"{kind[:3]}-{candidate.rsplit('-', 1)[1]}" not in taken:
                return candidate

    def write(self, message: Message) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"{message.id}.json"
        if path.exists():
            raise ShipError(f"{message.id} exists; messages are immutable")
        tmp = path.with_suffix(".tmp")
        tmp.write_text(message.to_json())
        os.replace(tmp, path)
        return path

    def all(self) -> list[Message]:
        if not self.dir.is_dir():
            return []
        messages = [Message.from_json(p.read_text()) for p in self.dir.glob("*.json")]
        return sorted(messages, key=lambda m: (m.at, m.id))

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
        return [m for m in self.all() if m.re == message.id]

    def closing_report(self, order: Message) -> Message | None:
        """The report from the addressee that closes the order, if any (law: open-until-closed)."""
        return next((m for m in self.replies(order) if m.kind == "report" and m.sender == order.to), None)

    def is_open(self, message: Message) -> bool:
        if message.kind == "order":
            return self.closing_report(message) is None
        if message.kind == "question":
            return not any(m.kind == "answer" for m in self.replies(message))
        return False

    def open_for(self, seat: str) -> list[Message]:
        return [m for m in self.for_seat(seat) if self.is_open(m)]

    def claimed(self, seat: str, before: datetime) -> str | None:
        """The order the seat last claimed with an ack before ``before``, for the Ledger."""
        acks = [m for m in self.all() if m.kind == "ack" and m.sender == seat and m.re and m.at <= before]
        return acks[-1].re if acks else None


# -- the typed form ----------------------------------------------------------------------


def typed(messages: list[Message]) -> str:
    """What the door types: each message in full, under a header and over a footer."""
    parts = []
    for message in messages:
        header = f"qd > {message.kind} from {message.sender} · {message.short}"
        if message.re:
            header += f" · re {message.re[:3]}-{message.re.rsplit('-', 1)[1]}"
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


def inbox_text(store: Store, seat: str) -> str:
    messages = store.for_seat(seat)
    open_ = [m for m in messages if store.is_open(m)]
    rest = [m for m in messages if not store.is_open(m)]
    lines = [f"# Inbox: {seat}", ""]
    lines += [f"## Open ({len(open_)})", ""]
    for message in open_:
        lines += _entry(message, store)
    lines += [f"## Received ({len(rest)})", ""]
    for message in reversed(rest):
        lines += _entry(message, store)
    return "\n".join(lines).rstrip() + "\n"


def _entry(message: Message, store: Store) -> list[str]:
    head = f"### {message.kind} {message.short} from {message.sender} · {message.at.strftime('%m-%d %H:%M')}"
    if message.subject:
        head += f" · {message.subject}"
    details = []
    if message.re:
        details.append(f"re {message.re}")
    if message.due:
        details.append(f"due {message.due}")
    if message.kind == "order":
        closed = store.closing_report(message)
        details.append(f"closed by {closed.short}" if closed else f"open · reply: qd post report --re {message.short}")
    return [head, *([" · ".join(details)] if details else []), "", message.body.rstrip(), ""]


def render_inbox(runtime: Path, store: Store, seat: str) -> Path:
    folder = runtime / "post" / "inbox"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{seat}.md"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(inbox_text(store, seat))
    os.replace(tmp, path)
    return path


def with_speed(message: Message, speed: str | None) -> Message:
    return replace(message, speed=speed)
