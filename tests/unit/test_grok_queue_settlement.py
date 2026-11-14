"""Positive Grok screen-queue handoff and subsequent exact-record settlement."""

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from quarterdeck import pending, post
from quarterdeck.door import Door
from quarterdeck.record_owner import register
from .test_pending_delivery import setup
from ..contract.test_grok_native_queue import frame, sample


def test_grok_visible_queue_waits_past_600s_then_settles_once(setup, monkeypatch):
    svc, old, live, _ = setup
    svc.ship.pools[old.pool.name] = replace(old.pool, provider="xai")
    svc.ship.seats[old.name] = replace(old.spec, harness="grok")
    seat = svc.seat(old.name)
    launch_path = seat.folder / "launch.json"
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
    (seat.folder / "record").write_text(str(record) + "\n")
    register(seat.folder, record, "grok")
    data = sample(112)
    payload = data["payloads"][0]
    message = svc.store.new("order", "sender", seat.name, "A fictional queued task.")
    attempt = svc.attempts.open(message, seat.name, "%1", 123, seat.launched().at)
    proof = pending.Proof.begin(seat, attempt, payload)
    proof.save(phase="submitted", shown="[Pasted: 8 lines]", since=at.isoformat())
    svc.attempts.close(attempt, "pending", "synthetic submission")
    screen = replace(seat.harness.screen(frame(data["screens"]["1"])), box="")
    door = Door(seat)
    door.proof = proof
    door.before_submit = seat.harness.screen(frame(data["draft"]))
    monkeypatch.setattr(door, "screen", lambda: screen)
    delivery = door.confirm(payload)
    assert delivery.result == "queued"
    assert proof.data["screen_queue"] is True
    proof.finish(delivery.result)
    assert proof.data["phase"] == "submitted"  # the record has not accepted it
    svc.attempts.close(attempt, delivery.result, delivery.detail)
    later = at + timedelta(seconds=601)
    monkeypatch.setattr(post, "now", lambda: later)
    assert proof.reading(seat, screen, now=later) == (
        "queued on screen; waiting for record confirmation", "record confirmation", True)
    svc.reconcile()
    assert svc.attempts.latest()[message.id] == "queued"
    assert len(svc.attempts.for_message(message.id)) == 1
    with record.open("a") as stream:
        stream.write(native_input(payload, later))
    svc.reconcile()
    svc.reconcile()
    assert svc.attempts.latest()[message.id] == "delivered"
    assert len(svc.attempts.for_message(message.id)) == 1
    saved = json.loads(proof.path.read_text())
    assert saved["phase"] == "delivered" and saved["screen_queue"] is False
    assert live["pasted"] == live["submitted"] == 0
