"""Native popAll returns one selected queued input to the draft, never to the model.

Shapes follow Claude Code 2.1.317 popAllEditable/queue logger/popQueuedIntoDraft.
All content, identities and times below are synthetic; no native conversation is copied.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from quarterdeck import input_receipt, post, post_threads, watch
from quarterdeck.harness import claude
from quarterdeck.seat import Seat
from quarterdeck.service import Service
from .test_human_recovery import seat_for
from .test_input_receipt import append, bind
from .test_pending_delivery import setup


AT = datetime(2030, 1, 1, tzinfo=timezone.utc)
FIRST = "Synthetic  answer\nsecond line"
OTHER = "Synthetic unrelated input"
REPORT = "Synthetic report"


def row(kind, second, **fields):
    return dict(type=kind, timestamp=(AT + timedelta(seconds=second)).isoformat(), **fields)


def queue(text, second, operation="enqueue", **fields):
    return row("queue-operation", second, operation=operation, content=text, **fields)


def user(text, second):
    return row("user", second, message=dict(role="user", content=text))


def working():
    return [user("Synthetic running task", 0), row("assistant", 1, message=dict(
        id="synthetic-step", model="synthetic", content=[dict(type="tool_use", id="synthetic-tool",
        name="Bash", input=dict(command="synthetic command"))], stop_reason="tool_use"))]


def finish():
    return [row("user", 7, message=dict(role="user", content=[dict(type="tool_result",
            tool_use_id="synthetic-tool", content="Synthetic result")])),
            row("assistant", 8, message=dict(id="synthetic-final", content=[dict(type="text", text="Done")], stop_reason="end_turn")),
            row("system", 9, subtype="turn_duration", durationMs=9000)]


def record(tmp_path, *entries):
    path = tmp_path / "record"
    append(path, *entries)
    return path


def test_pop_returns_one_item_and_never_proves_delivery(tmp_path):
    path = record(tmp_path, *working(), queue(FIRST, 2), queue(OTHER, 3), queue(FIRST, 4, "popAll"))
    state = claude.state(path)
    assert state.queued == (OTHER,)
    assert state.consumed == ()
    assert input_receipt.confirmed("claude", path, FIRST, AT + timedelta(seconds=1)) is None
    assert input_receipt.confirmed("claude", path, OTHER, AT + timedelta(seconds=1)) == "queued"
    assert state.turn_open is True and state.pending_tools == ("Bash",)
    assert state.completed_turns == () and state.last_input == "Synthetic running task"


@pytest.mark.parametrize("kind", ["answer", "report"])
def test_popped_nonopen_post_is_unread_and_carried_until_actual_submission(setup, monkeypatch, kind):
    svc, original, live, _ = setup
    path = original.home / "claude.jsonl"
    append(path, *working())
    seat = bind(svc, original, "claude", path)
    # Neither an answer nor a report is itself an open obligation.
    parent = svc.store.new("question" if kind == "answer" else "order", seat.name, "desk", "Synthetic parent")
    message = svc.store.new(kind, "desk", seat.name, "Synthetic reply", re=parent.id)
    attempt = svc.attempts.open(message, seat.name, "%1", 123, seat.launched().at)
    svc.attempts.close(attempt, "queued", "synthetic native queue proof")
    text = post.typed([message])
    append(path, queue(text, 2), queue(text, 4, "popAll"))
    assert message not in svc.store.open_for(seat.name)
    assert seat._queued_unread(svc.store, svc.attempts) == [message]
    restarted = Service(svc.ship, svc.tmux)
    try:
        restarted.reconcile()
        assert svc.attempts.latest()[message.id] == "queued"
        carried = []
        # Exercise the real restart/fresh lifecycle entry points and real first_message;
        # replace only retirement/program launch, so no native process or provider runs.
        monkeypatch.setattr(seat, "_down", lambda: "synthetic down")
        def launch(*, resume=False, observation=None):
            first, messages = seat.first_message()
            carried.append((resume, first, messages))
            return "synthetic launch"
        monkeypatch.setattr(seat, "_up", launch)
        seat.restart()
        seat.fresh()
        assert [resume for resume, _, _ in carried] == [True, False]
        assert all(message in messages for _, _, messages in carried)
        for _, first, _ in carried:
            rows = {row['id']: row for row in (json.loads(line) for line in first.splitlines() if line.startswith('{'))}
            assert message.id in rows[parent.id]['amendments']
            assert text not in first
        printed = post_threads.render(post_threads.whole(svc.store, message.id, seat.name))
        assert message.body in printed
        assert live["pasted"] == live["submitted"] == 0
        append(path, user(text, 10))
        restarted.reconcile()
        assert input_receipt.confirmed("claude", path, text, AT + timedelta(seconds=1)) == "delivered"
        found = [item for item in claude.inputs(path) if item.text == text]
        assert len(found) == 1 and found[0].at == AT + timedelta(seconds=2)
        assert found[0].consumed_at == AT + timedelta(seconds=10)
        assert svc.attempts.latest()[message.id] == "delivered"
        assert seat._queued_unread(svc.store, svc.attempts) == []
    finally:
        restarted.ledger.close()


def test_multiple_rows_pop_identical_fifo_slots_and_preserve_unrelated_queue(tmp_path):
    path = record(tmp_path, *working(), queue(FIRST, 2), queue(OTHER, 3), queue(FIRST, 4),
                  queue(FIRST, 5, "popAll"))
    assert claude.state(path).queued == (OTHER, FIRST)
    assert input_receipt.confirmed("claude", path, FIRST, AT + timedelta(seconds=4)) == "queued"
    append(path, queue(FIRST, 6, "popAll"), user(FIRST, 10))
    assert claude.state(path).queued == (OTHER,)
    consumed = [item for item in claude.inputs(path) if item.kind == "consumed" and item.text == FIRST]
    assert len(consumed) == 1 and consumed[0].at == AT + timedelta(seconds=2)
    assert input_receipt.confirmed("claude", path, FIRST, AT + timedelta(seconds=4)) is None
    append(path, user(FIRST, 11))
    assert input_receipt.confirmed("claude", path, FIRST, AT + timedelta(seconds=4)) == "delivered"
    assert claude.inputs(path).diagnostics()["total"] == 0


def test_submission_of_returned_item_does_not_take_identical_still_queued_slot(tmp_path):
    path = record(tmp_path, queue(FIRST, 2), queue(FIRST, 3), queue(FIRST, 4, "popAll"), user(FIRST, 5))
    assert claude.state(path).queued == (FIRST,)
    assert input_receipt.confirmed("claude", path, FIRST, AT + timedelta(seconds=3)) == "queued"
    append(path, queue(FIRST, 6, "popAll"))
    assert claude.state(path).queued == () and claude.state(path).queue_error is None
    assert input_receipt.confirmed("claude", path, FIRST, AT + timedelta(seconds=3)) is None


@pytest.mark.parametrize("content", [None, [], "Synthetic absent", ""])
def test_unresolved_pop_keeps_known_execution_facts_and_stays_unknown(tmp_path, content):
    event = row("queue-operation", 4, operation="popAll")
    if content is not None:
        event["content"] = content
    path = record(tmp_path, *working(), queue(FIRST, 2), event)
    state = claude.state(path)
    assert state.queue_error == "popAll identity unresolved"
    assert state.queued == (FIRST,) and state.consumed == ()  # candidate history only
    assert state.turn_open is True and state.pending_tools == ("Bash",)
    assert claude.inputs(path).diagnostics()["counts"] == [dict(
        kind="queue-operation", reason="popAll identity unresolved", count=1)]
    seat, calls = seat_for(path, "claude")
    active = watch.activity(seat)
    assert active.state == "working" and "Bash" in active.detail and "queue" in active.detail
    append(path, queue(FIRST, 5, "popAll"), *finish())
    assert claude.state(path).queue_error == state.queue_error
    idle = watch.activity(seat)
    assert idle.state == "idle" and "queue" in idle.detail and "unresolved" in idle.detail
    assert "1 queued" not in idle.detail
    # Explicit lifecycle recovery is not vetoed by queue_error after real completion.
    assert Service.relaunch(None, seat, True) == "restarted" and calls == ["restart"]
    other = tmp_path / "different-record"
    append(other, *working(), *finish())
    assert claude.state(other).queue_error is None


def test_repeated_unmatched_and_empty_pops_are_unresolved_not_noops(tmp_path):
    path = record(tmp_path, queue(FIRST, 2), queue(FIRST, 3, "popAll"))
    assert claude.state(path).queued == () and claude.state(path).queue_error is None
    append(path, queue(FIRST, 4, "popAll"))
    assert claude.state(path).queue_error and claude.state(path).consumed == ()
    empty = tmp_path / "empty"
    append(empty, queue(FIRST, 4, "popAll"))
    assert claude.state(empty).queue_error
    blank = tmp_path / "blank"
    append(blank, queue("", 2), queue("", 4, "popAll"))
    assert claude.state(blank).queue_error


def test_sidechain_pop_does_not_touch_root_queue_or_create_error(tmp_path):
    path = record(tmp_path, *working(), queue(FIRST, 2), queue(FIRST, 4, "popAll", isSidechain=True),
                  row("queue-operation", 5, operation="popAll", isSidechain=True))
    assert claude.state(path).queued == (FIRST,) and claude.state(path).queue_error is None
    assert input_receipt.confirmed("claude", path, FIRST, AT + timedelta(seconds=1)) == "queued"
    assert claude.inputs(path).diagnostics()["total"] == 0


@pytest.mark.parametrize("stamp", [None, "2030-01-01T00:00:00+00:00"])
def test_return_cannot_make_stale_or_untimed_acceptance_new(tmp_path, stamp):
    enqueue = queue(FIRST, 2)
    enqueue["timestamp"] = stamp
    path = record(tmp_path, enqueue, queue(FIRST, 4, "popAll"), user(FIRST, 10))
    assert input_receipt.confirmed("claude", path, FIRST, AT + timedelta(seconds=1)) is None


def test_old_echo_and_later_enqueue_keep_their_original_identity(tmp_path):
    path = record(tmp_path, queue(FIRST, 1), queue(FIRST, 2, "remove"), queue(FIRST, 3),
                  queue(FIRST, 4, "popAll"), user(FIRST, 5))
    assert input_receipt.confirmed("claude", path, FIRST, AT + timedelta(seconds=3)) is None
    append(path, user(FIRST, 6))
    assert input_receipt.confirmed("claude", path, FIRST, AT + timedelta(seconds=3)) == "delivered"
    append(path, queue(FIRST, 7))
    assert claude.state(path).queued == (FIRST,)
    assert input_receipt.confirmed("claude", path, FIRST, AT + timedelta(seconds=7)) == "queued"


def test_merged_or_edited_draft_has_no_substring_delivery(tmp_path):
    path = record(tmp_path, queue(FIRST, 2), queue(REPORT, 3), queue(FIRST, 4, "popAll"),
                  queue(REPORT, 4, "popAll"), user(FIRST + "\n" + REPORT, 10))
    assert all(input_receipt.confirmed("claude", path, text, AT) is None for text in (FIRST, REPORT))
    assert claude.state(path).queued == () and claude.state(path).queue_error is None


def test_wrapped_exact_content_pops_and_later_submission_retains_acceptance(tmp_path):
    wrapped = '<pasted_content id="synthetic">\n' + FIRST + '\n</pasted_content>'
    path = record(tmp_path, queue(wrapped, 2), queue(FIRST, 4, "popAll"), user(wrapped, 10))
    assert claude.state(path).queued == ()
    found = [item for item in claude.inputs(path) if item.text == FIRST]
    assert len(found) == 1 and found[0].at == AT + timedelta(seconds=2)
    assert input_receipt.confirmed("claude", path, FIRST, AT + timedelta(seconds=3)) is None


def test_unknown_operation_retains_warning_and_no_receipt(tmp_path):
    path = record(tmp_path, queue(FIRST, 2), queue(FIRST, 4, "synthetic-unknown"))
    assert claude.inputs(path).diagnostics()["counts"][0]["reason"] == "unknown queue operation"
    assert input_receipt.confirmed("claude", path, FIRST, AT) is None


@pytest.mark.parametrize("withdrawn", [False, True])
def test_known_empty_queue_preserves_tool_and_turn_completion_then_relaunch(tmp_path, withdrawn):
    from .test_check_lifetime import synthetic_ship
    path = record(tmp_path, *working(), queue(FIRST, 2), queue(FIRST, 4, "popAll"))
    seat, calls = seat_for(path, "claude")
    svc = Service(synthetic_ship(tmp_path / "ship"), None)
    svc.seat = lambda _: seat
    requests = svc.ship.runtime / "requests"
    requests.mkdir(parents=True)
    request = requests / "synthetic-relaunch.json"
    request.write_text(json.dumps(dict(kind="relaunch", seat="worker", resume=True)))
    if withdrawn:
        request.unlink()
    try:
        svc.requests()
        assert not calls and request.exists() is (not withdrawn)
        append(path, *finish()[:-1])
        svc.requests()
        assert not calls
        append(path, finish()[-1])
        svc.requests()
        assert calls == ([] if withdrawn else ["restart"])
        assert not request.exists()
    finally:
        svc.ledger.close()


@pytest.mark.usefixtures('synthetic_disk_room')  # the machine's disk is not this test's subject
def test_queue_unknown_is_visible_and_holds_only_proved_empty_queue_actions(setup, monkeypatch):
    svc, original, _, _ = setup
    path = original.home / "claude.jsonl"
    append(path, *working(), row("queue-operation", 4, operation="popAll"), *finish())
    seat = bind(svc, original, "claude", path)
    svc.ship.seats[seat.name] = replace(seat.spec, fresh="after-order")
    seat = svc.seat(seat.name)
    assert "queue" in svc.not_idle(seat) and "unresolved" in svc.not_idle(seat)
    order = svc.store.new("order", "desk", seat.name, "Synthetic work")
    monkeypatch.setattr(post, "now", lambda: AT + timedelta(seconds=8))
    svc.store.new("report", seat.name, "desk", "Synthetic done", re=order.id)
    calls = []
    monkeypatch.setattr(svc, "resolved_seats", lambda: [seat])
    monkeypatch.setattr(seat, "fresh", lambda: calls.append("fresh"))
    svc.fresh_after_order()
    assert calls == []
    facts = watch.facts(svc.ship, svc.tmux, walls=[], login=[])
    assert "worker" in facts.line() and "popAll identity unresolved" in facts.line()
    assert "popAll identity unresolved" in "\n".join(watch.render_full(watch.seat_reading(seat)))
    # The same otherwise eligible state with a proved queue does fresh; the hold is specific.
    state = claude.state(path)
    monkeypatch.setattr(seat.harness, "state", lambda _: replace(state, queue_error=None))
    svc.fresh_after_order()
    assert calls == ["fresh"]


def test_queue_unknown_survives_status_overflow_as_its_own_category():
    facts = watch.Facts(service="ok", waiting=0, oldest=0, blind=0,
                        queue_unknown=["synthetic-seat: queue unknown: " + "unresolved " * 50],
                        usage=[dict(pool="synthetic", window="synthetic", reading=None, reason="unknown")])
    assert "1 queue unknown" in facts.line() and "qd status" in facts.line()


@pytest.mark.parametrize("operation", ["dequeue", "synthetic-unknown"])
def test_older_pending_match_prevents_identifying_a_newer_queued_slot(tmp_path, operation):
    ambiguous = row("queue-operation", 3, operation=operation)
    if operation == "synthetic-unknown":
        ambiguous["content"] = FIRST
    path = record(tmp_path, queue(FIRST, 1), queue(OTHER, 2), ambiguous,
                  queue(FIRST, 4), queue(FIRST, 5, "popAll"))
    state = claude.state(path)
    assert state.queue_error == "popAll identity unresolved"
    assert state.queued == (FIRST, OTHER, FIRST)  # no guessed target removed from candidates
    found = claude.inputs(path)
    assert any(item["reason"] == "popAll identity unresolved" for item in found.diagnostics()["counts"])
    assert not any(item.kind == "consumed" for item in found)
    assert input_receipt.confirmed("claude", path, FIRST, AT + timedelta(seconds=4)) == "queued"
    append(path, user(FIRST, 6))
    # Real input still resolves the oldest acceptance, not the newer attempt.
    assert input_receipt.confirmed("claude", path, FIRST, AT + timedelta(seconds=4)) == "queued"
    consumed = [item for item in claude.inputs(path) if item.kind == "consumed"]
    assert len(consumed) == 1 and consumed[0].at == AT + timedelta(seconds=1)
    assert claude.state(path).queue_error == state.queue_error


def test_human_claude_automatic_status_never_reads_conversation(setup, monkeypatch):
    svc, seat, _, _ = setup
    svc.ship.seats[seat.name] = replace(seat.spec, harness="claude")
    svc.ship.sessions[seat.spec.session] = replace(seat.session, human=True)
    def forbidden(*args, **kwargs):
        pytest.fail("automatic human status must use owned metadata, not conversation")
    monkeypatch.setattr(Seat, "record", forbidden)
    monkeypatch.setattr(claude, "state", forbidden)
    monkeypatch.setattr(claude, "inputs", forbidden)
    facts = watch.facts(svc.ship, svc.tmux, walls=[], login=[])
    assert facts.queue_unknown == []
