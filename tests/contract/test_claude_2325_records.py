"""Minimal record boundaries captured with throwaway Claude Code 2.1.325 sessions."""

import json
from pathlib import Path

import pytest

from quarterdeck import idle
from quarterdeck.harness.claude import ClaudeHarness

from tests.conftest import FIXTURES

RECORDS = FIXTURES / 'claude' / '2.1.325'


def copy_record(tmp_path: Path, name: str) -> Path:
    path = tmp_path / 'record.jsonl'
    path.write_bytes((RECORDS / (name + '.jsonl')).read_bytes())
    return path


@pytest.mark.parametrize('name,turns,failed', [
    ('plain-turn', 1, []),
    ('tool-turn', 2, [False]),
    ('refused-tool', 3, [False, True]),
    ('interrupted-turn', 4, [False, True, True]),
])
def test_captured_turn_tool_refusal_and_interrupt_end_idle(tmp_path, name, turns, failed):
    record = copy_record(tmp_path, name)
    reader = ClaudeHarness()
    state = reader.state(record)
    receipts = reader.inputs(record)
    assert state.turn_open is False
    assert state.queued == () and state.pending_tools == ()
    assert len(state.completed_turns) == turns
    assert [tool.failed for turn in state.completed_turns for tool in turn.tools] == failed
    assert not receipts.skipped
    assert idle.check(state) is None


def test_an_interrupt_is_required_to_close_the_captured_open_turn(tmp_path):
    record = copy_record(tmp_path, 'interrupted-turn')
    rows = record.read_text().splitlines()
    record.write_text('\n'.join(rows[:-1]) + '\n')
    reader = ClaudeHarness()
    assert reader.state(record).turn_open is True
    assert idle.check(reader.state(record)).dimension == 'turn-open'
    with record.open('a') as stream:
        stream.write(rows[-1] + '\n')
    assert reader.state(record).turn_open is False
    assert idle.check(reader.state(record)) is None


@pytest.mark.parametrize('kind', ['bridge-session', 'cost-state'])
def test_observed_non_input_metadata_does_not_add_work_or_receipts(tmp_path, kind):
    record = copy_record(tmp_path, 'plain-turn')
    rows = [json.loads(line) for line in (RECORDS / 'activity-metadata.jsonl').read_text().splitlines()]
    captured = next(row for row in rows if row['type'] == kind)
    reader = ClaudeHarness()
    before = reader.inputs(record)
    with record.open('a') as stream:
        stream.write(json.dumps(captured) + '\n')
    after = reader.inputs(record)
    state = reader.state(record)
    assert [(item.text, item.at, item.kind) for item in after] == [(item.text, item.at, item.kind) for item in before]
    assert not after.skipped
    assert len(state.completed_turns) == 1 and idle.check(state) is None


def test_every_observed_metadata_kind_is_readable_but_never_a_receipt(tmp_path):
    record = copy_record(tmp_path, 'plain-turn')
    with record.open('a') as stream:
        stream.write((RECORDS / 'activity-metadata.jsonl').read_text())
    reader = ClaudeHarness()
    assert [item.text for item in reader.inputs(record)] == ['Reply OK.']
    assert not reader.inputs(record).skipped
    assert idle.check(reader.state(record)) is None


def test_identity_free_dequeue_waits_for_the_captured_user_echo(tmp_path):
    record = copy_record(tmp_path, 'plain-turn')
    queued = (RECORDS / 'queued-prompt.jsonl').read_text().splitlines()
    assert json.loads(queued[0])['operation'] == 'enqueue'
    assert json.loads(queued[1])['operation'] == 'dequeue' and 'content' not in json.loads(queued[1])
    reader = ClaudeHarness()
    with record.open('a') as stream:
        stream.write('\n'.join(queued[:2]) + '\n')
    assert reader.state(record).queued == ('Reply QUEUED.',)
    assert idle.check(reader.state(record)).dimension == 'queue-pending'
    with record.open('a') as stream:
        stream.write('\n'.join(queued[2:]) + '\n')
    state = reader.state(record)
    assert state.queued == () and idle.check(state) is None
    assert not reader.inputs(record).skipped
    assert [(item.text, item.kind) for item in reader.inputs(record)] == [
        ('Reply OK.', 'accepted'), ('Reply QUEUED.', 'consumed')]


def test_an_unmeasured_row_stays_unknown_after_the_known_newer_metadata(tmp_path):
    record = copy_record(tmp_path, 'plain-turn')
    with record.open('a') as stream:
        stream.write((RECORDS / 'activity-metadata.jsonl').read_text())
        stream.write(json.dumps({'type': 'future-unmeasured-row'}) + '\n')
    reader = ClaudeHarness()
    state = reader.state(record)
    assert state.pending_tools is None
    assert idle.check(state).dimension == 'tools-unknown'
    assert any(item['reason'] == 'unknown row kind' for item in reader.inputs(record).skipped)


def artifact_rows():
    return [json.loads(line) for line in (RECORDS / 'artifact-bookkeeping.jsonl').read_text().splitlines()]


def test_artifact_bookkeeping_is_neutral_in_both_native_positions(tmp_path):
    core = [json.loads(line) for line in (RECORDS / 'tool-turn.jsonl').read_text().splitlines()]
    count, link, monitor, ledger = artifact_rows()
    # The save block places frame-link between atis-latch and bridge-session.
    save_block = [{'type': 'atis-latch'}, count, {'type': 'bridge-session'}]
    # The incremental publication rows follow an assistant/API row and precede a result/user row.
    call = next(i for i, row in enumerate(core) if row['type'] == 'assistant'
                and any(block['type'] == 'tool_use' for block in row['message']['content']))
    record = tmp_path / 'record.jsonl'
    record.write_text(''.join(json.dumps(row) + '\n' for row in [*save_block, *core[:call + 1], link, monitor,
                                                                {'type': 'api-request'}, ledger]))
    reader = ClaudeHarness()
    active = reader.state(record)
    assert active.turn_open is True and active.pending_tools == ('Bash',)
    assert idle.check(active).dimension == 'turn-open'
    assert not reader.inputs(record).skipped
    with record.open('a') as stream:
        for row in [*core[call + 1:], ledger, link, monitor]:
            stream.write(json.dumps(row) + '\n')
    ended = reader.state(record)
    assert ended.turn_open is False and ended.pending_tools == () and ended.queued == ()
    assert len(ended.completed_turns) == 2
    assert idle.check(ended) is None
    assert [item.text for item in reader.inputs(record)] == ['Reply OK.', 'Run true with Bash, then reply OK.']
    assert not reader.inputs(record).skipped


@pytest.mark.parametrize('row', artifact_rows(), ids=['frame-count', 'frame-link', 'comment-monitor', 'autoreact-ledger'])
def test_each_artifact_row_carries_no_input_and_cannot_close_a_turn(tmp_path, row):
    core = json.loads((RECORDS / 'plain-turn.jsonl').read_text().splitlines()[0])
    record = tmp_path / 'record.jsonl'
    record.write_text(json.dumps(core) + '\n' + json.dumps(row) + '\n')
    reader = ClaudeHarness()
    assert reader.state(record).turn_open is True
    assert idle.check(reader.state(record)).dimension == 'turn-open'
    assert [item.text for item in reader.inputs(record)] == ['Reply OK.']
    assert not reader.inputs(record).skipped


def test_an_unknown_artifact_kind_is_still_unknown(tmp_path):
    record = copy_record(tmp_path, 'plain-turn')
    with record.open('a') as stream:
        for row in [*artifact_rows(), {'type': 'artifact-future-unmeasured'}]:
            stream.write(json.dumps(row) + '\n')
    reader = ClaudeHarness()
    assert reader.state(record).pending_tools is None
    assert idle.check(reader.state(record)).dimension == 'tools-unknown'
    assert any(item['reason'] == 'unknown row kind' for item in reader.inputs(record).skipped)
