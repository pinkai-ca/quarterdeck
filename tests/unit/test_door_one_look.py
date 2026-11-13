"""The door's one look at a delivery it cannot confirm, and qd submit. Synthetic Codex seat, stand-in tmux."""

from datetime import timedelta

import pytest

from quarterdeck import idle, pending, post
from .test_pending_delivery import input_row, send, setup  # noqa: F401


def unconfirmed(svc, seat, monkeypatch):
    """A delivery submitted and never confirmed, past the door's wait: what the door cannot confirm."""
    message, proof = send(svc, seat)
    later = post.now() + timedelta(seconds=pending.WAIT_SECONDS + 5)
    monkeypatch.setattr(post, "now", lambda: later)
    assert proof.reading(seat, None)[1] == "needs a human"
    return message, proof


def looks(proof):
    return pending.Proof(proof.path, __import__("json").loads(proof.path.read_text())).data.get("look")


def test_the_record_shows_it_so_it_counts_as_delivered(setup, monkeypatch):
    svc, seat, live, record = setup
    message, proof = unconfirmed(svc, seat, monkeypatch)
    input_row(record, post.typed([message], second=(message.id,)), post.now())  # recorded in its second form
    assert pending.one_look(svc, seat) == [f"{message.short} to worker: delivered (the record shows it)"]
    assert looks(proof)["outcome"] == "delivered" and svc.attempts.latest()[message.id] == "delivered"
    assert live["pasted"] == 1 and live["submitted"] == 1, "nothing typed or pressed"


def test_its_exact_text_still_in_the_box_is_submitted_once(setup, monkeypatch):
    svc, seat, live, record = setup
    message, proof = unconfirmed(svc, seat, monkeypatch)
    assert live["box"], "the paste still sits in the box"
    [line] = pending.one_look(svc, seat)
    assert "submitted from the box" in line and live["submitted"] == 2 and live["pasted"] == 1
    assert looks(proof)["outcome"] == "submitted"
    assert pending.one_look(svc, seat) == [] and live["submitted"] == 2, "the one look is never repeated"


def test_an_empty_box_an_idle_seat_and_no_record_types_it_once_more_with_the_same_id(setup, monkeypatch):
    svc, seat, live, record = setup
    message, proof = unconfirmed(svc, seat, monkeypatch)
    live["box"] = ""
    monkeypatch.setattr(idle, "reading", lambda seat, **kwargs: (None, None))  # the one idle proof, standing in
    [line] = pending.one_look(svc, seat)
    assert "typed once more" in line and live["pasted"] == 2
    assert live["box"].startswith(f"qd > second delivery of {message.id}; you may have it already: act on it once")
    assert looks(proof)["outcome"] == "retyped"
    second = [p for p in pending.journals(seat)[0] if p.path != proof.path]
    assert second and second[0].data["look"]["outcome"] == "second delivery"
    live["box"] = ""
    pending.one_look(svc, seat)
    assert live["pasted"] == 2, "never typed again by itself"


def test_anything_else_stays_held_and_the_look_is_journalled(setup, monkeypatch):
    svc, seat, live, record = setup
    message, proof = unconfirmed(svc, seat, monkeypatch)
    live["box"] = "synthetic foreign draft"
    assert pending.one_look(svc, seat) == []
    assert looks(proof)["outcome"] == "held" and live["pasted"] == 1 and live["submitted"] == 1
    assert proof.reading(seat, None)[1] == "needs a human", "held as today"


def test_qd_submit_presses_only_the_exact_pending_text(setup, monkeypatch):
    svc, seat, live, record = setup
    message, proof = unconfirmed(svc, seat, monkeypatch)
    live["box"] = "synthetic other text"
    assert pending.submit(svc, seat) == "refused: worker's box shows 'synthetic other text', not exactly a pending message of worker"
    live["box"] = ""
    assert pending.submit(svc, seat) == "refused: worker's box is empty"
    assert live["submitted"] == 1
    live["box"] = proof.data["payload"].rstrip("\n")
    line = pending.submit(svc, seat)
    assert line.startswith(f"{message.short} to worker: submitted from the box") and live["submitted"] == 2


def test_the_service_answers_a_submit_request_through_the_same_path(setup, monkeypatch):
    svc, seat, live, record = setup
    unconfirmed(svc, seat, monkeypatch)
    live["box"] = ""
    assert svc.answer({"kind": "submit", "seat": "worker"}) == "refused: worker's box is empty"
