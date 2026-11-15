"""A jailed seat's view is refreshed in the filing step itself: what it posted is in its drop or its view, never neither.

A jailed seat sees the post only through its view. If the service filed its drop and refreshed its view only at the
pass's later views step, a message would for that while be in neither, and the seat's own repeat guard could not find
it. A synthetic drydock copy, its worker-1 jailed; no program runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quarterdeck import post, post_view, ship as shipmod
from quarterdeck.service import Service


@pytest.fixture
def svc(drydock: Path):
    path = drydock / "ship.toml"
    path.write_text(shipmod.edit(path.read_text(), "seats.worker-1", "sandbox", True))
    service = Service(shipmod.load(drydock), None)
    assert service.ship.jailed(service.ship.seat("worker-1")) and not service.ship.jailed(service.ship.seat("captain"))
    yield service
    service.ledger.close()


def posted(svc: Service, seat: str, re: post.Message, body: str = "done") -> post.Message:
    """A report written as qd post writes it: one file in the seat's own drop, not yet filed."""
    message = svc.store.prepare("report", seat, re.sender, body, re=re.id)
    post.write_drop(svc.runtime, message)
    return message


def file(svc: Service, seat: str = "worker-1", tries: int = 50) -> None:
    """The service's filing step until the seat's drop holds no message: each call is one bounded slice."""
    for _ in range(tries):
        svc.file_drops()
        if not any(post.drop_dir(svc.runtime, seat).glob("*-*.json")):
            return
    raise AssertionError("the drop never emptied")


def in_view(svc: Service, seat: str, message: post.Message) -> bool:
    return (post_view.folder(svc.runtime, seat) / "store" / f"{message.id}.json").is_file()


def in_drop(svc: Service, seat: str, message: post.Message) -> bool:
    return (post.drop_dir(svc.runtime, seat) / f"{message.id}.json").is_file()


def test_a_report_filed_in_a_pass_is_in_the_jailed_seats_view_before_the_passs_next_step(svc, monkeypatch):
    order = svc.store.new("order", "captain", "worker-1", "build it")
    post_view.refresh(svc.runtime, "worker-1")  # the view as it stood when the order was typed
    report = posted(svc, "worker-1", order)
    assert in_drop(svc, "worker-1", report) and not in_view(svc, "worker-1", report)
    seen = []
    # Before every duty of the pass up to the deliveries, record where the report is: in the drop, in the view, or in
    # neither. The pass drains the drops before its first duty and after each, through the same filing step.
    # The filing step itself is not watched: the drains call it too, and each call is where the report may leave.
    steps = ("server_recovery", "reconcile", "groups", "cascades", "fleet_post", "inboxes", "views",
             "recoveries", "deliveries")
    for name in steps:
        step = getattr(svc, name)
        def watched(step=step, name=name):
            seen.append((name, in_drop(svc, "worker-1", report), in_view(svc, "worker-1", report)))
            return step()
        monkeypatch.setattr(svc, name, watched)
    svc.tick()
    assert [name for name, *_ in seen] == [name for name in steps], seen
    assert all(dropped or viewed for _, dropped, viewed in seen), f"in neither drop nor view at a step: {seen}"
    filed = next(i for i, (_, dropped, _) in enumerate(seen) if not dropped)
    assert filed < steps.index("views") and seen[filed][2], f"in the view at the first step after its filing: {seen}"
    assert svc.store.get(report.id).body == "done"


def test_only_a_seat_whose_drop_a_message_left_is_refreshed_and_only_then(svc, monkeypatch):
    order = svc.store.new("order", "captain", "worker-1", "build it")
    post_view.refresh(svc.runtime, "worker-1")
    refreshed = []
    original = post_view.refresh
    monkeypatch.setattr(post_view, "refresh",
                        lambda runtime, seat, snapshot=None: refreshed.append(seat) or original(runtime, seat, snapshot))
    snapshots = []
    real_snapshot = post_view.Snapshot
    monkeypatch.setattr(post_view, "Snapshot", lambda runtime: snapshots.append(1) or real_snapshot(runtime))
    svc.file_drops()
    assert refreshed == [] and snapshots == [], "nothing left any drop: no store listing for the views"
    # An open seat's filed message refreshes no view; a jailed seat's does, its own alone.
    open_report = svc.store.prepare("order", "captain", "desk", "an open seat's post")
    post.write_drop(svc.runtime, open_report)
    file(svc, "captain")
    assert refreshed == [] and snapshots == [] and svc.store.get(open_report.id) is not None
    report = posted(svc, "worker-1", order)
    file(svc)
    assert refreshed == ["worker-1"] and snapshots == [1] and in_view(svc, "worker-1", report)


def test_a_held_drop_refreshes_no_view_and_a_failing_view_never_stops_the_filing(svc, monkeypatch):
    order = svc.store.new("order", "captain", "worker-1", "build it")
    post_view.refresh(svc.runtime, "worker-1")
    junk = post.drop_dir(svc.runtime, "worker-1") / "not-a-message.json"
    junk.parent.mkdir(parents=True, exist_ok=True)
    junk.write_text("{")
    refreshed = []
    monkeypatch.setattr(post_view, "refresh", lambda runtime, seat, snapshot=None: refreshed.append(seat))
    for _ in range(5):  # slices enough to reach the held file
        svc.file_drops()
    assert junk.is_file() and refreshed == [], "a held file stays in the drop: nothing left it"
    junk.unlink()

    def broken(runtime, seat, snapshot=None):
        raise OSError("synthetic view failure")
    monkeypatch.setattr(post_view, "refresh", broken)
    report = posted(svc, "worker-1", order)
    file(svc)
    assert svc.store.get(report.id) is not None and not in_drop(svc, "worker-1", report), "filed all the same"
    monkeypatch.setattr(svc, "_viewed_seats", lambda: (_ for _ in ()).throw(RuntimeError("synthetic")))
    second = posted(svc, "worker-1", order, body="again, other words")
    file(svc)
    assert svc.store.get(second.id) is not None, "a failure to list the viewed seats never stops the filing"
