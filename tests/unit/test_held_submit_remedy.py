"""A held delivery names the available submit command without acting on it."""

from dataclasses import replace
from datetime import timedelta

import pytest

from quarterdeck import headlines, pending, post, watch
from quarterdeck.door import Door
from .test_pending_delivery import send, setup  # noqa: F401


@pytest.mark.parametrize("box", ["exact", "other", "empty", "ambiguous", "human", "unreadable"])
@pytest.mark.parametrize("phase", ["submitted", "pasting"])
def test_watch_and_status_name_submit_only_for_one_exact_pending_message(setup, monkeypatch, box, phase):
    svc, seat, live, record = setup
    message, proof = send(svc, seat)
    later = post.now() + timedelta(seconds=pending.WAIT_SECONDS + 5)
    monkeypatch.setattr(post, "now", lambda: later)
    proof.save(phase=phase, look=dict(at=later.isoformat(), outcome="held", saw="synthetic held delivery"))
    if box == "other":
        live["box"] = "synthetic other text"
    elif box == "empty":
        live["box"] = ""
    elif box == "ambiguous":
        proof.path.with_name("another.json").write_bytes(proof.path.read_bytes())
    elif box == "human":
        seat.ship.sessions["bridge"] = replace(seat.session, human=True)
    elif box == "unreadable":
        proof.path.write_text("{ broken")

    before = {path: path.read_bytes() for folder in (proof.path.parent, svc.attempts.dir)
              for path in folder.glob("*.json")}
    monkeypatch.setattr(Door, "submit", lambda *args: pytest.fail("a reading must not press submit"))
    monkeypatch.setattr(seat.tmux, "paste", lambda *args: pytest.fail("a reading must not paste"))
    monkeypatch.setattr(seat.tmux, "keys", lambda *args: pytest.fail("a reading must not press keys"))

    reading = watch.reading(seat).door
    facts = watch.facts(svc.ship, svc.tmux, walls=[], login=[])
    status = facts.line()
    detail = '\n'.join(headlines.details(facts))
    hint = "; qd submit worker sends it"
    assert (hint in reading.reason) == (box == "exact")
    assert (hint in detail) == (box == "exact")
    if box == "exact":
        reason = ("pending delivery exceeded 600s; not resent; box and record need inspection"
                  if phase == "submitted" else "paste/submit outcome uncertain; never resent")
        assert reading.reason == f"{reason} (worker, {message.short}); inspect with qd watch worker{hint}"
        assert reading.reason in detail and '1 pending' in status
    assert {path: path.read_bytes() for folder in (proof.path.parent, svc.attempts.dir)
            for path in folder.glob("*.json")} == before
    assert live["pasted"] == live["submitted"] == 1
