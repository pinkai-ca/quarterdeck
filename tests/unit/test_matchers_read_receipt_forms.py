"""Every matcher reads a message in the forms the receipt reads it in.

The refused turn's culprit (refusal.py) and the failed turn's unsettled attempts (turn_failure.py) name the message
an input was, among the attempts typed on the seat. They read each message as the receipt does, through one helper:
as typed, in the form the adapter measured its program to record it in, and with the characters it drops ignored
on both sides. So none of them misses a form another confirms. Claude Code 2.1.317's two measured paste forms are
the TAB recorded as four spaces and the zero-width space dropped. Synthetic messages and records only; invisible
characters are written as escapes.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from quarterdeck import harness, input_receipt, post, refusal, turn_failure
from quarterdeck.harness.types import Input, TurnFailure
from tests.unit.refusal_ship import World
from .test_claude_draft_return import AT, record, user
from .test_claude_tab_receipt import MEASURED, pasted, request

TAB_BODY = "An invented order with a\ttab in it."
ZERO_WIDTH_BODY = "An invented order naming '\u200b/config' in it."
FORMS = {
    "tab": (TAB_BODY, lambda text: text.replace("\t", "    ")),
    "zero-width": (ZERO_WIDTH_BODY, lambda text: text.replace("\u200b", "")),
}


@pytest.fixture
def world(tmp_path):
    built = World(tmp_path)
    yield built
    built.svc.ledger.close()


def typed_once(world, body):
    """One order the door typed on a Claude seat's launch, and that launch."""
    message = world.order("worker", body)
    launched = "2026-01-01T00:00:00+00:00"
    attempt = world.svc.attempts.open(message, "worker", "%1", 101, launched)
    world.svc.attempts.close(attempt, "delivered", "synthetic receipt")
    return message, {"at": launched}


def seat_with(tmp_path, recorded_text):
    """A Claude seat whose record holds the input as the measured program records it, after the attempt."""
    path = record(tmp_path, request(2), dict(user(pasted(recorded_text), 3), version=MEASURED))
    claude = harness.get("claude")
    seat = SimpleNamespace(name="worker", harness=claude, record=lambda: path)
    failure = TurnFailure("synthetic refusal", AT + timedelta(seconds=5), inputs=(Input(recorded_text, AT + timedelta(seconds=3)),),
                          model_output=False, kind=turn_failure.REFUSAL)
    return seat, failure, path


@pytest.mark.parametrize("form", FORMS)
def test_the_receipt_confirms_the_measured_form(world, tmp_path, form):
    body, recorded = FORMS[form]
    message, _ = typed_once(world, body)
    text = recorded(post.typed([message]))
    assert text != post.typed([message])
    seat, failure, path = seat_with(tmp_path, text)
    assert input_receipt.confirmed(seat.harness, path, post.delivered_forms(message), AT) == "delivered"


@pytest.mark.parametrize("form", FORMS)
def test_the_failed_turns_matching_reads_the_measured_form(world, tmp_path, form):
    body, recorded = FORMS[form]
    message, _ = typed_once(world, body)
    seat, failure, _ = seat_with(tmp_path, recorded(post.typed([message])))
    found = turn_failure.matching(seat, failure, world.svc.store, world.svc.attempts)
    assert [attempt.message for attempt in found] == [message.id]


@pytest.mark.parametrize("form", FORMS)
def test_the_refused_turns_culprit_reads_the_measured_form(world, tmp_path, form):
    body, recorded = FORMS[form]
    message, launch = typed_once(world, body)
    seat, failure, path = seat_with(tmp_path, recorded(post.typed([message])))
    found, why = refusal.culprit(seat, failure, launch, world.svc.store, world.svc.attempts, path)
    assert found is not None and found.id == message.id, why


@pytest.mark.parametrize("form", FORMS)
def test_a_visible_difference_is_matched_by_none(world, tmp_path, form):
    body, recorded = FORMS[form]
    message, launch = typed_once(world, body)
    text = recorded(post.typed([message])).replace("invented", "invened")
    seat, failure, path = seat_with(tmp_path, text)
    assert input_receipt.confirmed(seat.harness, path, post.delivered_forms(message), AT) is None
    assert turn_failure.matching(seat, failure, world.svc.store, world.svc.attempts) == []
    assert refusal.culprit(seat, failure, launch, world.svc.store, world.svc.attempts, path)[0] is None


def test_without_a_record_a_matcher_reads_the_message_as_typed(world, tmp_path):
    message, launch = typed_once(world, TAB_BODY)
    seat, failure, _ = seat_with(tmp_path, post.typed([message]))
    seat.record = lambda: None
    assert [a.message for a in turn_failure.matching(seat, failure, world.svc.store, world.svc.attempts)] == [message.id]
    assert refusal.culprit(seat, failure, launch, world.svc.store, world.svc.attempts, None)[0].id == message.id
