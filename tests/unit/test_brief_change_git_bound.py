"""The brief change's git is bounded inside the service: a git that does not answer leaves the change unknown and
kept for the next pass, never lost and never raised out of the service. A stand-in git on PATH, in the test's own
folder, hangs with a child of its own; the bound ends both."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import time

import pytest

from quarterdeck import brief_change
from .test_brief_change_carried import commit, git, repository  # noqa: F401
from .test_brief_render_stamp import case  # noqa: F401

STAND_IN = '''#!/bin/sh
# A stand-in git: it answers as the real one, except that it hangs on the subcommand the test names, with a child
# of its own holding its output open, as a stuck credential helper or fsmonitor would.
for argument in "$@"; do
  if [ "$argument" = "$SYNTHETIC_HANG_ON" ]; then
    sleep 600 &
    echo $! >> "$SYNTHETIC_PIDS"
    echo $$ >> "$SYNTHETIC_PIDS"
    wait
    exit 0
  fi
done
exec "$SYNTHETIC_REAL_GIT" "$@"
'''


def alive(pid):
    try:
        state = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return False
    return state != 'Z'


@pytest.fixture
def hanging(tmp_path, monkeypatch):
    real = shutil.which('git')
    folder = tmp_path / 'stand-in'
    folder.mkdir()
    script = folder / 'git'
    script.write_text(STAND_IN)
    script.chmod(0o755)
    pids = tmp_path / 'stand-in.pids'
    pids.touch()
    monkeypatch.setenv('SYNTHETIC_REAL_GIT', real)
    monkeypatch.setenv('SYNTHETIC_PIDS', str(pids))
    monkeypatch.setenv('PATH', f'{folder}{os.pathsep}{os.environ["PATH"]}')
    monkeypatch.setattr(brief_change, 'GIT_WAIT', 0.5)

    def hang_on(subcommand):
        monkeypatch.setenv('SYNTHETIC_HANG_ON', subcommand)

    def started():
        return [int(line) for line in pids.read_text().split()]

    yield hang_on, started
    for pid in started():  # the test ends whatever the bound did not
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass


def ended(pids, within=5.0):
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not any(alive(pid) for pid in pids):
            return True
        time.sleep(0.05)
    return False


@pytest.mark.parametrize('subcommand', ['rev-parse', 'log'])
def test_a_git_that_does_not_answer_leaves_the_editor_unknown_and_the_reading_where_it_was(
        repository, hanging, monkeypatch, subcommand):  # noqa: F811
    folder, runtime = repository
    hang_on, started = hanging
    marker = runtime / brief_change.FOLDER / brief_change.SHIP_HEAD
    before = marker.read_text()
    commit(folder, 'Orders, edited.\n', 'qd set seats.x.model = "y"\n\nQd-Seat: editor-seat')
    hang_on(subcommand)
    began = time.monotonic()
    with pytest.raises(brief_change.Unread) as raised:
        brief_change.editor(folder, runtime)
    assert time.monotonic() - began < 5, 'the bound, not the hang, ends the reading'
    assert subcommand in str(raised.value) and '0.5 s' in str(raised.value)
    assert marker.read_text() == before, 'the reading does not advance past what it could not read'
    assert started() and ended(started()), 'the stand-in and its own child are ended by the bound'
    monkeypatch.delenv('SYNTHETIC_HANG_ON')
    assert brief_change.editor(folder, runtime) == 'editor-seat', 'the next pass reads the same change'


def ship_repository(case, monkeypatch):
    """The case's ship folder as a git repository whose reading starts at its first commit."""
    folder, runtime = case.service.ship.folder, case.service.runtime
    git(folder, 'init', '-q')
    commit(folder, 'Synthetic orders\n', 'first')
    brief_change.editor(folder, runtime)
    return folder, runtime


def edit_by_the_seat(case):
    """The seat's own commit changes its brief: by trailer, it is that seat's own edit."""
    folder = case.service.ship.folder
    commit(folder, 'Synthetic orders, amended\n', f'qd set orders\n\nQd-Seat: {case.spec.name}')


def test_an_answered_git_does_not_tell_the_editor_of_its_own_edit(case, monkeypatch):  # noqa: F811
    ship_repository(case, monkeypatch)
    edit_by_the_seat(case)
    case.service.rebrief()
    assert 'Synthetic orders, amended' in case.path.read_text()
    assert brief_change.head(case.service.runtime, case.spec.name) is None


def test_a_hung_git_still_tells_the_editor_with_its_next_message(case, hanging, monkeypatch):  # noqa: F811
    service = case.service
    folder, runtime = ship_repository(case, monkeypatch)
    marker = runtime / brief_change.FOLDER / brief_change.SHIP_HEAD
    reading = marker.read_text()
    edit_by_the_seat(case)
    hang_on, started = hanging
    hang_on('log')
    service.rebrief()  # never raised out of the service
    assert 'Synthetic orders, amended' in case.path.read_text(), 'published, not held back'
    composed = brief_change.head(runtime, case.spec.name)
    assert composed is not None and '+Synthetic orders, amended' in composed[0], 'its next message carries the head'
    assert marker.read_text() == reading, 'the next pass reads the same commits again'
    assert [kwargs['step'] for args, kwargs in case.logs if args == ('error',)] == ['brief-change']
    # Told once: the head marks the told brief, and a later reading of those commits adds no head.
    brief_change.mark(runtime, case.spec.name, composed[1])
    monkeypatch.delenv('SYNTHETIC_HANG_ON')
    monkeypatch.setattr(brief_change, 'UNREAD_RETRY', 0.0)
    service.rebrief()
    assert brief_change.head(runtime, case.spec.name) is None, 'a seat told is not told the same change twice'
    assert marker.read_text() != reading and brief_change.unread(runtime) is None


def test_the_status_line_names_a_git_unanswered_past_three_passes_and_clears_when_it_answers(
        case, hanging, monkeypatch):  # noqa: F811
    from quarterdeck import watch
    service = case.service
    folder, runtime = ship_repository(case, monkeypatch)
    edit_by_the_seat(case)
    hang_on, _ = hanging
    hang_on('rev-parse')
    monkeypatch.setattr(brief_change, 'UNREAD_RETRY', 0.0)
    lines = []
    for _ in range(3):
        service.rebrief()
        lines.append(brief_change.status(runtime))
    assert lines[0] == lines[1] == [], 'not before the third failing pass'
    [named] = lines[2]
    assert named.startswith("ship folder's git has not answered for 3 passes (git rev-parse did not answer within")
    facts = watch.Facts('up', 0, 0, 0, brief_git=brief_change.status(runtime))
    assert named in facts.brief_git and '1 brief git alerts' in facts.line()
    assert len([1 for args, _ in case.logs if args == ('error',)]) == 1, 'the same failure is logged once'
    monkeypatch.delenv('SYNTHETIC_HANG_ON')
    service.rebrief()
    assert brief_change.status(runtime) == [] and brief_change.unread(runtime) is None


def test_a_git_still_unanswered_is_asked_at_most_once_a_minute(case, hanging, monkeypatch):  # noqa: F811
    service = case.service
    folder, runtime = ship_repository(case, monkeypatch)
    edit_by_the_seat(case)
    hang_on, _ = hanging
    hang_on('rev-parse')
    service.rebrief()
    for _ in range(5):
        service.rebrief()  # the brief is published: these passes only ask again, and not within the minute
    assert brief_change.unread(runtime)['passes'] == 1
