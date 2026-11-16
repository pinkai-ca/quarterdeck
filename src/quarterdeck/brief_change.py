"""A change to a seat's standing orders reaches it with its next message, never on its own.

No notice is typed or delivered for a changed brief: an edit of the ship's orders, a role card or the ship file, or
a sync, wakes no seat. Instead the service keeps, per seat, the brief it last told that seat of
(``runtime/brief-told/<seat>.md``). The next message delivered to the seat for its own sake, through its door or
through its watch, carries at its head one line saying that its standing orders changed, then the changed lines
only, as a unified diff of the brief's text with no context, then one end line. Several changes before that message
fold into one diff, from the brief last told to the current one. Past ``BOUND`` bytes the head names instead the
sections changed and their line counts, and says that ``qd brief`` prints the whole. The message itself is unchanged
and keeps its id: the head is added at delivery, typed with it as one input, and every matcher reads the input past
it (``input_receipt.without_brief_head``).

A launch, a fresh start or a restart loads the current brief, and so sets the brief last told to it. Where an edit
of the ship folder can be attributed to a seat, that seat is not told of its own edit: a commit names its seat by a
``Qd-Seat: <seat>`` trailer (``qd set`` writes one for the seat that runs it), or by the session trailer its program
writes on its own commits, matched to the session that seat's home records (``harness.remote_session``). A commit
that names no seat is no one's own edit, and every seat whose brief it changed is told, its editor included.
"""
from __future__ import annotations

import difflib
from pathlib import Path
import re
import subprocess

from . import durable
from .input_receipt import BRIEF_HEAD, BRIEF_HEAD_END

FOLDER = 'brief-told'
SHIP_HEAD = '.ship-head'  # the ship folder's commit the last attribution read up to
BOUND = 8192  # bytes of diff a head carries; past it, the sections and their line counts
SECTION = re.compile(r'^<!-- (.+?) -->$')


def _path(runtime: Path, name: str) -> Path:
    return Path(runtime) / FOLDER / f'{name}.md'


def published(runtime: Path, name: str) -> str | None:
    """The seat's current brief, as the service published it."""
    try:
        return (Path(runtime) / 'brief' / f'{name}.md').read_text()
    except FileNotFoundError:
        return None


def told(runtime: Path, name: str) -> str | None:
    try:
        return _path(runtime, name).read_text()
    except FileNotFoundError:
        return None


def mark(runtime: Path, name: str, text: str) -> None:
    """The seat now holds ``text``: it loaded it at a launch, or a head told it the change to it."""
    path = _path(runtime, name)
    if told(runtime, name) == text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    durable.replace_file(path, text)


def seed(runtime: Path, name: str, text: str) -> None:
    """A seat with no brief on record holds the one it was last published: from here, what changes is carried."""
    if told(runtime, name) is None:
        mark(runtime, name, text)


def _sections(text: str) -> list[str]:
    """Each line's section, by the brief's own markers (laws, ship's orders, role card, harness card, your seat)."""
    current, out = 'the brief', []
    for line in text.splitlines():
        if match := SECTION.match(line.strip()):
            current = match.group(1)
        out.append(current)
    return out


def head(runtime: Path, name: str) -> tuple[str, str] | None:
    """The head the seat's next message carries, and the brief it tells of; None when nothing changed since the
    brief last told, or when none is on record."""
    before, after = told(runtime, name), published(runtime, name)
    if before is None or after is None or before == after:
        return None
    return compose(name, before, after), after


def compose(name: str, before: str, after: str) -> str:
    old, new = before.splitlines(), after.splitlines()
    first = f'{BRIEF_HEAD}; what changed, then your message (qd brief {name} prints the whole):'
    diff = [line for line in difflib.unified_diff(old, new, lineterm='', n=0)
            if not line.startswith(('--- ', '+++ '))]
    body = '\n'.join(diff)
    if len(body.encode()) > BOUND:
        counts: dict[str, list[int]] = {}
        sections_old, sections_new = _sections(before), _sections(after)
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
            if tag == 'equal':
                continue
            for i in range(i1, i2):
                counts.setdefault(sections_old[i], [0, 0])[1] += 1
            for j in range(j1, j2):
                counts.setdefault(sections_new[j], [0, 0])[0] += 1
        lines = [f'  {section}: {added} line{"s" if added != 1 else ""} added, {removed} removed'
                 for section, (added, removed) in counts.items()]
        body = '\n'.join(lines + [f'  too long to show here: qd brief {name} prints the whole'])
    return f'{first}\n{body}\n{BRIEF_HEAD_END}\n'


# -- the editor is not told of its own edit ----------------------------------------------------------------------

def _git(folder: Path, *args: str) -> str | None:
    done = subprocess.run(['git', '-C', str(folder), *args], capture_output=True, text=True)
    return done.stdout if done.returncode == 0 else None


def editor(folder: Path, runtime: Path) -> str | None:
    """The one seat that made every commit of the ship folder since the last reading, where every one names it;
    else None. The reading advances to the folder's current commit either way."""
    marker = Path(runtime) / FOLDER / SHIP_HEAD
    now = (_git(folder, 'rev-parse', 'HEAD') or '').strip()
    try:
        last = marker.read_text().strip()
    except FileNotFoundError:
        last = ''
    if now:
        marker.parent.mkdir(parents=True, exist_ok=True)
        durable.replace_file(marker, now + '\n')
    if not now or not last or last == now:
        return None
    log = _git(folder, 'log', '--format=%x00%B', f'{last}..{now}')
    if not log:
        return None
    from .harness import remote_session
    known, seats = None, set()
    for message in log.split('\x00')[1:]:
        named = re.findall(r'(?m)^Qd-Seat: ([a-z0-9][a-z0-9-]*)\s*$', message)
        if not named and remote_session.TRAILER.search(message):
            known = remote_session.sessions(runtime) if known is None else known
            named = remote_session.seats_named(message, runtime, known)
        if len(set(named)) != 1:
            return None  # a commit that names no seat, or more than one, is no one seat's own edit
        seats.update(named)
    return seats.pop() if len(seats) == 1 else None
