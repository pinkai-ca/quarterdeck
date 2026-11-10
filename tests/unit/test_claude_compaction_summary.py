"""A compaction summary is not a prompt: an idle compaction leaves a Claude seat idle.

Claude Code 2.1.317 compacts an idle session by writing a compact_boundary system row,
a user row marked isCompactSummary ('This session is being continued...'), attachments,
an informational system row and the metadata rows; no reply or turn_duration follows.
The fixture has that shape with synthetic values (tests/fixtures/claude/2.1.317).
"""

from datetime import datetime, timezone
import json
from pathlib import Path

from quarterdeck.harness import claude
from quarterdeck.harness.claude import ClaudeHarness

FIXTURE = Path(__file__).parents[1] / "fixtures" / "claude" / "2.1.317" / "idle-compaction.jsonl"
SUMMARY = "This session is being continued"


def rows() -> list[dict]:
    return [json.loads(line) for line in FIXTURE.read_text().splitlines()]


def record(tmp_path: Path, selected: list[dict]) -> Path:
    path = tmp_path / "record.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in selected))
    return path


def test_the_fixture_is_a_finished_turn_then_an_idle_compaction():
    kinds = [(row["type"], row.get("subtype"), bool(row.get("isCompactSummary"))) for row in rows()]
    assert ("system", "turn_duration", False) in kinds
    boundary = kinds.index(("system", "compact_boundary", False))
    assert kinds[boundary + 1] == ("user", None, True) and ("system", "informational", False) in kinds[boundary:]
    assert not any(kind == "assistant" for kind, _, _ in kinds[boundary:])


def test_an_idle_compaction_leaves_the_seat_idle_and_its_last_input_unchanged(tmp_path):
    path = record(tmp_path, rows())
    state = ClaudeHarness().state(path)
    assert state.turn_open is False
    assert state.last_input == "Synthetic finished order"
    assert state.last_input_at == datetime(2026, 11, 10, 5, 17, tzinfo=timezone.utc)
    assert len(state.completed_turns) == 1


def test_a_compaction_mid_turn_leaves_that_turn_open(tmp_path):
    selected = [row for row in rows() if row.get("type") != "assistant" and row.get("subtype") != "turn_duration"]
    state = ClaudeHarness().state(record(tmp_path, selected))
    assert state.turn_open is True
    assert state.last_input == "Synthetic finished order", "the open turn is still the typed prompt's"
    assert len(state.completed_turns) == 0


def test_inputs_do_not_list_the_summary(tmp_path):
    found = claude.inputs(record(tmp_path, rows()))
    texts = [item.text for item in found]
    assert texts == ["Synthetic finished order"]
    assert not any(text.startswith(SUMMARY) for text in texts)


def test_only_the_marked_summary_is_set_aside(tmp_path):
    typed = [dict(row, isCompactSummary=False) if row.get("isCompactSummary") else row for row in rows()]
    state = ClaudeHarness().state(record(tmp_path, typed))
    assert state.turn_open is True and state.last_input.startswith(SUMMARY), "an unmarked user row is still a prompt"
