"""A negative integration assertion must wait for duties, not just a heartbeat."""

import threading

import pytest

from quarterdeck import service, ship as shipmod
from tests.integration.progress import service_progress


def test_progress_does_not_pass_a_heartbeat_before_a_blocked_delivery(drydock, monkeypatch):
    ship = shipmod.load(drydock)
    svc = service.Service(ship, None)
    entered, release = threading.Event(), threading.Event()
    counts = {"deliveries": 0, "failover": 0}

    def deliveries():
        counts["deliveries"] += 1
        entered.set()
        assert release.wait(3), "test failed to release the service duty"

    def failover():
        counts["failover"] += 1

    # Keep the real tick, request duty and heartbeat; no panes or providers exist.
    for name in ("reconcile", "account", "inboxes", "fresh_after_order", "rebrief", "credentials"):
        monkeypatch.setattr(svc, name, lambda: None)
    monkeypatch.setattr(svc, "deliveries", deliveries)
    monkeypatch.setattr(svc, "failover", failover)
    # SQLite belongs to the thread that constructs it; these duties do not use it.
    svc.ledger.close()
    tick = threading.Thread(target=svc.tick)
    tick.start()
    try:
        assert entered.wait(2)
        assert service.read_heartbeat(ship.runtime) is not None
        with pytest.raises(AssertionError, match="condition not met"):
            service_progress(ship, seconds=0.1)
    finally:
        release.set()
        tick.join(3)
    assert not tick.is_alive()

    errors = []
    def observe():
        try:
            service_progress(ship, seconds=2)
        except Exception as error:
            errors.append(error)
    observer = threading.Thread(target=observe)
    observer.start()
    before = dict(counts)
    # Each call is a real duty pass; the observer waits for two distinct answers.
    for _ in range(100):
        svc.tick()
        observer.join(0.01)
        if not observer.is_alive():
            break
    observer.join(3)
    assert not observer.is_alive() and not errors
    assert counts["deliveries"] >= before["deliveries"] + 2
    assert counts["failover"] >= before["failover"] + 2
