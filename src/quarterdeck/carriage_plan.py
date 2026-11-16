"""Policy-bounded thread indexes and a fresh seat's complete self-handoff.

Only the handoff may be copied in full. An oversized handoff is one immutable file,
explicitly past the ship policy; first-message and index-page bytes remain bounded.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path

from . import durable
from .post_threads import IndexRow


def size(text: str | None) -> int:
    return len(text.encode('utf-8')) if text else 0


def _line(data: dict) -> str:
    """Readable Unicode, with every control/format/line-separator escaped as JSON."""
    text = json.dumps(data, ensure_ascii=False)
    return ''.join(json.dumps(char, ensure_ascii=True)[1:-1]
                   if unicodedata.category(char) in ('Cc', 'Cf', 'Cs', 'Zl', 'Zp') else char
                   for char in text) + '\n'


class ImpossibleBudget(ValueError):
    """The minimum first-message account or index entry cannot fit; publish nothing."""


@dataclass(frozen=True)
class Plan:
    opening: str | None
    files: dict[str, str]
    directory: Path
    limit: int

    @property
    def index_path(self) -> Path | None:
        return self.directory / 'index.txt' if 'index.txt' in self.files else None

    @property
    def handoff_path(self) -> Path | None:
        return self.directory / 'handoff.txt' if 'handoff.txt' in self.files else None


def build(rows: tuple[IndexRow, ...], *, handoff: str, header: str, directory: Path,
          limit: int, inline_limit: int = 32768, force_files: bool = False, history: str = '', idle: str = '') -> Plan:
    if type(limit) is not int or limit <= 0:
        raise ValueError('carry_bytes must be a positive integer')
    if not rows and not handoff and not history:
        return Plan(None, {}, directory, limit)  # an empty index adds no model turn; the native brief still loads
    budget = min(limit, inline_limit)
    files: dict[str, str] = {}
    serial = 0

    def put(name: str, text: str) -> str:
        if size(text) > limit:
            raise ImpossibleBudget('carry_bytes cannot hold a minimal index entry')
        files[name] = text
        return str(directory / name)

    def pages(rows: list[tuple[str, str]], prefix: str) -> str:
        nonlocal serial
        if not rows:
            return put(prefix + '-empty.txt', '')
        level = [(key, key, line) for key, line in rows]
        routing = False
        while True:
            packed, page, used = [], [], 0
            for first, last, line in level:
                amount = size(line)
                if amount > limit:
                    raise ImpossibleBudget('carry_bytes cannot hold a minimal index entry')
                if page and used + amount > limit:
                    packed.append(page)
                    page, used = [], 0
                page.append((first, last, line))
                used += amount
            if page:
                packed.append(page)
            routes = []
            for page in packed:
                serial += 1
                path = put(f'{prefix}-{serial}.txt', ''.join(line for _, _, line in page))
                first, last = page[0][0], page[-1][1]
                routes.append((first, last, json.dumps({'first': first, 'last': last, 'path': path}) + '\n'))
            if len(routes) == 1:
                return path
            if routing and len(routes) >= len(level):
                raise ImpossibleBudget('carry_bytes cannot hold a shrinking index')
            level, routing = routes, True

    lines = []
    for row in rows:
        data = asdict(row)
        if not data.get('settled'):
            data.pop('settled', None)  # keep ordinary index rows unchanged
        data['thread'] = f'qd thread {row.id}'
        if row.subject and size(json.dumps(row.subject)) > 200:
            prefix = row.subject[:200]
            while size(json.dumps(prefix + '...')) > 200:
                prefix = prefix[:-1]
            data['subject'] = prefix + '...'
            data['subject_truncated'] = True
        line = _line(data)
        if size(line) > limit:
            # The one thread line still names every long id list through bounded pages.
            for key in ('amendments', 'active', 'awaiting', 'withheld', 'settled'):
                values = data.get(key, ())
                if values:
                    path = pages([(f'{n:020d}', json.dumps({'value': value}) + '\n')
                                  for n, value in enumerate(values)], key)
                    data[key] = {'count': len(values), 'index': path}
            line = _line(data)
        if size(line) > limit:
            raise ImpossibleBudget('carry_bytes cannot hold a minimal thread-index entry')
        lines.append((row.id, line))

    instructions = (f'qd > open threads: {len(rows)}; carry_bytes={limit} (policy bound)\n'
                    'qd > before acting on any message, run qd thread ID and read that thread whole. '
                    'Sent, awaiting threads hold no duty. Every message stays a file.\n')
    inline_handoff = f'qd > your handoff, in full; read it first\n{handoff}\n' if handoff else ''
    prefix = header + '\n' + (history + '\n' if history else '')
    ending = idle + '\n' if idle and not rows else ''  # nothing open: the opening ends by saying so, plainly (a hand's)
    inline = prefix + inline_handoff + instructions + ''.join(line for _, line in lines) + ending
    if not force_files and not files and size(inline) <= budget:
        return Plan(inline, {}, directory, limit)

    root = pages(sorted(lines), 'threads')
    files['index.txt'] = files.pop(Path(root).name)
    index_account = instructions + f'qd > thread index: {directory / "index.txt"}; follow bounded pages by id as needed\n'
    opening = prefix + inline_handoff + index_account
    if handoff and (force_files or size(opening) > budget):
        files['handoff.txt'] = handoff
        warning = (f'qd > your handoff is {size(handoff)} bytes, over carry_bytes; write a shorter one next time\n'
                   if size(handoff) > limit else '')
        opening = (prefix + f'qd > read {directory / "handoff.txt"} whole first, before acting; it is your handoff\n'
                   + warning + index_account)
    opening += ending  # the same last line whether the index is inline or in files
    if size(opening) > budget:
        raise ImpossibleBudget('carry_bytes cannot hold the minimal thread-index account')
    return Plan(opening, files, directory, limit)


def publish(plan: Plan) -> None:
    """Atomically publish one complete private generation; never rewrite an earlier one."""
    if size(plan.opening) > plan.limit or any(size(text) > plan.limit for name, text in plan.files.items() if name != 'handoff.txt'):
        raise ImpossibleBudget('thread index exceeds carry_bytes')
    if not plan.files:
        return
    for name in plan.files:
        if Path(name).name != name or name in ('.', '..'):
            raise ValueError('carriage file name must be a plain basename')
    stage = Path(tempfile.mkdtemp(prefix='.carriage-', dir=plan.directory.parent))
    try:
        for name, text in plan.files.items():
            descriptor = os.open(stage / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(text.encode('utf-8'))
                stream.flush()
        if os.path.lexists(plan.directory):
            raise FileExistsError(plan.directory)
        os.rename(stage, plan.directory)
    finally:
        if stage.exists():
            shutil.rmtree(stage)  # only the unpublished temporary made by this call
