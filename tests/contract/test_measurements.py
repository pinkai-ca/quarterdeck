"""The shared meaning of a step's absent, malformed or measured token counters."""

import json
from pathlib import Path

import pytest

from quarterdeck.harness import claude, codex, pi, opencode_record, antigravity_record
from quarterdeck.harness.types import Tokens
from tests.antigravity_shapes import _pb_uint, _pb_str


READERS = [
    (claude.usage_tokens, "input_tokens", "output_tokens"),
    (codex.usage_tokens, "input_tokens", "output_tokens"),
    (lambda u: pi._tokens({"usage": u}), "input", "output"),
    (lambda u: opencode_record._tokens({"tokens": u}), "input", "output"),
]


@pytest.mark.parametrize("reader,input_key,output_key", READERS)
@pytest.mark.parametrize("usage", [None, [], "foreign", {}, {"foreign": 2}])
def test_an_absent_or_unrecognized_measurement_is_not_zero(reader, input_key, output_key, usage):
    assert reader(usage) == Tokens.unknown()


@pytest.mark.parametrize("reader,input_key,output_key", READERS)
@pytest.mark.parametrize("count", [None, True, -1, 1.2, "3", float("nan"), float("inf")])
def test_invalid_counts_do_not_become_measured(reader, input_key, output_key, count):
    assert reader({"total": 1, input_key: count, output_key: 0}) == Tokens.unknown()


@pytest.mark.parametrize("reader,input_key,output_key", READERS)
def test_measured_zero_is_complete_and_unreported_classes_are_zero(reader, input_key, output_key):
    assert reader({"total": 0, input_key: 0, output_key: 0}) == Tokens()
    assert reader({"total": 3, input_key: 3}) == Tokens(read_uncached=3)


def test_proto_presence_proves_zero_but_absence_and_bad_wire_measurements_do_not():
    assert antigravity_record._parse_usage(None) == Tokens.unknown()
    assert antigravity_record._parse_usage(b"") == Tokens(), "present proto3 scalar defaults are zero"
    assert antigravity_record._parse_usage(_pb_uint(2, 3)) == Tokens(read_uncached=3)
    assert antigravity_record._parse_usage(_pb_str(2, "wrong wire")) == Tokens.unknown()
    assert antigravity_record._parse_usage(b"\x80") == Tokens.unknown()


def write(path: Path, rows: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return path


def test_repeated_response_blocks_and_late_usage_replace_one_unknown_measurement(tmp_path):
    user = {"type": "user", "message": {"content": "synthetic"}}
    answer = {"type": "assistant", "message": {"id": "response", "content": [{"type": "text", "text": "answer"}]}}
    finish = {"type": "system", "subtype": "turn_duration"}
    rows = [user, answer, answer, finish]
    path = write(tmp_path / "record.jsonl", rows)
    assert claude.turns(path)[0].tokens == Tokens.unknown(), "blocks share one response identity"
    late = {"type": "assistant", "message": {**answer["message"], "usage": {"input_tokens": 7, "output_tokens": 2}}}
    rows[-1:] = [late, late, finish]
    path = write(path, rows)
    assert claude.turns(path)[0].tokens == Tokens(read_uncached=7, written=2)
    other = {"type": "assistant", "message": {"id": "another", "content": [], "usage": []}}
    rows.insert(-1, other)
    assert claude.turns(write(path, rows))[0].tokens == Tokens(read_uncached=7, written=2, unmeasured=1)
    assert claude.turns(path) == claude.turns(path), "rereading does not accumulate usage"


def event(kind, **payload):
    return {"type": "event_msg", "payload": {"type": kind, **payload}}


def item(kind, **payload):
    return {"type": "response_item", "payload": {"type": kind, **payload}}


def test_codex_missing_tool_response_usage_keeps_measured_final_response(tmp_path):
    rows = [event("task_started"), item("function_call", call_id="call", name="lookup"),
            item("function_call_output", call_id="call", output="done"),
            item("message", role="assistant", content="answer"),
            event("token_count", info={"last_token_usage": {"input_tokens": 5, "output_tokens": 2}}),
            event("task_complete")]
    path = write(tmp_path / "record.jsonl", rows)
    assert codex.turns(path)[0].tokens == Tokens(read_uncached=5, written=2, unmeasured=1)
    rows.insert(2, event("token_count", info={"last_token_usage": {"input_tokens": 0, "output_tokens": 0}}))
    assert codex.turns(write(path, rows))[0].tokens == Tokens(read_uncached=5, written=2)
    for row in rows[:]:
        if row["payload"]["type"] == "token_count":
            rows.remove(row)
    assert codex.turns(write(path, rows))[0].tokens == Tokens(unmeasured=2)


def test_pi_valid_step_survives_malformed_sibling_usage(tmp_path):
    rows = [{"type": "message", "message": {"role": "user", "content": "synthetic"}},
            {"type": "message", "message": {"role": "assistant", "content": [], "stopReason": "toolUse",
                                            "usage": {"input": 5, "output": 3}}},
            {"type": "message", "message": {"role": "assistant", "content": [], "stopReason": "stop", "usage": []}}]
    assert pi.turns(write(tmp_path / "record.jsonl", rows))[0].tokens == Tokens(read_uncached=5, written=3, unmeasured=1)
