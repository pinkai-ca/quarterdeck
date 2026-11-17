"""The ruling's case of 12 November: a relaunch, a delivery in its gap, then a second delivery.

The message whose delivery falls in the gap reaches the seat once, through the new launch, and nothing is typed
twice. Real join and lifecycle code on synthetic seats; only the native program and the door's transport are
stand-ins, and the stand-in door records every text it types and the pane it typed into.
"""
from types import SimpleNamespace

from quarterdeck import post
from .test_pending_lifecycle import SyntheticSeat, no_credentials, service_for  # noqa: F401


def test_a_delivery_in_a_relaunchs_gap_arrives_once_through_the_new_launch(tmp_path, monkeypatch, no_credentials):
    seat = SyntheticSeat(tmp_path, "worker")
    service = service_for(tmp_path, seat)
    typed = []

    class Door:
        def __init__(self, seat):
            self.seat = seat

        def reading(self):
            return SimpleNamespace(open=True, clears=None)

        def screen(self):
            return SimpleNamespace(text="", box="")

        def deliver(self, text, speed=None, proof=None):
            typed.append((self.seat.pane().id, text))
            return SimpleNamespace(result="delivered", detail="synthetic: the record shows it")

    monkeypatch.setattr("quarterdeck.service.Door", Door)
    old = seat.launched()
    gap = service.store.new("order", "builder", seat.name, "synthetic order sent in the gap")
    in_gap = []

    def deliver_in_the_gap():
        in_gap.append(service.deliver(seat, [gap]))
    seat.retire_hook = deliver_in_the_gap
    seat.fresh()
    new = seat.launched()
    assert new.at != old.at and in_gap, "the delivery was tried in the relaunch's gap"
    assert typed == [], "nothing is typed while the seat is between launches"
    assert gap.id in [m.id for m in service.attempts.undelivered(service.store, seat.name)]

    service.deliver(seat, [gap])
    second = service.store.new("order", "builder", seat.name, "synthetic second order")
    service.deliver(seat, [second])
    service.deliver(seat, service.attempts.undelivered(service.store, seat.name))  # a later pass types nothing more

    assert [pane for pane, _ in typed] == [new.pane, new.pane], "both through the new launch"
    assert typed[0][1] == post.typed([gap]) and typed[1][1] == post.typed([second]), "each once, in order"
    accepted = [a for a in service.attempts.for_message(gap.id) if a.accepted]
    assert len(accepted) == 1 and accepted[0].launched_at == new.at
    assert [a.result for a in service.attempts.for_message(second.id)] == ["delivered"]
