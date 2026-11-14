"""The same id twice is one message: the brief says so, and a second delivery's header says it is one."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from quarterdeck import input_receipt, post, standing
from quarterdeck.harness.types import Input

AT = datetime(2031, 1, 1, tzinfo=timezone.utc)


def message():
    return post.Message("ord-20310101-0000-issuer-q7r2", "order", "issuer", "worker", AT, "synthetic work q", None)


def test_a_first_delivery_has_no_second_line_and_a_second_has_it_before_the_same_envelope():
    m = message()
    first, second, *legacy = post.delivered_forms(m)
    assert "second delivery" not in first
    line = f"qd > second delivery of {m.id}; you may have it already: act on it once"
    assert second == line + "\n" + first, "the envelope is unchanged under the one added line"
    assert post.typed([m, m], second=(m.id,)).count(line) == 2 and post.typed([m]) == first


def test_a_receipt_reads_either_form_and_nothing_else():
    m = message()
    first, second, *legacy = post.delivered_forms(m)
    for typed_form in (first, second, *legacy):
        harness = SimpleNamespace(inputs=lambda record, text=typed_form: [Input(text, AT + timedelta(seconds=1), "accepted")])
        assert input_receipt.confirmed(harness, None, post.delivered_forms(m), AT) == "delivered"
    other = SimpleNamespace(inputs=lambda record: [Input("synthetic other text q", AT + timedelta(seconds=1), "accepted")])
    assert input_receipt.confirmed(other, None, post.delivered_forms(m), AT) is None


def test_every_brief_says_a_message_twice_is_one_message(drydock):
    from quarterdeck import ship as shipmod
    ship = shipmod.load(drydock)
    for spec in ship.seats.values():
        text = standing.brief(ship, spec).text
        assert "A message that reaches you twice with the same id is one message: read it once and act on it once." in text
