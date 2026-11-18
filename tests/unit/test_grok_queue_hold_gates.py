"""Grok queue evidence requires an exact, complete native preview in the same launch."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from quarterdeck import pending, post
from quarterdeck.door import Door
from quarterdeck.harness.grok import GrokHarness
from quarterdeck.record_owner import register
from .test_pending_delivery import setup  # noqa: F401  (fixture)
from ..contract.test_grok_native_queue import frame, sample

WIDTHS = [112, 256]


def rows(width, count):
    return list(sample(width)["screens"][str(count)])


def edited(lines, old, new):
    changed = [line.replace(old, new) for line in lines]
    assert changed != lines, f"fixture no longer contains {old!r}"
    return changed


@pytest.mark.parametrize("width", WIDTHS)
def test_a_queue_title_whose_count_disagrees_with_its_entries_is_unreadable(width):
    harness, data = GrokHarness(), sample(width)
    screen = harness.screen(frame(edited(rows(width, 1), "▾ Queued 1", "▾ Queued 2")))
    assert not harness.queued_waiting(data["payloads"][0], screen)
    before = harness.screen(frame(data["draft"]))
    assert harness.queued_input(data["payloads"][0], before, screen) is None


@pytest.mark.parametrize("width", WIDTHS)
def test_entries_out_of_order_or_unnumbered_are_unreadable(width):
    harness, data = GrokHarness(), sample(width)
    swapped = edited(edited(rows(width, 2), "#1 ", "#X "), "#2 ", "#1 ")
    swapped = edited(swapped, "#X ", "#2 ")
    screen = harness.screen(frame(swapped))
    for payload in data["payloads"]:
        assert not harness.queued_waiting(payload, screen)


@pytest.mark.parametrize("width", WIDTHS)
def test_a_queue_that_does_not_show_this_payload_is_not_this_delivery(width):
    harness, data = GrokHarness(), sample(width)
    screen = harness.screen(frame(rows(width, 1)))
    assert harness.queued_waiting(data["payloads"][0], screen)
    assert not harness.queued_waiting(data["payloads"][1], screen)
    other = data["payloads"][0].replace("ord-a1b2", "ord-z9y8", 1)
    assert not harness.queued_waiting(other, screen)
    longer = data["payloads"][0] + "\nOne more fictional line."
    assert not harness.queued_waiting(longer, screen)


@pytest.mark.parametrize("width", WIDTHS)
def test_no_new_entry_or_an_unworking_before_screen_is_not_a_queue_handoff(width):
    harness, data = GrokHarness(), sample(width)
    one = harness.screen(frame(rows(width, 1)))
    two = harness.screen(frame(rows(width, 2)))
    # The entry was already there before the paste: nothing new was queued by it.
    assert harness.queued_input(data["payloads"][0], one, one) is None
    # Two entries appeared, or the wrong one is last: not exactly this paste.
    empty = harness.screen(frame(rows(width, 0)))
    assert harness.queued_input(data["payloads"][1], empty, two) is None
    assert harness.queued_input(data["payloads"][0], one, two) is None
    # A before screen that was not working cannot have queued anything.
    idle = replace(harness.screen(frame(data["draft"])), working=False)
    assert harness.queued_input(data["payloads"][0], idle, one) is None


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("change", ["unknown", "interruption", "box", "idle", "no-box"])
def test_any_screen_the_reader_cannot_vouch_for_is_not_waiting(width, change):
    harness, data = GrokHarness(), sample(width)
    screen = harness.screen(frame(rows(width, 1)))
    assert harness.queued_waiting(data["payloads"][0], screen)
    spoiled = {
        "unknown": replace(screen, unknown="unmeasured"),
        "interruption": replace(screen, interruption="synthetic dialog"),
        "box": replace(screen, box="a draft the person typed"),
        "idle": replace(screen, working=False),
        "no-box": replace(screen, box=None),
    }[change]
    assert not harness.queued_waiting(data["payloads"][0], spoiled)


def test_the_send_now_key_stays_unset():
    assert GrokHarness().keys.send_now is None


def _queued_delivery(setup, monkeypatch, width):  # noqa: F811
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
    record.write_text(json.dumps({
        "timestamp": int((at - timedelta(seconds=1)).timestamp()), "method": "session/update",
        "params": {"sessionId": "synthetic-queue", "update": {
            "sessionUpdate": "user_message_chunk",
            "content": {"type": "text", "text": "Initial fictional turn is still running."}}}}) + "\n")
    (seat.trusted / "record").write_text(str(record) + "\n")
    register(seat.trusted, record, "grok")
    data = sample(width)
    payload = data["payloads"][0]
    message = svc.store.new("order", "sender", seat.name, "A fictional queued task.")
    attempt = svc.attempts.open(message, seat.name, "%1", 123, seat.launched().at)
    proof = pending.Proof.begin(seat, attempt, payload)
    proof.save(phase="submitted", shown="[Pasted: 8 lines]", since=at.isoformat())
    svc.attempts.close(attempt, "pending", "synthetic submission")
    queued = replace(seat.harness.screen(frame(data["screens"]["1"])), box="")
    door = Door(seat)
    door.proof = proof
    door.before_submit = seat.harness.screen(frame(data["draft"]))
    monkeypatch.setattr(door, "screen", lambda: queued)
    delivery = door.confirm(payload)
    assert delivery.result == "queued" and proof.data["screen_queue"] is True
    proof.finish(delivery.result)
    svc.attempts.close(attempt, delivery.result, delivery.detail)
    return svc, seat, live, proof, message, data, at


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("later", ["queue-gone", "other-payload", "unknown", "box-typed", "clipped", "duplicate"])
def test_after_600s_a_queue_that_no_longer_vouches_holds_and_is_never_resent(
        setup, monkeypatch, width, later):  # noqa: F811
    svc, seat, live, proof, message, data, at = _queued_delivery(setup, monkeypatch, width)
    harness = seat.harness
    entry = next(row for row in rows(width, 1) if "#1 " in row)
    duplicate = edited(rows(width, 2), next(row for row in rows(width, 2) if "#2 " in row),
                       entry.replace("#1 ", "#2 ", 1))
    first = proof.data["payload"].split("\n", 1)[0]
    edge = next(row.index("╮") for row in rows(width, 1) if row.startswith("  ╭"))
    clipped = entry.rstrip().replace(first, first + "x" * (edge - len(entry.rstrip())))
    proof_at_edge = pending.Proof(proof.path, {
        **proof.data, "payload": proof.data["payload"].replace(first, first + "x" * (edge - len(entry.rstrip())), 1)})
    screens = {
        "queue-gone": replace(harness.screen(frame(data["screens"]["0"])), box=""),
        "other-payload": replace(harness.screen(frame(
            edited(rows(width, 1), "ord-a1b2", "ord-z9y8"))), box=""),
        "unknown": replace(harness.screen(frame(data["screens"]["1"])), box="", unknown="unmeasured"),
        "box-typed": replace(harness.screen(frame(data["screens"]["1"])), box="a person's draft"),
        "clipped": replace(harness.screen(frame(edited(rows(width, 1), entry, clipped))), box=""),
        "duplicate": replace(harness.screen(frame(duplicate)), box=""),
    }
    moment = at + timedelta(seconds=601)
    monkeypatch.setattr(post, "now", lambda: moment)
    reading = proof_at_edge if later == "clipped" else proof
    reason, remedy, quiet = reading.reading(seat, screens[later], now=moment)
    assert quiet is False and remedy == "needs a human"
    assert "native Grok queue entry cannot be verified" in reason
    assert len(svc.attempts.for_message(message.id)) == 1
    assert live["pasted"] == live["submitted"] == 0


@pytest.mark.parametrize("width", WIDTHS)
def test_before_600s_a_lost_queue_holds_and_is_never_resent(setup, monkeypatch, width):  # noqa: F811
    svc, seat, live, proof, message, data, at = _queued_delivery(setup, monkeypatch, width)
    gone = replace(seat.harness.screen(frame(data["screens"]["0"])), box="")
    moment = at + timedelta(seconds=30)
    reason, remedy, quiet = proof.reading(seat, gone, now=moment)
    assert quiet is False and remedy == "needs a human" and "native Grok queue entry cannot be verified" in reason
    assert live["pasted"] == live["submitted"] == 0


def test_an_exact_queue_entry_on_a_different_launch_cannot_quiet_its_alarm(setup, monkeypatch):
    svc, seat, live, proof, _, data, at = _queued_delivery(setup, monkeypatch, 112)
    path = seat.trusted / "launch.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "at": (at + timedelta(seconds=1)).isoformat()}))
    native = seat.harness.screen(frame(data["screens"]["1"]))
    assert proof.reading(seat, native)[1:] == ("needs a human", False)
    assert pending.detached(seat)
    svc.reconcile()
    assert live["pasted"] == live["submitted"] == 0
