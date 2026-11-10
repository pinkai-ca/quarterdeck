"""Prove service progress after a test establishes the condition it is checking."""

import json
import time

from .waiting import wait_for


def service_progress(ship, seconds=8):
    """Two request acknowledgments cross a whole service pass, without a fixed sleep.

    The first may belong to a tick already past deliveries when the precondition
    was created. requests() snapshots its filenames; a second, newly filed request
    is therefore answered on a later tick, after reconciliation and deliveries.
    Duties after requests (including failover) also ran between the two answers.
    A heartbeat before a blocked duty cannot satisfy either acknowledgment.
    The service owns a published request until it removes it, after writing the
    full answer. On timeout or failure, leave that request and any partial answer
    to the service; the enclosing test tears down its temporary runtime afterward.
    """
    folder = ship.runtime / "requests"
    folder.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        path = folder / f"test-progress-{time.monotonic_ns()}.json"
        pending = path.with_suffix(".tmp")
        done = path.with_suffix(".done")

        def acknowledged():
            if path.exists():
                return False
            try:
                return done.read_text() == "unknown request 'test-progress'\n"
            except FileNotFoundError:
                return False

        try:
            pending.write_text(json.dumps({"kind": "test-progress"}))
            pending.replace(path)
            assert wait_for(acknowledged, seconds=seconds), "service did not finish its duties"
        finally:
            pending.unlink(missing_ok=True)
            if not path.exists():
                done.unlink(missing_ok=True)
