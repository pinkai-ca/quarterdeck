"""Post: immutable files, what closes what, the typed form, the inbox."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from quarterdeck import post
from quarterdeck.post import Message, Store
from quarterdeck.ship import ShipError


def test_a_message_is_one_immutable_file(tmp_path: Path):
    store = Store(tmp_path)
    order = store.new("order", "desk", "maker", "Build the Codex profile.", subject="Codex profile")
    files = list((tmp_path / "post" / "store").glob("*.json"))
    assert files == [tmp_path / "post" / "store" / f"{order.id}.json"]
    data = json.loads(files[0].read_text())
    assert data["from"] == "desk" and data["to"] == "maker" and data["kind"] == "order" and data["re"] is None
    assert order.id.startswith("ord-") and order.short == f"ord-{order.id[-6:]}"
    with pytest.raises(ShipError, match="immutable"):
        store.write(order)  # law: post-is-files
    assert store.get(order.short) == order and store.get(order.id) == order
    assert Message.from_json(order.to_json()) == order


def test_only_a_report_from_the_addressee_closes_an_order(tmp_path: Path):
    store = Store(tmp_path)
    order = store.new("order", "desk", "maker", "Do the thing.")
    assert store.is_open(order) and store.open_for("maker") == [order]
    store.new("ack", "maker", "desk", "on it", re=order.short)
    assert store.is_open(order), "an ack claims, it never closes"  # law: open-until-closed
    store.new("report", "chair", "desk", "I did it for him", re=order.id)
    assert store.is_open(order), "a report from anyone but the addressee closes nothing"
    report = store.new("report", "maker", "desk", "done", re=order.short)
    assert not store.is_open(order) and store.closing_report(order) == report
    assert report.re == order.id, "a short reference is stored as the full id"
    assert store.open_for("maker") == []


def test_a_question_is_open_until_answered(tmp_path: Path):
    store = Store(tmp_path)
    question = store.new("question", "maker", "desk", "Which branch?")
    assert store.open_for("desk") == [question]
    store.new("answer", "desk", "maker", "main", re=question.id)
    assert store.open_for("desk") == []


def test_refusals(tmp_path: Path):
    store = Store(tmp_path)
    with pytest.raises(ShipError, match="no message kind"):
        store.new("memo", "a", "b", "x")
    with pytest.raises(ShipError, match="no speed"):
        store.new("order", "a", "b", "x", speed="fast")
    with pytest.raises(ShipError, match="no message 'ord-0000'"):
        store.get("ord-0000")


def test_the_typed_form(tmp_path: Path):
    store = Store(tmp_path)
    order = store.new("order", "desk", "maker", "Build the Codex profile of the fake harness.\nReport when its contract tests pass.",
                      subject="Codex profile of the fake harness", due="2026-11-08 18:00")
    text = post.typed([order])
    assert text == (
        f"qd > order from desk · {order.short} · Codex profile of the fake harness · due 2026-11-08 18:00\n"
        "Build the Codex profile of the fake harness.\nReport when its contract tests pass.\n"
        f"qd > reply: qd post report --re {order.short}\n"
    )
    report = store.new("report", "maker", "desk", "done", re=order.id)
    text = post.typed([report])
    assert text.startswith(f"qd > report from maker · {report.short} · re {order.short}\ndone\n")
    assert post.typed([order, report]).count("qd > ") == 3, "several messages are one paste, each under its own header"


def test_the_briefed_header():
    store_less = [Message("ord-1-a-0001", "order", "d", "s", post.now(), "x"), Message("que-1-a-0002", "question", "d", "s", post.now(), "y")]
    assert post.briefed_header("maker", "engineering", store_less) == "qd > briefed as maker, engineering · 1 order open, 1 question open"
    assert post.briefed_header("maker", "engineering", []) == "qd > briefed as maker, engineering · nothing open"


def test_the_inbox_shows_open_obligations_first(tmp_path: Path):
    store = Store(tmp_path)
    old = store.new("order", "desk", "maker", "old order")
    store.new("report", "maker", "desk", "done", re=old.id)
    new = store.new("order", "chair", "maker", "new order", subject="Now")
    note = store.new("report", "chair", "maker", "a report to read")
    path = post.render_inbox(tmp_path, store, "maker")
    text = path.read_text()
    assert path == tmp_path / "post" / "inbox" / "maker.md"
    assert text.index("## Open (1)") < text.index(f"order {new.short}") < text.index("## Received (2)")
    assert text.index(f"report {note.short}") < text.index(f"order {old.short}"), "newest received first"
    assert f"open · reply: qd post report --re {new.short}" in text and "closed by rep-" in text


def test_an_attempt_still_open_holds_its_message_back(tmp_path: Path):
    store, attempts = Store(tmp_path), post.Attempts(tmp_path)
    order = store.new("order", "desk", "maker", "once only")
    assert attempts.undelivered(store, "maker") == [order]
    attempt = attempts.open(order, "maker", "%1", 42, "2026-11-07T00:00:00+00:00")
    assert attempts.undelivered(store, "maker") == [], "being judged, never sent again"  # law: one-door
    attempts.close(attempt, "blind", "never confirmed")
    assert attempts.undelivered(store, "maker") == [] and attempts.blind_ids() == {order.id}
    again = store.new("order", "desk", "maker", "a second")
    assert attempts.undelivered(store, "maker") == [again]
    attempts.close(attempts.open(again, "maker", "%1", 42, None), "delivered")
    assert attempts.undelivered(store, "maker") == [] and attempts.accepted_ids() == {again.id}


def test_queued_is_a_state_of_its_own_and_an_ack_is_never_delivered(tmp_path: Path):
    store, attempts = Store(tmp_path), post.Attempts(tmp_path)
    order = store.new("order", "desk", "maker", "mid-turn")
    attempts.close(attempts.open(order, "maker", "%1", 1, None), "queued", "queued in the record")
    assert attempts.queued_ids() == {order.id} and attempts.undelivered(store, "maker") == []
    attempts.close(attempts.open(order, "maker", "%1", 1, None), "delivered", "read")
    assert attempts.queued_ids() == set(), "the latest attempt speaks"
    assert attempts.last_results("maker") == {order.id: "delivered"}
    ack = store.new("ack", "maker", "desk", "claimed", re=order.id)
    assert ack not in attempts.undelivered(store, "desk"), "an ack claims and never wakes"  # law: open-until-closed
    assert ack in store.for_seat("desk") and f"ack {ack.short}" in post.inbox_text(store, "desk"), "but the inbox shows it"


def test_the_store_reads_its_folder_once_until_a_file_arrives(tmp_path: Path):
    store = Store(tmp_path)
    first = store.new("order", "desk", "maker", "one")
    assert store.all() == [first]
    cached = store.all()
    assert cached == [first] and cached is not store._cache[1], "callers get a copy"
    second = store.new("order", "desk", "maker", "two")
    assert store.all() == [first, second], "a new file invalidates the cache"
    reply = store.new("report", "maker", "desk", "done", re=first.id)
    assert store.replies(first) == [reply] and store.replies(second) == [], "replies come from the same reading, by id"


def test_the_services_verdict_never_overwrites_a_result_the_watch_wrote(tmp_path: Path):
    store, attempts = Store(tmp_path), post.Attempts(tmp_path)
    question = store.new("question", "operator", "desk", "Which branch?")
    opened = attempts.open(question, "desk", None, None, None)
    assert attempts.judge(opened, "blind", "a watch opened it and never reported printing it") is True
    assert attempts.for_message(question.id)[0].result == "blind"
    again = attempts.open(question, "desk", None, None, None)
    post.Attempts(tmp_path).close(again, "watch", "printed by qd inbox --wait")  # the watch, in its own process
    assert attempts.judge(again, "blind", "a late verdict") is False
    assert attempts.for_message(question.id)[-1].result == "watch", "the result the watch wrote stands"  # law: one-door


def test_the_watch_hands_back_open_attempts_to_close_after_printing(tmp_path: Path):
    store = Store(tmp_path)
    question = store.new("question", "operator", "desk", "Which branch?")
    text, opened = post.wait(tmp_path, "desk", interval=0.05, stop_after=2)
    assert question.short in text and [a.result for a in opened] == [None]
    assert not post.watch_file(tmp_path, "desk").exists(), "unregistered once it returns"
    assert post.Attempts(tmp_path).undelivered(store, "desk") == [], "held while the print is in flight"
    post.printed(tmp_path, opened)
    assert post.Attempts(tmp_path).for_message(question.id)[0].result == "watch"
    assert post.wait(tmp_path, "desk", interval=0.05, stop_after=0.2) is None


def test_claimed_order_for_attribution(tmp_path: Path):
    store = Store(tmp_path)
    a = store.new("order", "desk", "maker", "a")
    b = store.new("order", "desk", "maker", "b")
    assert store.claimed("maker", post.now()) is None
    store.new("ack", "maker", "desk", "on a", re=a.id)
    assert store.claimed("maker", post.now()) == a.id
    store.new("ack", "maker", "desk", "now b", re=b.id)
    assert store.claimed("maker", post.now()) == b.id
    report = store.new("report", "maker", "desk", "b is done", re=b.id)
    assert store.claimed("maker", post.now()) == a.id, "a closed order is no longer the claim; the earlier open one is"
    assert store.claimed("maker", report.at - timedelta(microseconds=1)) == b.id, "but it was, back when the turn ran"
    store.new("report", "maker", "desk", "a is done too", re=a.id)
    assert store.claimed("maker", post.now()) is None


def test_an_acked_question_is_claimed_until_it_is_answered_and_no_longer(tmp_path: Path):
    store = Store(tmp_path)
    question = store.new("question", "chair", "desk", "which branch?")
    ack = store.new("ack", "desk", "chair", "looking", re=question.id)
    assert store.claimed("desk", ack.at) == question.id
    answer = store.new("answer", "desk", "chair", "main", re=question.id)
    assert store.claimed("desk", answer.at - timedelta(microseconds=1)) == question.id, "claimed before its answer"
    assert store.claimed("desk", answer.at) is None, "an answered question claims nothing, by is_open's own rule"
    assert store.claimed("desk", post.now()) is None and not store.is_open(question)
    assert store.closing(question) == answer


def test_a_claim_on_a_message_that_is_never_open_is_no_claim(tmp_path: Path):
    store = Store(tmp_path)
    report = store.new("report", "desk", "maker", "done")
    store.new("ack", "maker", "desk", "seen", re=report.id)
    assert store.claimed("maker", post.now()) is None and store.closing(report) == report
