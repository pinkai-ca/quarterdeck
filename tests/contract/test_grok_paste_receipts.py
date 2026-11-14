"""Synthetic ACP rows for measured single-line, folded and resumed 1.0.77 pastes.

Folding is a composer display feature: each accepted native input stores the
expanded payload in one user_message_chunk, including after same-home resume.
No captured runtime data, account or native process is used here.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from quarterdeck.harness import get
from quarterdeck.input_receipt import confirmed

FIXTURES = Path(__file__).parents[1] / "fixtures/grok/1.0.77"
LINES = ["Synthetic paste receipt; reply only OK."] + [
    f"Fictional row {i:02d}: blue green orange violet; 0123456789; another sample for the receipt."
    for i in range(48)
]
CASES = [
    ("paste-single-line", " ".join(LINES), 0),
    ("paste-folded-lines", "\n".join(LINES), 1),
    ("paste-resumed-folded", "\n".join(LINES), 3),
]
START = datetime(2040, 1, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize("name,payload,second", CASES)
def test_measured_paste_shapes_keep_full_exact_payload(name, payload, second):
    harness = get("grok")
    record = FIXTURES / f"{name}.jsonl"
    evidence, = harness.inputs(record)
    assert evidence.text == payload
    assert evidence.at == START + timedelta(seconds=second)
    assert evidence.kind == "accepted"
    assert "[Pasted:" not in evidence.text
    state = harness.state(record)
    assert state.last_input == payload and state.last_input_at == evidence.at
    assert confirmed(harness, record, payload, evidence.at) == "delivered"


@pytest.mark.parametrize("name,payload,second", CASES)
def test_folded_and_single_line_receipts_require_the_whole_payload(name, payload, second):
    harness = get("grok")
    record = FIXTURES / f"{name}.jsonl"
    at = START + timedelta(seconds=second)
    # Keep the same length and every character except the last. A matching start
    # or the same folded line count cannot establish a different native input.
    different = payload[:-1] + "!"
    assert len(different) == len(payload)
    assert confirmed(harness, record, different, at) is None
    assert confirmed(harness, record, payload[:200], at) is None
    assert confirmed(harness, record, "[Pasted: 49 lines]", at) is None
    assert confirmed(harness, record, payload, at + timedelta(seconds=1)) is None


def test_receipt_reader_observes_appended_expanded_input_after_resume(tmp_path):
    harness = get("grok")
    record = tmp_path / "updates.jsonl"
    record.write_text((FIXTURES / "paste-single-line.jsonl").read_text())
    folded = "\n".join(LINES)
    at = START + timedelta(seconds=3)
    assert confirmed(harness, record, folded, at) is None
    # Grok resumes the existing session file rather than creating another one.
    # Its background_tasks row supplies no receipt; the following user row does.
    background, accepted = (FIXTURES / "paste-resumed-folded.jsonl").read_text().splitlines()
    with record.open("a") as stream:
        stream.write(background + "\n")
    assert confirmed(harness, record, folded, at) is None
    with record.open("a") as stream:
        stream.write(accepted + "\n")
    assert confirmed(harness, record, folded, at) == "delivered"
    assert len(harness.inputs(record)) == 2
    assert json.loads(accepted)["params"]["update"]["content"]["text"] == folded
