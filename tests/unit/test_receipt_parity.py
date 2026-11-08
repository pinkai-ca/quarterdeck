"""The check and the service confirm the same fresh, exact adapter input evidence."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import json

import pytest

from quarterdeck import check, input_receipt, pending, post
from quarterdeck.door import Door
from quarterdeck.harness.claude import ClaudeHarness
from .test_pending_delivery import setup, input_row


@pytest.mark.parametrize("operation, result", [("enqueue", "queued"), ("remove", "delivered"),
                                               ("dequeue", "delivered"), ("future-operation", None)])
def test_check_and_journal_match_the_same_timestamped_queue(tmp_path, operation, result):
    at = datetime(2030, 1, 1, tzinfo=timezone.utc)
    record = tmp_path / "synthetic.jsonl"
    text = "exact  transport\nsecond line"
    rows = [{"type": "queue-operation", "operation": "enqueue", "content": text, "timestamp": at.isoformat()}]
    if operation != "enqueue":
        rows.append(dict(rows[0], operation=operation, timestamp=(at + timedelta(seconds=1)).isoformat()))
    record.write_text("\n".join(map(json.dumps, rows)))
    seat = SimpleNamespace(harness=ClaudeHarness(), tmux=None, record=lambda: record)
    door = Door(seat)
    door.since = at
    plain = door.confirm(text, seconds=0)
    proof = pending.Proof(tmp_path / "proof.json", {"identity": {"harness": "claude"},
        "record": str(record), "payload": text, "since": at.isoformat()})
    door.proof = proof
    journal = door.confirm(text, seconds=0)
    assert plain.result == (result or "blind")
    assert journal.result == (result or "pending")
    assert proof.confirmed(seat) == result
    assert input_receipt.confirmed(seat.harness, record, text, at + timedelta(seconds=2)) is None


@pytest.mark.parametrize("fresh", [True, False])
def test_real_check_probe_and_service_delivery_use_adapter_inputs(setup, monkeypatch, fresh):
    svc, seat, live, record = setup
    original_keys = svc.tmux.keys
    def keys(*args):
        original_keys(*args)
        at = post.now() if fresh else post.now() - timedelta(days=1)
        input_row(record, live["box"], at)
        live["box"] = ""
    monkeypatch.setattr(svc.tmux, "keys", keys)
    monkeypatch.setattr(seat, "up", lambda **kwargs: "synthetic launch")
    monkeypatch.setattr(check, "wait_for", lambda predicate, *args: predicate())
    checks = check.probe(svc.ship, seat)
    transport = next(c for c in checks if "paste matched" in c.name)
    assert transport.passed is fresh
    message = svc.store.new("order", "ship", seat.name, "second exact transport")
    svc.deliver(seat, [message])
    assert svc.attempts.latest()[message.id] == ("delivered" if fresh else "pending")
    assert live["pasted"] == live["submitted"] == 2


def test_fifo_queue_receipts_keep_untimed_slots_and_unknown_operations_prove_nothing(tmp_path):
    at = datetime(2030, 1, 1, tzinfo=timezone.utc)
    record = tmp_path / "queue.jsonl"
    text = "exact  input"
    def event(operation, stamp):
        return {"type": "queue-operation", "operation": operation,
                "content": '<pasted_content id="synthetic">\n' + text + '\n</pasted_content>',
                "timestamp": stamp.isoformat() if stamp else None}
    rows = [event("enqueue", None), event("enqueue", at), event("dequeue", at + timedelta(seconds=1))]
    record.write_text("\n".join(map(json.dumps, rows)))
    assert input_receipt.confirmed("claude", record, text, at) == "queued", "dequeue consumed the untimed FIFO slot"
    rows.append(event("remove", at + timedelta(seconds=2)))
    record.write_text("\n".join(map(json.dumps, rows)))
    assert input_receipt.confirmed("claude", record, text, at) == "delivered"
    assert input_receipt.confirmed("claude", record, text, at + timedelta(seconds=1)) is None
    rows = [event("enqueue", at), event("unrecognized", at + timedelta(seconds=1))]
    record.write_text("\n".join(map(json.dumps, rows)))
    assert input_receipt.confirmed("claude", record, text, at) is None


@pytest.mark.parametrize("name", ["claude", "codex", "pi"])
def test_unreadable_input_shapes_do_not_hide_fresh_siblings_or_confirm_untimed_history(tmp_path, name):
    at = datetime(2030, 1, 1, tzinfo=timezone.utc)
    make = {
        "claude": lambda value: {"type": "user", "message": {"content": value}},
        "codex": lambda value: {"type": "response_item", "payload": {"role": "user", "content": value}},
        "pi": lambda value: {"type": "message", "message": {"role": "user", "content": value}},
    }[name]
    content = [{"type": "input_text", "text": "exact input"}] if name == "codex" else "exact input"
    record = tmp_path / "synthetic.jsonl"
    rows = [make(content), dict(make(17), timestamp=at.isoformat())]
    record.write_text("\n".join(map(json.dumps, rows)))
    assert input_receipt.confirmed(name, record, "exact input", at) is None
    rows.append(dict(make(content), timestamp=at.isoformat()))
    record.write_text("\n".join(map(json.dumps, rows)))
    assert input_receipt.confirmed(name, record, "exact input", at) == "delivered"
