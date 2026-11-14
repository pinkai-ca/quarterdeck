"""Immutable message identity, JSON and delivered envelopes shared with attribution.

Store listing, publication and transport live in post; this module only interprets
message values and their exact typed forms.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Collection

KINDS = ("order", "report", "question", "answer", "ack")
REPLY_VERB = {"order": "report", "question": "answer"}


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


SECOND = "qd > second delivery of {id}; you may have it already: act on it once"


def typed(messages: list[Message], *, second: Collection[str] = (), whole: bool = False) -> str:
    """Messages under their envelopes; whole preserves the exact thread body.

    A second delivery keeps its explicit line before the unchanged envelope. Ordinary delivery
    retains its existing trailing-space projection; thread reading can request the whole body.
    """
    return _typed(messages, second=second, whole=whole)


def _typed(messages: list[Message], *, second: Collection[str] = (), whole: bool = False,
           legacy: bool = False) -> str:
    parts = []
    for message in messages:
        header = f"qd > {message.kind} from {message.sender} · {message.short}"
        if message.re:
            header += f" · re {short_of(message.re)}"
        if message.subject:
            header += f" · {message.subject}"
        if message.due:
            header += f" · due {message.due}"
        footer = ""
        if legacy:
            footer = f"\nqd > inbox: post/inbox/{message.to}.md"
        if message.obligation:
            footer = f"\nqd > reply: qd post {REPLY_VERB[message.kind]} --re {message.short}"
            if legacy:
                footer += f" · inbox: post/inbox/{message.to}.md"
        again = SECOND.format(id=message.id) + "\n" if message.id in second else ""
        body = message.body if whole else message.body.rstrip()
        parts.append(f"{again}{header}\n{body}{footer}")
    return "\n".join(parts) + "\n"


def delivered_forms(message: Message) -> tuple[str, ...]:
    """Current first/second deliveries, then their legacy inbox-footer forms for old receipts."""
    return (typed([message]), typed([message], second=(message.id,)),
            _typed([message], legacy=True), _typed([message], second=(message.id,), legacy=True))
