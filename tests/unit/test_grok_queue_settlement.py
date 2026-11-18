"""Positive Grok screen-queue handoff and subsequent exact-record settlement."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from quarterdeck import pending, post, watch
from quarterdeck.door import Door
from quarterdeck.record_owner import register
from quarterdeck.tmux import ControlFrame
from .test_pending_delivery import setup
from ..contract.test_grok_native_queue import frame, sample


@pytest.mark.parametrize("layout", [112, "response-256"])
def test_grok_visible_queue_waits_past_600s_then_settles_once(setup, monkeypatch, layout):
    svc, old, live, _ = setup
    svc.ship.pools[old.pool.name] = replace(old.pool, provider="xai")
    svc.ship.seats[old.name] = replace(old.spec, harness="grok")
    seat = svc.seat(old.name)
    launch_path = seat.trusted / "launch.json"
    launch = json.loads(launch_path.read_text())
    launch.update(harness="grok", argv=["grok"])
    launch_path.write_text(json.dumps(launch))
    record = seat.home / "sessions/sample/session/updates.jsonl"
    record.parent.mkdir(parents=True)
    at = post.now()

    def native_input(text, stamp):
        return json.dumps({"timestamp": int(stamp.timestamp()), "method": "session/update", "params": {
            "sessionId": "synthetic-queue", "update": {"sessionUpdate": "user_message_chunk",
            "content": {"type": "text", "text": text}}}}) + "\n"

    record.write_text(native_input("Initial fictional turn is still running.", at - timedelta(seconds=1)))
    (seat.trusted / "record").write_text(str(record) + "\n")
    register(seat.trusted, record, "grok")
    data = sample(layout)
    payload = data["payloads"][0]
    rows = data["screens"]["1"]

    def control_frame(_pane):
        prompt = next(i for i, row in enumerate(rows) if row.startswith("  │ ❯ "))
        return ControlFrame(live["pane"], frame(rows), True, (6, prompt))

    monkeypatch.setattr(svc.tmux, "capture", lambda *args, **kwargs: frame(rows))
    monkeypatch.setattr(svc.tmux, "read_control_frame", control_frame, raising=False)
    message = svc.store.new("order", "sender", seat.name, "A fictional queued task.")
    attempt = svc.attempts.open(message, seat.name, "%1", 123, seat.launched().at)
    proof = pending.Proof.begin(seat, attempt, payload)
    draft = seat.harness.screen(frame(data["draft"]))
    proof.save(phase="submitted", shown=draft.box.rstrip(" "), since=at.isoformat())
    svc.attempts.close(attempt, "pending", "synthetic submission")
    door = Door(seat)
    door.proof = proof
    door.before_submit = draft
    delivery = door.confirm(payload)
    assert delivery.result == "queued"
    assert proof.data["screen_queue"] is True
    proof.finish(delivery.result)
    assert proof.data["phase"] == "submitted"  # the record has not accepted it
    svc.attempts.close(attempt, delivery.result, delivery.detail)
    later = at + timedelta(seconds=601)
    monkeypatch.setattr(post, "now", lambda: later)
    screen = door.screen()
    assert screen.box == ""
    assert proof.reading(seat, screen, now=later) == (
        "queued on screen; waiting for record confirmation", "record confirmation", True)
    facts = watch.facts(svc.ship, svc.tmux, walls=[], login=[])
    assert seat.name not in facts.needs_human
    assert f"{seat.name}: queued on screen; waiting for record confirmation" in facts.pending
    svc.reconcile()
    assert svc.attempts.latest()[message.id] == "queued"
    assert len(svc.attempts.for_message(message.id)) == 1
    assert proof.confirmed(seat) is None
    # A different queued envelope no longer keeps this delivery quiet.
    other = "qd > answer from sample · " + "-".join(("ans", "f0e1d2"))
    rows = [row.replace(payload.split("\n", 1)[0], other) for row in rows]
    facts = watch.facts(svc.ship, svc.tmux, walls=[], login=[])
    assert seat.name in facts.needs_human
    assert any("pending delivery exceeded 600s" in reason for reason in facts.pending)
    # The old foreground turn ends; the queue leaves the screen before the new
    # user row lands. The preceding record completion bounds a five-second wait.
    completion = {"timestamp": int((later - timedelta(seconds=1)).timestamp()), "method": "_x.ai/session/update",
        "params": {"sessionId": "synthetic-queue", "update": {
            "sessionUpdate": "turn_completed", "prompt_id": "synthetic-initial-prompt"}}}
    with record.open("a") as stream:
        stream.write(json.dumps(completion) + "\n")
    rows = data["screens"]["0"]
    starting = door.screen()
    assert proof.confirmed(seat) is None  # the service's first record read
    before = proof.path.read_bytes()
    assert proof.reading(seat, starting, now=later) == (
        "queued prompt starting; waiting for record confirmation", "record confirmation", True)
    assert proof.path.read_bytes() == before  # a screen read advances no clock
    assert not seat.harness.queued_waiting(payload, starting, record=record, now=later + timedelta(seconds=6))
    # A later pass reads the old record, then sees the dequeued screen. The row
    # lands in between: the second record check wins even after both time bounds.
    assert proof.confirmed(seat) is None
    with record.open("a") as stream:
        stream.write(native_input(payload, later))
    assert proof.reading(seat, starting, now=later + timedelta(seconds=6)) is None
    svc.reconcile()
    svc.reconcile()
    assert svc.attempts.latest()[message.id] == "delivered"
    assert len(svc.attempts.for_message(message.id)) == 1
    saved = json.loads(proof.path.read_text())
    assert saved["phase"] == "delivered" and saved["screen_queue"] is False
    assert live["pasted"] == live["submitted"] == 0
