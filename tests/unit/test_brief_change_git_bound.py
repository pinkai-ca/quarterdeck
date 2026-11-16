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


def test_the_service_keeps_an_unread_change_for_its_next_pass(case, monkeypatch):  # noqa: F811
    service = case.service
    monkeypatch.setattr(type(service.ship), 'is_repository', property(lambda ship: True))
    case.framework.joinpath('laws.md').write_text('Synthetic laws, amended\n')
    stamp = service.brief_stamp

    def unread(folder, runtime):
        raise brief_change.Unread('git log did not answer within 0.5 s')

    monkeypatch.setattr(brief_change, 'editor', unread)
    service.rebrief()  # never raised out of the service
    assert service.brief_stamp == stamp and case.writes == [], 'nothing published, nothing lost'
    assert [kwargs for args, kwargs in case.logs if args == ('error',)] == [
        dict(step='brief-change', error='git log did not answer within 0.5 s')]
    service.rebrief()
    assert len([1 for args, _ in case.logs if args == ('error',)]) == 1, 'the same failure is logged once'
    monkeypatch.setattr(brief_change, 'editor', lambda folder, runtime: None)
    service.rebrief()
    assert 'Synthetic laws, amended' in case.path.read_text() and service.brief_stamp != stamp
