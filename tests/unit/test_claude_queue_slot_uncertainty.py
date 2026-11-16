"""Ordinary synthetic queue uncertainty belongs to named slots and ends with them."""

import pytest
from types import SimpleNamespace

from quarterdeck import human_wake, idle
from quarterdeck.harness.types import Join, Screen
from tests.unit.test_claude_completed_queue import end, enqueue, prompt, row, write


TEXT = 'Synthetic queued input oak'
OTHER = 'Synthetic queued input willow'


def uncertain(second=5, text=TEXT):
    return row('queue-operation', second, operation='synthetic-unmeasured', content=text)


def prefix():
    return [prompt(), end(3), enqueue(4, TEXT), uncertain()]


def test_a_standing_named_slot_reports_queue_unknown(tmp_path):
    state = write(tmp_path / 'synthetic.jsonl', prefix())
    assert state.queued == (TEXT,)
    assert state.queue_error == 'unknown queue operation'
    assert idle.check(state).dimension == 'queue-unknown'


@pytest.mark.parametrize('settlement', ['remove', 'dequeue', 'input', 'completed-turn'])
def test_the_slots_own_settlement_leaves_no_record_wide_hold(tmp_path, settlement):
    record = tmp_path / 'synthetic.jsonl'
    assert write(record, prefix()).queue_error == 'unknown queue operation'
    if settlement in ('remove', 'dequeue'):
        later = [row('queue-operation', 6, operation=settlement, content=TEXT)]
    elif settlement == 'input':
        later = [prompt(6, TEXT), end(7)]
    else:
        later = [prompt(6, 'Synthetic later turn'), end(7)]
    state = write(record, later, append=True)
    assert state.queued == () and state.queue_error is None
    assert idle.check(state) is None
    if settlement == 'completed-turn':
        assert TEXT not in state.consumed


def test_unknown_operation_cannot_taint_an_unrelated_slot(tmp_path):
    record = tmp_path / 'synthetic.jsonl'
    write(record, prefix() + [enqueue(6, OTHER)])
    state = write(record, [row('queue-operation', 7, operation='remove', content=TEXT)], append=True)
    assert state.queued == (OTHER,) and state.queue_error is None
    assert idle.check(state).dimension == 'queue-pending'
    state = write(record, [prompt(8), end(9)], append=True)
    assert state.queued == () and state.queue_error is None
    assert idle.check(state) is None


def test_an_unknown_operation_with_no_matching_slot_leaves_no_hold(tmp_path):
    state = write(tmp_path / 'synthetic.jsonl', [prompt(), end(3), uncertain()])
    assert state.queued == () and state.queue_error is None
    assert idle.check(state) is None


def test_only_remaining_unknown_candidates_keep_the_reading_unknown(tmp_path):
    record = tmp_path / 'synthetic.jsonl'
    write(record, [prompt(), end(3), enqueue(4, TEXT), enqueue(5, TEXT), uncertain(6)])
    state = write(record, [row('queue-operation', 7, operation='remove', content=TEXT)], append=True)
    assert state.queued == (TEXT,) and state.queue_error == 'unknown queue operation'
    assert idle.check(state).dimension == 'queue-unknown'
    state = write(record, [row('queue-operation', 8, operation='remove', content=TEXT)], append=True)
    assert state.queued == () and state.queue_error is None
    assert idle.check(state) is None


def test_incremental_completion_matches_cold_and_a_later_input_stays_pending(tmp_path):
    record, cold = tmp_path / 'incremental.jsonl', tmp_path / 'cold.jsonl'
    assert write(record, prefix()).queue_error == 'unknown queue operation'
    later = [prompt(6), end(7), enqueue(8, OTHER)]
    incremental = write(record, later, append=True)
    full = write(cold, prefix() + later)
    assert incremental.queued == full.queued == (OTHER,)
    assert incremental.queue_error == full.queue_error is None
    assert incremental.completed_turns == full.completed_turns
    assert idle.check(incremental).dimension == 'queue-pending'


def test_cleared_slot_releases_the_positive_human_wake_record_consumer(tmp_path, monkeypatch):
    from quarterdeck.harness import claude
    record = tmp_path / 'synthetic.jsonl'
    write(record, prefix())
    seat = SimpleNamespace(launched=lambda: object(), pane=lambda: SimpleNamespace(dead=False, in_mode=False),
                           join=lambda: Join(record), harness=SimpleNamespace(state=claude.state))
    # Metadata preconditions are outside this ordinary record-consumer test.
    monkeypatch.setattr(human_wake, 'candidate_hold', lambda *args: None)
    screen = Screen(text='Synthetic composer', box='', working=False)
    assert human_wake.eligible(seat, screen) == 'human wake held: record activity unknown (queue)'
    write(record, [prompt(6), end(7)], append=True)
    assert human_wake.eligible(seat, screen) is None
