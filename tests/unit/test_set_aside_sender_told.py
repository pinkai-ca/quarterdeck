"""A message set aside with its journal is marked, never typed again, and its sender told once (law: nothing-waits-
silently). The same throwaway runtime and synthetic Codex seat as the set-aside act's own tests; no program runs.
"""
import json
import shutil

from quarterdeck import acts, post
from .test_set_aside_journal import aside, archive, setup, stuck  # noqa: F401


def notices(svc, message):
    return [m for m in svc.store.all() if m.kind == "report" and m.sender == post.SHIP and m.re == message.id]


def marks(svc, message):
    return [a for a in svc.attempts.for_message(message.id) if (a.detail or "").startswith(acts.MARK_DETAIL)]


def test_a_held_message_set_aside_is_marked_and_its_sender_told_once(setup):
    svc, seat, live = setup
    message, proof = stuck(svc, seat)
    line = aside(svc, seat, proof.path.name)
    [notice] = notices(svc, message)
    assert notice.to == "builder" and f"Your order {message.short} to worker could not be settled" in notice.body
    assert "a systems seat set its stuck delivery journal aside" in notice.body, "what happened"
    assert "It may not have reached worker." in notice.body, "that it may not have reached its addressee"
    assert "will not type it to worker again: if it matters, send it again" in notice.body
    assert f"marked {acts.MARK_RESULT}, its sender told once ({notice.id})" in line
    [mark] = marks(svc, message)
    assert mark.result == acts.MARK_RESULT and svc.attempts.latest()[message.id] == acts.MARK_RESULT
    assert acts.MARK_RESULT in post.HELD and acts.MARK_RESULT not in post.UNSETTLED
    assert message.id not in [m.id for m in svc.attempts.undelivered(svc.store, seat.name)], "never typed again"
    first = json.loads(acts.path(seat).read_text().splitlines()[0])
    assert first["mark"] is True and first["notice"]["id"] == notice.id, "recorded before the move"


def test_a_second_act_or_any_pass_tells_nothing_more(setup):
    svc, seat, live = setup
    message, proof = stuck(svc, seat)
    aside(svc, seat, proof.path.name)
    for _ in range(3):
        acts.settle_set_asides(svc)
    [moved] = archive(svc)
    again = proof.path.parent / "again.json"  # a second journal of the same message
    shutil.copyfile(moved, again)
    line = aside(svc, seat, again.name)
    acts.settle_set_asides(svc)
    [notice] = notices(svc, message)
    assert f"its sender told once ({notice.id})" in line, "the second act names the same, one notice"
    assert len(marks(svc, message)) == 1


def test_a_message_never_typed_is_neither_marked_nor_told(setup):
    svc, seat, live = setup
    message, proof = stuck(svc, seat)
    [name] = proof.data["attempts"]
    record = post.attempt_path(svc.ship.runtime, name)
    record.write_text(json.dumps(dict(json.loads(record.read_text()), result="withheld", detail="synthetic")))
    line = aside(svc, seat, proof.path.name)
    acts.settle_set_asides(svc)
    assert "the door may deliver it" in line and "marked" not in line
    assert notices(svc, message) == [] and marks(svc, message) == []


def test_an_unidentified_message_gets_no_mark_and_no_notice_but_is_named_in_the_act(setup):
    svc, seat, live = setup
    folder = svc.ship.runtime / "post" / "pending" / seat.name
    folder.mkdir(parents=True)
    (folder / "unreadable.json").write_bytes(b"{not a journal")
    line = aside(svc, seat, "unreadable.json")
    acts.settle_set_asides(svc)
    assert "its message could not be identified" in line
    assert [m for m in svc.store.all() if m.sender == post.SHIP] == []
    first = json.loads(acts.path(seat).read_text().splitlines()[0])
    assert first["message"] is None and first["mark"] is False and first["notice"] is None


def test_a_pass_completes_the_mark_and_the_notice_a_cut_left(setup, monkeypatch):
    svc, seat, live = setup
    message, proof = stuck(svc, seat)
    real = acts._settle_aside

    def cut(*args):
        raise OSError("synthetic cut after the move")
    monkeypatch.setattr(acts, "_settle_aside", cut)
    line = aside(svc, seat, proof.path.name)
    assert "its mark and notice wait for the next pass" in line
    assert notices(svc, message) == [] and marks(svc, message) == []
    monkeypatch.setattr(acts, "_settle_aside", real)
    acts.settle_set_asides(svc)
    acts.settle_set_asides(svc)
    assert len(notices(svc, message)) == 1 and len(marks(svc, message)) == 1
