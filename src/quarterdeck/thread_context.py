"""Open-thread context, with incoming carriage separate from outgoing explanation."""

from dataclasses import dataclass
from collections.abc import Collection, Iterable
from datetime import datetime
import json
import re

from . import post
from .harness.types import Input


@dataclass(frozen=True)
class Entry:
    message: post.Message
    context_only: bool = False


def closed_order_answers(store: post.Store, seat: str) -> frozenset[str]:
    """Answers below the recipient's reported order are settled carriage history.

    The nearest order owns the answer: a newer open order must not inherit an
    older order's closure. Missing/cyclic ancestry proves nothing. This is one
    read-only snapshot, with no receipt written and nothing kept between calls.
    """
    with post.reading_pass(store=store):
        messages = store.all()
        by_id = {message.id: message for message in messages}
        settled: dict[str, bool] = {}

        def closed(identifier):
            trail, seen = [], set()
            while identifier in by_id and identifier not in settled and identifier not in seen:
                seen.add(identifier)
                message = by_id[identifier]
                if message.kind == 'order':
                    settled[identifier] = message.to == seat and store.closing_report(message) is not None
                    break
                trail.append(identifier)
                identifier = message.re
            result = settled.get(identifier, False)
            settled.update((item, result) for item in trail)
            return result

        return frozenset(message.id for message in messages
                         if message.to == seat and message.kind == 'answer' and closed(message.re))


def context(message: post.Message) -> str:
    header = (f"qd context > own question to {message.to} · {message.short}"
              " · context only; no delivery or reply requested")
    if message.re:
        header += f" · re {post.short_of(message.re)}"
    return f"{header}\n{message.body.rstrip()}\nqd context > end context\n"


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
            parts.append(context(message))
        return ''.join(parts)


def consumed_ids(store: post.Store, seat: str, role: str, inputs: Iterable[Input], *,
                 excluded: Collection[str] = ()) -> frozenset[str]:
    """Read complete canonical inputs, never search for envelopes inside prose.

    The caller establishes the actual resumed record and its reader. Every token
    must match in full, from the beginning through the end of one native input.
    Header counts validate formatting only; context and stubs yield no ids.
    """
    from .refusal import stub

    messages = store.all()
    told = post._told(store, seat) if excluded else frozenset()  # the stub says what the refusal's event holds
    sender_seat = post._sender_seat(store) if excluded else None
    tokens = {}
    for message in messages:
        incoming = message.to == seat and message.kind != 'ack'
        if message.id in excluded:
            told_it = message.id in told
            token = stub(message, told_it, None if told_it or sender_seat is None else sender_seat(message.sender)) \
                if incoming else None
            incoming = False
        elif incoming:
            token = post.typed([message]).strip()
            second = post.typed([message], second=(message.id,)).strip()  # a second delivery reads as consumed too
            tokens.setdefault(second.partition('\n')[0], []).append((second, message, incoming))
        elif message.sender == seat and message.kind == 'question':
            token = context(message).strip()
        else:
            token = None
        if token:
            tokens.setdefault(token.partition('\n')[0], []).append((token, message, incoming))
    orders = [m for m in messages if m.to == seat and m.kind == 'order']
    questions = [m for m in messages if m.to == seat and m.kind == 'question']

    def header(line):
        prefix = f'qd > briefed as {seat}, {role} · '
        if not line.startswith(prefix):
            return False
        what = line[len(prefix):]
        if what == 'nothing open':
            return line == post.briefed_header(seat, role, [])
        match = re.fullmatch(r'(?:(\d+) orders? open)?(?:, )?(?:(\d+) questions? open)?', what)
        if match is None:
            return False
        no, nq = (int(value or 0) for value in match.groups())
        return (no <= len(orders) and nq <= len(questions)
                and line == post.briefed_header(seat, role, orders[:no] + questions[:nq]))

    consumed = set()
    for item in inputs:
        if (not isinstance(item.text, str) or not isinstance(item.at, datetime) or item.at.tzinfo is None
                or item.kind not in ('accepted', 'consumed')
                or item.kind == 'consumed' and (not isinstance(item.consumed_at, datetime)
                    or item.consumed_at.tzinfo is None or item.consumed_at < item.at)):
            continue
        text = item.text.strip()
        first, separator, rest = text.partition('\n')
        if header(first):
            text = rest if separator else ''
        found = set()
        while text:
            matches = [(token, message, incoming) for token, message, incoming in tokens.get(text.partition('\n')[0], ())
                       if text == token or text.startswith(token + '\n')]
            if len(matches) != 1:
                break
            token, message, incoming = matches[0]
            if message.at > item.at:
                break
            if incoming:
                found.add(message.id)
            text = text[len(token):]
            if text.startswith('\n'):
                text = text[1:]
        else:
            consumed.update(found)
    return frozenset(consumed)


def _terminal_notice(store: post.Store, message: post.Message, root: post.Message,
                     seat: str, by_id: dict[str, post.Message]) -> bool:
    """Recognize a published terminal intent by metadata only, without repairing it.

    A group's saved notice is its terminal marker. A cascade additionally saves
    done/stopped. Neither an arbitrary report on the same root nor the existence
    of a structure alone establishes that this candidate is its terminal notice.
    Do not call the structures' full validators: those compare message bodies.
    """
    if not (message.sender == post.SHIP and message.kind == 'report' and message.to == seat
            and root.to == seat and message.re == root.id):
        return False
    if re.fullmatch(r'[A-Za-z0-9_-]+', root.id) is None:
        return False

    from . import cascade_runtime, group_post
    from .ship import ShipError

    def matches(saved, actual):
        if not isinstance(saved, dict) or actual is None:
            return False
        expected = dict(id=actual.id, kind=actual.kind, to=actual.to,
                        at=actual.at.isoformat(), re=actual.re, subject=actual.subject,
                        due=actual.due, speed=actual.speed)
        expected['from'] = actual.sender
        return all(saved[key] == value for key, value in expected.items())

    runtime = store.dir.parent.parent
    group = runtime / 'post' / 'groups' / (root.id + '.json')
    cascade = runtime / 'ring' / root.id / 'state.json'
    try:
        paths = []
        for path in (group, cascade):
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            paths.append(path)
        if len(paths) != 1:
            return False  # no evidence, or competing structures on this root
        data = json.loads(cascade_runtime.read_file(paths[0], max_bytes=1024 * 1024))
        if not isinstance(data, dict) or type(data.get('format')) is not int or data['format'] != 1:
            return False
        if paths[0] == cascade:
            ring = data['declaration']
            if (root.kind != 'question' or data['question'] != root.id
                    or data['status'] not in ('done', 'stopped') or not isinstance(ring, dict)
                    or ring['orchestrator'] != seat or not isinstance(ring['seats'], list)
                    or len(ring['seats']) != 3
                    or any(not isinstance(name, str) or not name or name == seat for name in ring['seats'])
                    or len(set(ring['seats'])) != 3 or not isinstance(data['intents'], dict)):
                return False
            return matches(data['intents']['final'], message)

        if (root.kind != 'order' or data['lead'] != seat
                or not isinstance(data['group'], str) or not data['group']
                or not matches(data['root'], root) or not matches(data['notice'], message)
                or not isinstance(data['copies'], list) or not data['copies']):
            return False
        members, identities = {}, {root.id, message.id}
        for saved in data['copies']:
            if not isinstance(saved, dict):
                return False
            copy = by_id.get(saved['id'])
            if (not matches(saved, copy) or copy.kind != 'order' or copy.sender != seat
                    or copy.re != root.id or copy.id in identities or copy.to in members.values()
                    or not isinstance(copy.to, str) or not copy.to or copy.to == seat
                    or (copy.subject, copy.due, copy.speed) != (root.subject, root.due, root.speed)):
                return False
            identities.add(copy.id)
            members[copy.id] = copy.to
        failed = data.get('failed', {})
        return isinstance(failed, dict) and all(
            identity in members and isinstance(outcome, dict) and set(outcome) == {'member', 'reason'}
            and outcome['member'] == members[identity] and outcome['reason'] in group_post.FAILURE_REASONS
            for identity, outcome in failed.items())
    except (OSError, ValueError, KeyError, TypeError, RecursionError, ShipError):
        return False  # unknown evidence grants no exception; no repair or derived artifact


def amends(message: post.Message, root: post.Message, seat: str,
           thread: dict[str, post.Message], *, terminal_notice: bool = False) -> bool:
    """Whether a message addressed to the seat in an open root's thread changes what the root asks.

    Decided by the explicit reply graph and the senders alone, never by what a
    body says. A ship report addressed to the seat directly on the open root
    amends it only with verified structure-terminal metadata supplied by select.
    Other such reports are history, even when the root's sender is ship. It also amends
    when it answers one of the seat's own questions (its
    nearest message from the seat, below the root, is that question), whoever
    sent it; or when the root's own sender sent it, unless it is a report or
    an answer replying directly to an order the seat gave: that closes or
    answers the seat's own sub-order, which is history. A question never
    amends: an open one addressed to the seat is a root of its own, and one
    already answered is settled history. Everything else in the thread is
    history too; open messages are roots of their own.
    """
    parent = thread.get(message.re) if message.re else None
    if (message.sender == post.SHIP and message.kind == 'report' and message.to == seat
            and root.to == seat and message.re == root.id):
        return terminal_notice
    if (message.kind == 'answer' and (own := _nearest_own(message, root, seat, thread)) is not None
            and own.kind == 'question'):
        return True
    if message.sender != root.sender or message.kind == 'question':
        return False
    return not (message.kind in ('report', 'answer') and parent is not None
                and parent.id != root.id and parent.sender == seat and parent.kind == 'order')


def _nearest_own(message: post.Message, root: post.Message, seat: str,
                 thread: dict[str, post.Message]) -> post.Message | None:
    parent, visited = message.re, set()
    while parent in thread and parent != root.id and parent not in visited:
        visited.add(parent)
        ancestor = thread[parent]
        if ancestor.sender == seat:
            return ancestor
        parent = ancestor.re
    return None


def select(store: post.Store, seat: str, extra: Iterable[post.Message] = (), *,
           consumed: Collection[str] = (), excluded: Collection[str] = ()) -> Selection:
    """Carry open roots and what amends them once, in root/thread order.

    ``consumed`` describes proved consumption in the resumed conversation or
    canonical read/closure settlement, never a launch receipt alone.
    Open roots always return, preserving canonical ``Store.is_open`` semantics.
    A root's thread contributes only what amends it (see ``amends``) and the
    open roots inside it; delivered history stays in the inbox and the store.
    Unread/queued messages join the same inventory through ``extra``, whatever
    their kind. Only the seat's own outgoing questions on a carried message's
    ancestry supply explanatory context; other participants' bodies remain
    private. Excluded ids retain their open-root identity but never supply any body.
    """
    messages = store.all()
    by_id = {message.id: message for message in messages}
    roots = tuple(store.open_for(seat))
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

    threads = {}
    for root in roots:
        descendants = {}
        waiting = [root]
        while waiting:
            message = waiting.pop()
            if message.id in descendants:
                continue
            descendants[message.id] = message
            waiting.extend(children.get(message.id, ()))
        threads[root.id] = descendants
    # Each root judges amendments by its own sender; a nested root's amendments keep thread order in its ancestors.
    incoming, bridges = set(), set()
    for root in roots:
        descendants = threads[root.id]
        carried = {message.id for message in descendants.values()
                   if message.to == seat and message.kind != 'ack'
                   and message.id not in excluded
                   and (message.id in root_ids
                        or message.id not in consumed and amends(
                            message, root, seat, descendants,
                            terminal_notice=_terminal_notice(store, message, root, seat, by_id)))}
        incoming |= carried
        for message_id in carried:
            parent = descendants[message_id].re
            visited = set()
            while parent in descendants and parent not in visited:
                visited.add(parent)
                message = descendants[parent]
                if message.sender == seat and message.kind == 'question' and message.to != seat:
                    bridges.add(parent)
                parent = message.re
    for root in roots:
        append(root)
        for message in sorted(threads[root.id].values(), key=lambda message: (message.at, message.id)):
            if message.id in incoming:
                append(message)
            elif message.id in bridges:
                append(message, context_only=True)
    # Extra unread work outside an open thread keeps ordinary timestamp order.
    for message in sorted(extra, key=lambda message: (message.at, message.id)):
        if message.to == seat and message.kind != 'ack' and message.id not in consumed:
            append(by_id.get(message.id, message))
    return Selection(roots, tuple(entries))
