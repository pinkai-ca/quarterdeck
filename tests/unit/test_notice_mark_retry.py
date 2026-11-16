"""A record read inside a time-bound unknown is accounted once that unknown has passed, with no new row.

The accounting pass marks a record whose turn state it read as unknown, and does not read it again while its bytes
stand still. Claude's notice window is unknown only for a grace on the reader's clock: once it has passed, the
same bytes read idle, so the mark is read again then, checkpointed and its turns counted. Every other mark keeps
the ordinary rule. Every clock here is injected; nothing waits on wall time.
"""

from datetime import timedelta
import json

import pytest

from quarterdeck import record_owner
from quarterdeck.harness import claude, held_reading
from quarterdeck.harness.base import State
from tests.unit.test_ledger_pass_boundaries import world
from tests.unit.test_resume_notice_ordinary import AT, completed_background_turn, notice, row


GRACE = claude.NOTICE_GRACE_SECONDS
NOTICE_SECOND = 5  # notice() writes its row at AT + 5 s


def at_clock(monkeypatch, seconds):
    monkeypatch.setattr(claude._Reader, 'clock', staticmethod(lambda: AT + timedelta(seconds=seconds)))


def claude_world(tmp_path, monkeypatch, rows):
    """The accounting world, its one record a real Claude record read by Claude's own reader."""
    w = world(tmp_path, monkeypatch, records=('synthetic-claude-q.jsonl',))
    path = w.paths[0]
    path.write_text(''.join(json.dumps(entry) + '\n' for entry in rows))
    harness = claude.ClaudeHarness()
    reads = []
    original = harness.read

    def read(record):
        reads.append(record)
        return original(record)

    monkeypatch.setattr(harness, 'read', read)
    w.owned[path] = record_owner.Owned(path, 'claude', harness)
    w.claude_reads = reads
    return w, path


def ledger_state(w, path):
    db = w.svc.ledger.db
    mark = db.execute('SELECT error FROM records WHERE record=?', (str(path),)).fetchone()
    checkpoint = db.execute('SELECT 1 FROM record_accounting WHERE record=?', (str(path),)).fetchone()
    turns = db.execute('SELECT COUNT(*) FROM turns WHERE record=?', (str(path),)).fetchone()[0]
    return (mark[0] if mark else None), checkpoint is not None, turns


def test_a_fresh_notice_is_marked_and_read_again_once_its_grace_has_passed(tmp_path, monkeypatch):
    w, path = claude_world(tmp_path, monkeypatch, completed_background_turn() + [notice()])
    at_clock(monkeypatch, NOTICE_SECOND + 1)
    w.svc.account_records()
    assert ledger_state(w, path) == ('incomplete turn state in record', False, 1)
    assert held_reading.unknown_until(w.owned[path]).until == AT + timedelta(seconds=NOTICE_SECOND + GRACE)
    assert len(w.claude_reads) == 1
    # Still inside the grace: the mark stands and the unchanged record is not read again.
    at_clock(monkeypatch, NOTICE_SECOND + GRACE - 0.001)
    w.svc.account_records()
    assert len(w.claude_reads) == 1
    assert ledger_state(w, path) == ('incomplete turn state in record', False, 1)
    # The grace has passed on the reader's clock, the bytes unchanged: the next pass reads it, checkpoints it and
    # counts its turns.
    at_clock(monkeypatch, NOTICE_SECOND + GRACE)
    w.svc.account_records()
    assert len(w.claude_reads) == 2
    assert ledger_state(w, path) == (None, True, 1)
    assert w.svc.read_holds == {}
    # Accounted: later passes neither read it nor hold it.
    at_clock(monkeypatch, 3600)
    w.svc.account_records()
    assert len(w.claude_reads) == 2


def test_the_grace_ending_between_the_two_reads_leaves_a_due_hold(tmp_path, monkeypatch):
    """The pass reads the record inside the window and marks it; the grace ends before the hold is asked for.

    The second reading finds the window already passed. It still returns the hold, due at once, so the next pass
    reads the unchanged record again rather than leaving the mark until the bytes change.
    """
    w, path = claude_world(tmp_path, monkeypatch, completed_background_turn() + [notice()])
    at_clock(monkeypatch, NOTICE_SECOND + GRACE - 0.001)
    original = held_reading.unknown_until

    def late(owned):
        at_clock(monkeypatch, NOTICE_SECOND + GRACE)  # the grace ends between the pass's read and this one
        return original(owned)

    monkeypatch.setattr(held_reading, 'unknown_until', late)
    w.svc.account_records()
    assert ledger_state(w, path) == ('incomplete turn state in record', False, 1)
    hold = w.svc.read_holds[path]
    assert hold.until == AT + timedelta(seconds=NOTICE_SECOND + GRACE) and held_reading.due(hold)
    monkeypatch.setattr(held_reading, 'unknown_until', original)
    w.svc.account_records()
    assert len(w.claude_reads) == 2
    assert ledger_state(w, path) == (None, True, 1)
    assert w.svc.read_holds == {}
    at_clock(monkeypatch, 3600)
    w.svc.account_records()
    assert len(w.claude_reads) == 2


@pytest.mark.parametrize('seconds', [GRACE - 1, GRACE + 1])
def test_a_window_passed_with_a_reply_or_never_opened_gives_no_hold(tmp_path, monkeypatch, seconds):
    """Only the notice window holds: a record whose notice was answered reads known, and asks for no hold."""
    reply = dict(role='assistant', id='message-oak', model='synthetic-model', stop_reason='end_turn',
                 content=[dict(type='text', text='synthetic notice response')],
                 usage=dict(input_tokens=7, output_tokens=3))
    w, path = claude_world(tmp_path, monkeypatch, completed_background_turn() + [notice()]
                           + [row('assistant', 6, message=reply), row('system', 8, subtype='turn_duration')])
    at_clock(monkeypatch, NOTICE_SECOND + seconds)
    assert held_reading.unknown_until(w.owned[path]) is None


def test_a_reply_inside_the_grace_moves_the_mark_by_its_bytes_as_before(tmp_path, monkeypatch):
    w, path = claude_world(tmp_path, monkeypatch, completed_background_turn() + [notice()])
    at_clock(monkeypatch, NOTICE_SECOND + 1)
    w.svc.account_records()
    assert ledger_state(w, path) == ('incomplete turn state in record', False, 1)
    reply = dict(role='assistant', id='message-elm', model='synthetic-model', stop_reason='end_turn',
                 content=[dict(type='text', text='synthetic notice response')],
                 usage=dict(input_tokens=7, output_tokens=3))
    with path.open('a') as stream:
        stream.write(json.dumps(row('assistant', 6, message=reply)) + '\n')
        stream.write(json.dumps(row('system', 8, subtype='turn_duration')) + '\n')
    at_clock(monkeypatch, 9)
    w.svc.account_records()
    assert len(w.claude_reads) == 2
    assert ledger_state(w, path) == (None, True, 2)
    at_clock(monkeypatch, 3600)
    w.svc.account_records()
    assert len(w.claude_reads) == 2


def test_an_untimed_notice_keeps_the_ordinary_rule(tmp_path, monkeypatch):
    """A notice whose time cannot be read is unknown until a row follows it, so its mark is not retried."""
    untimed = {key: value for key, value in notice().items() if key != 'timestamp'}
    w, path = claude_world(tmp_path, monkeypatch, completed_background_turn() + [untimed])
    at_clock(monkeypatch, NOTICE_SECOND + 1)
    w.svc.account_records()
    assert ledger_state(w, path) == ('incomplete turn state in record', False, 1)
    assert held_reading.unknown_until(w.owned[path]) is None
    at_clock(monkeypatch, 3600)
    w.svc.account_records()
    assert len(w.claude_reads) == 1
    assert ledger_state(w, path) == ('incomplete turn state in record', False, 1)


@pytest.mark.parametrize('unknown', [dict(turn_open=None), dict(turns=None)])
def test_a_mark_for_any_other_reason_is_not_retried(tmp_path, monkeypatch, unknown):
    w = world(tmp_path, monkeypatch)
    path = w.paths[0]
    w.sources[path] = (State(**{**dict(turns=1, turn_open=False), **unknown}), w.sources[path][1])
    w.svc.account_records()
    assert ledger_state(w, path)[0] == 'incomplete turn state in record'
    assert held_reading.unknown_until(w.owned[path]) is None
    at_clock(monkeypatch, 3600)
    w.clock.now += 3600  # the service's own retry clock moves too
    w.svc.account_records()
    assert w.reads == [path]


def test_a_hold_is_judged_on_the_readers_clock_when_asked(monkeypatch):
    hold = held_reading.Hold(AT + timedelta(seconds=GRACE), claude._Reader)
    at_clock(monkeypatch, GRACE - 1)
    assert not held_reading.due(hold)
    at_clock(monkeypatch, GRACE)
    assert held_reading.due(hold)
    assert not held_reading.due(None)
