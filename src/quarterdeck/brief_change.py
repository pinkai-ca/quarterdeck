"""A change to a seat's standing orders reaches it with its next message, never on its own.

No notice is typed or delivered for a changed brief: an edit of the ship's orders, a role card or the ship file, or
a sync, wakes no seat. Instead the service keeps, per seat, the brief it last told that seat of
(``runtime/brief-told/<seat>.md``). The next message delivered to the seat for its own sake, through its door or
through its watch, carries at its head one line saying that its standing orders changed, then the changed lines
only, as a unified diff of the brief's text with no context, then one end line. Several changes before that message
fold into one diff, from the brief last told to the current one. Past ``BOUND`` bytes the head names instead the
sections changed and their line counts, and says that ``qd brief`` prints the whole. The message itself is unchanged
and keeps its id: the head is added at delivery, typed with it as one input, and every matcher reads the input past
it (``input_receipt.without_brief_head``). A hand takes its current brief at fresh launch and receives no new head.

A launch, a fresh start or a restart loads the current brief, and so sets the brief last told to it. Where an edit
of the ship folder can be attributed to a seat, that seat is not told of its own edit: a commit names its seat by a
``Qd-Seat: <seat>`` trailer (``qd set`` writes one for the seat that runs it), or by the session trailer its program
writes on its own commits, matched to the session that seat's home records (``harness.remote_session``). A commit
that names no seat is no one's own edit, and every seat whose brief it changed is told, its editor included.
"""
from __future__ import annotations

import difflib
import json
import os
from pathlib import Path
import re
import signal
import subprocess

from . import durable
from .input_receipt import BRIEF_HEAD, BRIEF_HEAD_END
from .tmux import PRIVATE_COMMAND_WAIT

FOLDER = 'brief-told'
SHIP_HEAD = '.ship-head'  # the ship folder's commit the last attribution read up to
BOUND = 8192  # bytes of diff a head carries; past it, the sections and their line counts
SECTION = re.compile(r'^<!-- (.+?) -->$')
GIT_WAIT = PRIVATE_COMMAND_WAIT  # the service's bound on a command it waits for: tmux's, for a private server
GIT_UNREAD = '.git-unread.json'  # beside SHIP_HEAD: the passes the ship folder's git has not answered, until it does
UNREAD_ALERT = 3  # failing passes after which the status line names it (law: nothing-waits-silently)
UNREAD_RETRY = 60.0  # seconds between asks while git does not answer: a hung git costs a pass GIT_WAIT at most



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


def forget(runtime: Path, name: str) -> None:
    """Remove the last-told brief of a retired seat."""
    path = _path(runtime, name)
    try:
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.unlink(path.name, dir_fd=directory)
        finally:
            os.close(directory)
    except FileNotFoundError:
        pass


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


def carried(text: str) -> str:
    """The standing-orders head a typed text carries at its start, or ''."""
    from .input_receipt import without_brief_head
    rest = without_brief_head(text)
    if rest is text:
        return ''
    stripped = text.lstrip()
    return stripped[:len(stripped) - len(rest)]


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

class Unread(Exception):
    """The ship folder's git did not answer within its bound, or could not be run: who made the pending change is
    unknown, and the change waits for the next pass."""


def _git(folder: Path, *args: str) -> str | None:
    """git's output, None when it answers with an error; Unread when it does not answer within GIT_WAIT. It runs in
    a process group of its own, so a hung git and anything it started (a helper holding its output) end together."""
    try:
        process = subprocess.Popen(['git', '-C', str(folder), *args], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
    except OSError as error:
        raise Unread(f'git {args[0]} could not be run ({type(error).__name__})') from None
    with process:
        try:
            out, _ = process.communicate(timeout=GIT_WAIT)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise Unread(f'git {args[0]} did not answer within {GIT_WAIT:g} s') from None
    return out if process.returncode == 0 else None


def unread(runtime: Path) -> dict | None:
    """The record of a git that has not answered: its failing passes, its last error and when it last asked."""
    try:
        data = json.loads((Path(runtime) / FOLDER / GIT_UNREAD).read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return dict(passes=UNREAD_ALERT, error='its record unreadable', at=0.0)
    return data if isinstance(data, dict) else dict(passes=UNREAD_ALERT, error='its record unreadable', at=0.0)


def note_unread(runtime: Path, error: str, at: float) -> int:
    """One more pass that git did not answer; the count so far."""
    passes = ((unread(runtime) or {}).get('passes') or 0) + 1
    path = Path(runtime) / FOLDER / GIT_UNREAD
    path.parent.mkdir(parents=True, exist_ok=True)
    durable.replace_file(path, json.dumps(dict(passes=passes, error=error, at=at)) + '\n')
    return passes


def answered(runtime: Path) -> None:
    """git answered: whatever it had not answered is cleared."""
    (Path(runtime) / FOLDER / GIT_UNREAD).unlink(missing_ok=True)


def status(runtime: Path) -> list[str]:
    """The status line's words for a ship-folder git that has not answered past UNREAD_ALERT passes."""
    data = unread(runtime)
    if data is None or not isinstance(data.get('passes'), int) or data['passes'] < UNREAD_ALERT:
        return []
    return [f"ship folder's git has not answered for {data['passes']} passes ({data.get('error')}); "
            "rule changes are told to every seat they change, its editor included"]


def editor(folder: Path, runtime: Path) -> str | None:
    """The one seat that made every commit of the ship folder since the last reading, where every one names it;
    else None. The reading advances to the folder's current commit once that is read; a git that does not answer
    raises Unread and leaves the reading where it was, so the next pass reads the same change."""
    marker = Path(runtime) / FOLDER / SHIP_HEAD
    now = (_git(folder, 'rev-parse', 'HEAD') or '').strip()
    try:
        last = marker.read_text().strip()
    except FileNotFoundError:
        last = ''
    log = _git(folder, 'log', '--format=%x00%B', f'{last}..{now}') if now and last and last != now else None
    if now:
        marker.parent.mkdir(parents=True, exist_ok=True)
        durable.replace_file(marker, now + '\n')
    if not now or not last or last == now:
        return None
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
