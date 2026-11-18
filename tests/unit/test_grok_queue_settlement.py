"""Positive Grok screen-queue handoff and subsequent exact-record settlement."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from quarterdeck import alarms, pending, post, watch
from quarterdeck.door import Door
from quarterdeck.harness import grok_screen
from quarterdeck.record_owner import register
from quarterdeck.tmux import ControlFrame
from .test_pending_delivery import setup
from ..contract.test_grok_native_queue import frame, sample


@pytest.mark.parametrize(("layout", "composer", "first"), [
    (112, "empty", "queued"),
    ("response-256", "empty", "queued"),
    ("response-256", "unread", "queued"),
    ("response-256", "unread", "pending"),
    ("response-256", "unread", "legacy-submitted"),
])
def test_grok_visible_queue_waits_past_600s_then_settles_once(setup, monkeypatch, layout, composer, first):
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
    message = svc.store.new("order", "sender", seat.name, "A fictional queued task.")
    payload = post.typed([message])
    rows = [row.replace(grok_screen.queue_preview(data["payloads"][0]), grok_screen.queue_preview(payload))
            for row in data["screens"]["1"]]
    queued_rows = rows
    if composer == "unread":
        rows = list(rows)
        prompt = next(i for i, row in enumerate(rows) if row.startswith("  │ ❯ "))
        rows.insert(prompt + 1, rows[prompt].replace("❯", " ", 1))
        queued_rows = rows

    def control_frame(_pane):
        prompt = next(i for i, row in enumerate(rows) if row.startswith("  │ ❯ "))
        return ControlFrame(live["pane"], frame(rows), True, (6, prompt))

    monkeypatch.setattr(svc.tmux, "capture", lambda *args, **kwargs: frame(rows))
    monkeypatch.setattr(svc.tmux, "read_control_frame", control_frame, raising=False)
    attempt = svc.attempts.open(message, seat.name, "%1", 123, seat.launched().at)
    proof = pending.Proof.begin(seat, attempt, payload)
    draft = seat.harness.screen(frame(data["draft"]))
    proof.save(phase="submitted", shown=draft.box.rstrip(" "), since=at.isoformat())
    svc.attempts.close(attempt, "pending", "synthetic submission")
    door = Door(seat)
    door.proof = proof
    door.before_submit = draft
    if first == "pending":
        rows = data["screens"]["0"]
    delivery = door.confirm(payload)
    assert delivery.result == ("pending" if first == "pending" else "queued")
    proof.finish(delivery.result)
    assert proof.data["phase"] == ("submitted" if first == "pending" else "queued")
    if first == "legacy-submitted":
        proof.save(phase="submitted")
    elif first == "pending":
        proof.save(record=None)
    svc.attempts.close(attempt, delivery.result, delivery.detail)
    rows = queued_rows
    later = at + timedelta(seconds=601)
    monkeypatch.setattr(post, "now", lambda: later)
    screen = door.screen()
    if composer == "unread":
        assert screen.box is None and screen.working is None and screen.unknown == grok_screen.UNKNOWN_HINT
    else:
        assert screen.box == ""
    before = proof.path.read_bytes()
    with monkeypatch.context() as reading:
        reading.setattr(seat, "record", lambda: pytest.fail("the view must not publish a record join"))
        assert proof.reading(seat, screen, now=later) == (
            "queued on screen; waiting for record confirmation", "record confirmation", True)
    facts = watch.facts(svc.ship, svc.tmux, walls=[], login=[])
    assert seat.name not in facts.needs_human
    assert f"{seat.name}: queued on screen; waiting for record confirmation" in facts.pending
    assert proof.path.read_bytes() == before
    assert not door.reading().open
    svc.reconcile()
    proof = pending.active(seat)[0]
    assert proof.data["phase"] == "queued" and proof.data["screen_queue"] is True
    assert svc.attempts.latest()[message.id] == "queued"
    assert len(svc.attempts.for_message(message.id)) == 1
    assert proof.confirmed(seat) is None
    # A different queued envelope no longer keeps this delivery quiet.
    other = "qd > answer from sample · " + "-".join(("ans", "f0e1d2"))
    rows = [row.replace(payload.split("\n", 1)[0], other) for row in queued_rows]
    facts = watch.facts(svc.ship, svc.tmux, walls=[], login=[])
    assert seat.name in facts.needs_human
    assert any("native Grok queue entry cannot be verified" in reason for reason in facts.pending)
    monkeypatch.setattr(svc, "alarm_recipients", lambda *_: ["operator"])
    for _ in range(3):
        svc.needs_human(seat)
    alert = alarms.load(svc.runtime, seat.name, "human")
    assert len(alert["published"]) == 1
    # The old turn ends and the queue disappears before the new user row lands.
    # Missing and different entries share the same standing alarm.
    completion = {"timestamp": int((later - timedelta(seconds=1)).timestamp()), "method": "_x.ai/session/update",
        "params": {"sessionId": "synthetic-queue", "update": {
            "sessionUpdate": "turn_completed", "prompt_id": "synthetic-initial-prompt"}}}
    with record.open("a") as stream:
        stream.write(json.dumps(completion) + "\n")
    rows = data["screens"]["0"]
    starting = door.screen()
    assert proof.confirmed(seat) is None  # the service's first record read
    before = proof.path.read_bytes()
    assert proof.reading(seat, starting, now=later)[1:] == ("needs a human", False)
    assert proof.path.read_bytes() == before
    svc.needs_human(seat)
    assert alarms.load(svc.runtime, seat.name, "human")["published"] == alert["published"]
    svc.looks()
    assert live["pasted"] == live["submitted"] == 0
    # The exact owned record wins even with a missing queue and before the writer settles it.
    assert proof.confirmed(seat) is None
    with record.open("a") as stream:
        stream.write(native_input(payload.replace("fictional", "different"), later))
    assert proof.reading(seat, starting, now=later)[1:] == ("needs a human", False)
    with record.open("a") as stream:
        stream.write(native_input(payload, later + timedelta(seconds=1)))
    assert proof.reading(seat, starting, now=later + timedelta(seconds=6)) is None
    assert proof.path.read_bytes() == before
    svc.reconcile()
    svc.reconcile()
    assert svc.attempts.latest()[message.id] == "delivered"
    assert len(svc.attempts.for_message(message.id)) == 1
    saved = json.loads(proof.path.read_text())
    assert saved["phase"] == "delivered" and saved["screen_queue"] is False
    assert live["pasted"] == live["submitted"] == 0
