"""Read an immutable post thread, from its first message through every reply."""

from __future__ import annotations

from dataclasses import dataclass
import heapq

from . import post
from .ship import ShipError

INDEXED_LAUNCH_DETAIL = 'indexed at launch; body not confirmed read'
# law: context-is-cache -- a thread is printed whole as far as the reader sent or received it
NOT_YOURS = 'in this thread; you neither sent nor received it, so its words are not shown'
CONTINUATION_LABEL = ('a recovery continuation for an earlier conversation of this seat; '
                      'it is not addressed to this conversation')


@dataclass(frozen=True)
class Thread:
    root: str
    requested: str
    messages: tuple[post.Message, ...]  # those the reader sent or received, a refused one as its notice
    others: tuple[str, ...] = ()  # '<kind> <id>' of the thread's other messages: that they exist, nothing more
    continuations: tuple[str, ...] = ()  # delivered recovery continuations among messages, labelled when printed


def party(message: post.Message, seat: str) -> bool:
    """The reader sent or received this message: only then are its words the reader's to read."""
    return seat in (message.to, message.sender)


def continuation_texts() -> frozenset[str]:
    """The fixed texts a thinking recovery posts as its continuation report."""
    from .harness import opencode_thinking, pi_thinking
    return frozenset({pi_thinking.Client.continuation, opencode_thinking.Client.continuation})


def delivered_continuations(store: post.Store, reader: str, messages) -> tuple[str, ...]:
    """Recovery continuations already delivered to this seat: they continued a conversation it may no longer have."""
    texts = continuation_texts()
    candidates = [m for m in messages if m.sender == post.SHIP and m.kind == 'report' and m.to == reader
                  and m.body in texts]
    if not candidates:
        return ()
    attempts = post.Attempts(store.dir.parent.parent)
    return tuple(m.id for m in candidates
                 if any(a.seat == reader and a.result in ('delivered', 'watch') for a in attempts.for_message(m.id)))


@dataclass(frozen=True)
class IndexRow:
    id: str
    kind: str
    sender: str
    subject: str | None
    due: str | None
    amendments: tuple[str, ...]
    notice: str | None = None
    active: tuple[str, ...] = ()
    awaiting: tuple[str, ...] = ()
    withheld: tuple[str, ...] = ()


def index_rows(store: post.Store, roots: tuple[post.Message, ...],
               incoming: tuple[post.Message, ...], withheld: frozenset[str],
               sent: tuple[post.Message, ...] = (), seat: str | None = None) -> tuple[IndexRow, ...]:
    """Metadata only. Callers supply their existing open/unread inventory and exclusions.

    With ``seat``, each row's amendments, active, waiting and withheld lists hold only messages that seat
    sent or received, and a root it neither sent nor received gives no subject."""
    by_id = {message.id: message for message in store.all()}

    root_for: dict[str, str] = {}

    def root_of(message: post.Message) -> str:
        current, trail, seen = message, [], set()
        while current.id not in root_for:
            if current.id in seen:
                raise ShipError('thread ancestry has a cycle; inspect the immutable posts')
            seen.add(current.id)
            trail.append(current.id)
            if not current.re:
                break
            if current.re not in by_id:
                raise ShipError(f'thread ancestry is incomplete: missing {current.re}; inspect the immutable posts')
            current = by_id[current.re]
        identifier = root_for.get(current.id, current.id)
        root_for.update((value, identifier) for value in trail)
        return identifier

    eligible = {message.id for message in (*roots, *incoming, *sent)}
    selected_roots = {root_of(by_id[identifier]) for identifier in eligible}
    active_ids = {message.id for message in (*roots, *incoming)}
    grouped: dict[str, list[post.Message]] = {identifier: [] for identifier in selected_roots}
    for message in by_id.values():
        try:
            identifier = root_of(message)
        except ShipError:
            continue  # malformed unrelated history cannot hold this seat's index
        if identifier in grouped:
            grouped[identifier].append(message)
    rows = []
    for root in sorted((by_id[identifier] for identifier in selected_roots), key=lambda message: (message.at, message.id)):
        members = grouped[root.id] if seat is None else [m for m in grouped[root.id] if party(m, seat)]
        shown = post._shown(root, withheld)
        subject = shown.subject if seat is None or party(root, seat) else None
        amendments = tuple(message.id for message in members if message.id != root.id)
        waiting = tuple(f'{message.id}: sent, awaiting {message.to}' for message in sent
                        if root_for[message.id] == root.id)
        hidden = tuple(message.id for message in members if message.id in withheld)
        notice = post._shown(by_id[hidden[0]], withheld).body if hidden else None
        rows.append(IndexRow(root.id, root.kind, root.sender, subject, root.due, amendments,
                             notice,
                             tuple(message.id for message in members if message.id in active_ids), waiting,
                             hidden))
    return tuple(rows)


def whole(store: post.Store, reference: str, reader: str) -> Thread:
    """One read-only snapshot; missing ancestry or a cycle is explicit, never omitted."""
    requested = store.get(reference)
    messages = store.all()
    by_id = {message.id: message for message in messages}
    root = requested
    visited: set[str] = set()
    while root.re:
        if root.id in visited:
            raise ShipError('thread ancestry has a cycle; inspect the immutable posts')
        visited.add(root.id)
        if root.re not in by_id:
            raise ShipError(f'thread ancestry is incomplete: missing {root.re}; inspect the immutable posts')
        root = by_id[root.re]
    children: dict[str, list[str]] = {}
    for message in messages:
        if message.re:
            children.setdefault(message.re, []).append(message.id)
    included, ordered = set(), []
    waiting = [(root.at, root.id)]
    while waiting:
        _, identifier = heapq.heappop(waiting)
        if identifier not in included:
            included.add(identifier)
            ordered.append(by_id[identifier])
            for child in children.get(identifier, ()):
                heapq.heappush(waiting, (by_id[child].at, child))
    withheld = post._withheld(store, reader)
    mine = [message for message in ordered if party(message, reader)]
    others = tuple(f'{message.kind} {message.id}' for message in ordered if not party(message, reader))
    shown = tuple(post._shown(message, withheld) for message in mine)
    visible = [message for message, masked in zip(mine, shown) if masked is message]  # a refused one stays its notice
    return Thread(root.id, requested.id, shown, others, delivered_continuations(store, reader, visible))


def render(thread: Thread) -> str:
    # law: context-is-cache -- one command prints the thread whole, as far as the seat sent or received it
    head = f'qd > thread {thread.root}: {len(thread.messages)} messages; read whole before acting on any message in it'
    if thread.others:
        head += f'; {len(thread.others)} more in it, named below by id only'
    parts = [head + '\n']
    for message in thread.messages:
        if message.id in thread.continuations:
            parts.append(CONTINUATION_LABEL + '\n')
        parts.append(post.typed([message], whole=True))
    parts += [f'qd > {other} {NOT_YOURS}\n' for other in thread.others]
    return ''.join(parts) + f'qd > end of thread {thread.root}\n'

