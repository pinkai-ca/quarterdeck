"""Pure byte-bounded launch planning; posts remain immutable at their original paths.

The bound is a ship policy unless an adapter supplies a version-specific measurement.
It is neither a token estimate nor a claim about a provider's context window. Optional
read views and their navigation pages are bounded individually, not required reading.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import durable


def size(text: str) -> int:
    return len(text.encode('utf-8'))


class ImpossibleBudget(ValueError):
    """Even the minimal account or a navigable index cannot fit; publish nothing."""


@dataclass(frozen=True)
class ReadableCapacity:
    """An adapter's measured carriage bytes, after its brief and working-turn reserve.

    No adapter has such a measurement by default. A declaration must name the exact
    native version, evidence source and the reserves used in that measurement.
    """

    version: str
    bytes: int
    source: str
    reserves: str

    def __post_init__(self):
        if type(self.bytes) is not int or self.bytes <= 0 or not all((self.version, self.source, self.reserves)):
            raise ValueError('readable capacity requires positive bytes, version, source and reserves')


@dataclass(frozen=True)
class Bound:
    bytes: int
    label: str


def effective_bound(policy: int, profile: ReadableCapacity | None, version: str | None) -> Bound:
    if type(policy) is not int or policy <= 0:
        raise ValueError('carry_bytes must be a positive integer')
    if profile is None or profile.version != version:
        return Bound(policy, 'policy bound')
    return Bound(min(policy, profile.bytes), 'policy bound and measured adapter ceiling')


@dataclass(frozen=True)
class Item:
    id: str
    sender: str
    recipient: str
    kind: str
    at: str
    subject: str
    text: str
    source: Path | None = None
    context_only: bool = False
    requires: tuple[str, ...] = ()  # admitted parent/context must also fit whole


def handoff(item: Item, seat: str) -> bool:
    return item.kind == 'order' and item.sender == seat and item.recipient == seat and not item.context_only


def render_order(items: tuple[Item, ...], seat: str) -> tuple[Item, ...]:
    """Self-handoffs first; every other admitted item keeps its thread position."""
    return tuple(item for item in items if handoff(item, seat)) + tuple(item for item in items if not handoff(item, seat))


@dataclass(frozen=True)
class Plan:
    opening: str
    files: dict[str, str]
    whole: tuple[str, ...]
    overflow: tuple[str, ...]
    bound: Bound
    directory: Path

    @property
    def required_bytes(self) -> int:
        return size(self.opening) + size(self.files.get('whole.txt', ''))


def _display(text: str, limit: int = 200) -> str:
    # JSON makes controls, newlines and formatting characters visible on one line.
    shown = json.dumps(text, ensure_ascii=True)
    if size(shown) <= limit:
        return shown
    prefix = text[:limit]
    while size(json.dumps(prefix + '...', ensure_ascii=True)) > limit:
        prefix = prefix[:-1]
    return json.dumps(prefix + '...', ensure_ascii=True)


def _chunks(text: str, limit: int):
    """Exact UTF-8 reconstruction, including an oversized line or multibyte character."""
    raw = text.encode('utf-8')
    start = 0
    while start < len(raw):
        end = min(start + limit, len(raw))
        while end < len(raw) and raw[end] & 0xc0 == 0x80:
            end -= 1
        if end == start:
            raise ImpossibleBudget('carry_bytes cannot hold one UTF-8 character')
        yield raw[start:end].decode('utf-8')
        start = end


def build(items: tuple[Item, ...], *, seat: str, header: str, inline: str,
          directory: Path, bound: Bound, force_files: bool = False) -> Plan:
    """Plan every byte before publication; unknown capacity uses the supplied policy.

    Whole-priority handoffs are explicitly self-addressed orders, never self questions
    or answers. Admission, exclusions and thread membership belong to the caller.
    """
    unique = render_order(tuple({item.id: item for item in items}.values()), seat)
    if not force_files and size(inline) <= bound.bytes:
        return Plan(inline, {}, tuple(item.id for item in unique), (), bound, directory)
    files: dict[str, str] = {}

    def put(name: str, text: str) -> str:
        if size(text) > bound.bytes:
            raise ImpossibleBudget('carry_bytes cannot hold a minimal index entry')
        files[name] = text
        return str(directory / name)

    serial = 0

    def index(rows: list[tuple[str, str]], prefix: str) -> str:
        """Bounded pages of complete lines, then bounded range-routing pages.

        The routing keys are full ids (or part ordinals), so a reader can navigate to
        one id without reading all pages. Every level must shrink or it is impossible.
        """
        nonlocal serial
        level = [(key, key, line) for key, line in rows]
        routing = False
        while True:
            pages: list[list[tuple[str, str, str]]] = []
            page: list[tuple[str, str, str]] = []
            used = 0
            for first, last, line in level:
                amount = size(line)
                if amount > bound.bytes:
                    raise ImpossibleBudget('carry_bytes cannot hold a minimal index entry')
                if page and used + amount > bound.bytes:
                    pages.append(page)
                    page, used = [], 0
                page.append((first, last, line))
                used += amount
            if page:
                pages.append(page)
            routes = []
            for page in pages:
                serial += 1
                path = put(f'{prefix}-{serial}.txt', ''.join(line for _, _, line in page))
                first, last = page[0][0], page[-1][1]
                route = json.dumps({'first': first, 'last': last, 'path': path}, ensure_ascii=True) + '\n'
                routes.append((first, last, route))
            if len(routes) == 1:
                return path
            if routing and len(routes) >= len(level):
                raise ImpossibleBudget('carry_bytes cannot hold a shrinking index')
            level = routes
            routing = True

    def opening(whole_count: int, overflow_count: int, root: str) -> str:
        return (f'{header}\nqd > carriage: {whole_count} whole; {overflow_count} overflow; '
                f'{bound.bytes} UTF-8 bytes ({bound.label}).\n'
                f'qd > read {directory / "whole.txt"} whole before acting on its included messages only.\n'
                f'qd > overflow index: {root}; one line per message with id, sender, subject and path. '
                'Read by id as needed, following bounded index pages and ordered read-view parts; '
                'originals stay in place. Overflow need not be read before acting on included work. '
                'An index is navigation, not delivery or proof of reading.\nqd > end of account')

    # A fixed root name keeps the account cost independent of index depth/part count.
    root_reserve = str(directory / 'overflow.txt')
    reserved = size(opening(len(unique), len(unique), root_reserve))
    if reserved > bound.bytes:
        raise ImpossibleBudget('carry_bytes cannot hold the minimal first-message account')
    priority = sorted(unique, key=lambda item: (
        0 if handoff(item, seat)
        else 1 if item.kind == 'order' and not item.context_only
        else 2 if item.kind == 'question' and not item.context_only else 3,
        item.at, item.id))
    chosen: set[str] = set()
    used = reserved
    by_id = {item.id: item for item in unique}
    for item in priority:
        needed, waiting = set(), [item.id]
        while waiting:
            identifier = waiting.pop()
            if identifier in chosen or identifier in needed:
                continue
            needed.add(identifier)
            parent = by_id[identifier]
            if not handoff(parent, seat):
                waiting.extend(ref for ref in parent.requires if ref in by_id)
        amount = sum(size(by_id[identifier].text) for identifier in needed)
        if used + amount <= bound.bytes:
            chosen.update(needed)
            used += amount
    # Handoffs first, then admitted thread order, including explanatory context.
    whole = tuple(item.id for item in unique if item.id in chosen)
    overflow = tuple(item.id for item in unique if item.id not in chosen)
    put('whole.txt', ''.join(item.text for item in unique if item.id in chosen))
    rows = []
    sequence = {item.id: position for position, item in enumerate(unique)}
    for ordinal, item in enumerate(sorted((item for item in unique if item.id not in chosen), key=lambda item: item.id)):
        parts = []
        for number, text in enumerate(_chunks(item.text, bound.bytes)):
            path = put(f'view-{ordinal}-{number}.txt', text)
            key = f'{number:020d}'
            parts.append((key, json.dumps({'part': number + 1, 'path': path}, ensure_ascii=True) + '\n'))
        view = index(parts, 'parts') if parts else put(f'view-{ordinal}-empty.txt', '')
        row = (f'id={json.dumps(item.id)} sender={json.dumps(item.sender)} kind={json.dumps(item.kind)} '
               f'subject={_display(item.subject)} path={json.dumps(view)} '
               f'original={json.dumps(str(item.source)) if item.source is not None else "null"} '
               f'sequence={sequence[item.id]} context_only={str(item.context_only).lower()}\n')
        rows.append((item.id, row))
    root = index(rows, 'messages') if rows else put('messages-empty.txt', '')
    files['overflow.txt'] = files.pop(Path(root).name)
    account = opening(len(whole), len(overflow), root_reserve)
    plan = Plan(account, files, whole, overflow, bound, directory)
    if plan.required_bytes > bound.bytes:
        raise ImpossibleBudget('carry_bytes cannot hold the final account and whole messages')
    return plan


def publish(plan: Plan) -> None:
    """Publish a complete generation by one rename; a failed plan publishes nothing."""
    if plan.required_bytes > plan.bound.bytes or any(size(text) > plan.bound.bytes for text in plan.files.values()):
        raise ImpossibleBudget('carriage plan exceeds its byte bound')
    if not plan.files:
        return
    for name in plan.files:
        if Path(name).name != name or name in ('.', '..'):
            raise ValueError('carriage file name must be a plain basename')
    stage = Path(tempfile.mkdtemp(prefix='.carriage-', dir=plan.directory.parent))
    try:
        for name, text in plan.files.items():
            target = stage / name
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(text.encode('utf-8'))
                stream.flush()
                os.fsync(stream.fileno())
        durable.sync_folder(stage)
        if os.path.lexists(plan.directory):
            raise FileExistsError(plan.directory)
        os.rename(stage, plan.directory)
        durable.sync_folder(plan.directory.parent)
    finally:
        if stage.exists():
            shutil.rmtree(stage)  # only the unpublished temporary made by this call
