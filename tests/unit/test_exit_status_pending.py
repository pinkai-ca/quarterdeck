"""A closed terminal can precede tmux's reaped exit status."""
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

import pytest

from quarterdeck.seat import Seat
from quarterdeck.service import Service
from quarterdeck.ship import ShipError
from quarterdeck.tmux import Pane
from quarterdeck.watch import activity


def dead(status=None):
    return Pane("%synthetic-pane", "Synthetic crew", "Synthetic worker", 123,
                100, 50, False, True, status, 946684800)


@pytest.mark.parametrize("status,detail", [(None, "exit status not yet known"), (0, "exited 0"), (7, "exited 7")])
def test_dead_activity_names_pending_or_known_status(status, detail):
    seat = SimpleNamespace(pane=lambda: dead(status))
    reading = activity(seat)
    assert reading.state == "off"
    assert reading.detail == detail


@pytest.mark.parametrize("status,detail", [(None, "exited at once; exit status not yet known"),
                                          (0, "exited 0 at once"), (7, "exited 7 at once")])
def test_immediate_exit_keeps_its_refusal_without_printing_none(monkeypatch, status, detail):
    monkeypatch.setattr("quarterdeck.seat.time.sleep", lambda seconds: None)
    queries = []
    def pane(pane_id):
        queries.append(pane_id)
        return dead(status)
    seat = SimpleNamespace(name="synthetic-worker", harness=SimpleNamespace(synthetic=True),
                           tmux=SimpleNamespace(pane=pane, capture=lambda *args, **kwargs: "synthetic output"))
    with pytest.raises(ShipError) as raised:
        Seat._survives(seat, "%synthetic-pane", "synthetic-program")
    text = str(raised.value)
    assert detail in text
    assert "exited None" not in text
    assert "its pane stays for a look" in text
    assert "qd up synthetic-worker tries again" in text
    assert text.endswith("It printed: synthetic output")
    assert queries == ["%synthetic-pane"]


def logger():
    events = []
    service = SimpleNamespace(log=lambda event, **fields: events.append((event, fields)))
    return service, SimpleNamespace(name="synthetic-worker"), events


@pytest.mark.parametrize("status", [0, 7])
def test_death_logs_pending_then_known_once_from_existing_observations(status):
    service, seat, events = logger()
    for value in [None, None, status, status, None, status]:
        Service.pane_died(service, seat, dead(value))
    assert [event for event, fields in events] == ["pane-dead", "pane-dead"]
    assert [fields["status"] for event, fields in events] == [None, status]
    for event, fields in events:
        assert fields == dict(seat=seat.name, pane="%synthetic-pane", pid=123,
                              died=fields["died"], status=fields["status"])
        assert datetime.fromisoformat(fields["died"]).timestamp() == 946684800


def test_known_zero_is_logged_once_and_new_launch_identity_is_independent():
    service, seat, events = logger()
    pane = dead(0)
    Service.pane_died(service, seat, pane)
    Service.pane_died(service, seat, pane)
    Service.pane_died(service, seat, replace(pane, pid=124, exit_status=None))
    Service.pane_died(service, seat, replace(pane, pid=124, exit_status=9))
    Service.pane_died(service, seat, replace(pane, id="%another-synthetic-pane"))
    assert [(fields["pane"], fields["pid"], fields["status"]) for event, fields in events] == [
        (pane.id, 123, 0), (pane.id, 124, None), (pane.id, 124, 9), ("%another-synthetic-pane", 123, 0)]


def test_failed_log_does_not_consume_the_observation():
    service, seat, events = logger()
    saved = service.log
    def fail(*args, **kwargs):
        raise OSError("synthetic log unavailable")
    service.log = fail
    with pytest.raises(OSError):
        Service.pane_died(service, seat, dead())
    service.log = saved
    Service.pane_died(service, seat, dead())
    Service.pane_died(service, seat, dead(0))
    assert [fields["status"] for event, fields in events] == [None, 0]
