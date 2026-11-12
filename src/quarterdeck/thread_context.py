"""Open-thread context, with incoming carriage separate from outgoing explanation."""

from dataclasses import dataclass
from collections.abc import Collection, Iterable

from . import post


@dataclass(frozen=True)
class Entry:
    message: post.Message
    context_only: bool = False


@dataclass(frozen=True)
class Selection:
    roots: tuple[post.Message, ...]
    entries: tuple[Entry, ...]

    @property
    def incoming(self) -> list[post.Message]:
        """Only these messages may receive launch attempts; context has no receipt."""
        return [entry.message for entry in self.entries if not entry.context_only]

    def render(self) -> str:
        parts = []
        for entry in self.entries:
            message = entry.message
            if not entry.context_only:
                parts.append(post.typed([message]))
                continue
            header = (f"qd context > own question to {message.to} · {message.short}"
                      " · context only; no delivery or reply requested")
            if message.re:
                header += f" · re {post.short_of(message.re)}"
            parts.append(f"{header}\n{message.body.rstrip()}\nqd context > end context\n")
        return ''.join(parts)


def select(store: post.Store, seat: str, extra: Iterable[post.Message] = (), *,
           consumed: Collection[str] = (), excluded: Collection[str] = ()) -> Selection:
    """Carry open roots and their incoming amendments once, in root/thread order.

    ``consumed`` must describe the actual conversation being resumed, never a
    retired conversation or launch receipt. A new conversation passes no ids.
    Open roots always return, preserving canonical ``Store.is_open`` semantics.
    Unread/queued messages join the same inventory through ``extra``. Only the
    seat's own outgoing questions on a selected incoming message's ancestry
    supply explanatory context; other participants' bodies remain private.
    Excluded ids retain their open-root identity but never supply any body.
    """
    messages = store.all()
    by_id = {message.id: message for message in messages}
    roots = tuple(message for message in messages if message.to == seat and store.is_open(message))
    root_ids = {message.id for message in roots}
    children: dict[str, list[post.Message]] = {}
    for message in messages:
        if message.re:
            children.setdefault(message.re, []).append(message)
    seen: set[str] = set()
    entries = []

    def append(message: post.Message, context_only: bool = False) -> None:
        if message.id not in seen and message.id not in excluded:
            seen.add(message.id)
            entries.append(Entry(message, context_only))

    for root in roots:
        descendants = {}
        waiting = [root]
        while waiting:
            message = waiting.pop()
            if message.id in descendants:
                continue
            descendants[message.id] = message
            waiting.extend(children.get(message.id, ()))
        incoming = {message.id for message in descendants.values()
                    if message.to == seat and message.kind != 'ack'
                    and message.id not in excluded
                    and (message.id not in consumed or message.id in root_ids)}
        bridges = set()
        for message_id in incoming:
            parent = descendants[message_id].re
            visited = set()
            while parent in descendants and parent not in visited:
                visited.add(parent)
                message = descendants[parent]
                if message.sender == seat and message.kind == 'question' and message.to != seat:
                    bridges.add(parent)
                parent = message.re
        append(root)
        for message in sorted(descendants.values(), key=lambda message: (message.at, message.id)):
            if message.id in incoming:
                append(message)
            elif message.id in bridges:
                append(message, context_only=True)
    # Extra unread work outside an open thread keeps ordinary timestamp order.
    for message in sorted(extra, key=lambda message: (message.at, message.id)):
        if message.to == seat and message.kind != 'ack' and message.id not in consumed:
            append(by_id.get(message.id, message))
    return Selection(roots, tuple(entries))
