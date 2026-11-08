"""Prove service progress after a test establishes the condition it is checking."""

import json
import time

from .test_seat import wait_for


def service_progress(ship, seconds=8):
    """Two request acknowledgments cross a whole service pass, without a fixed sleep.

    The first may belong to a tick already past deliveries when the precondition
    was created. requests() snapshots its filenames; a second, newly filed request
    is therefore answered on a later tick, after reconciliation and deliveries.
    Duties after requests (including failover) also ran between the two answers.
    A heartbeat before a blocked duty cannot satisfy either acknowledgment.
    """
    folder = ship.runtime / "requests"
    folder.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        path = folder / f"test-progress-{time.monotonic_ns()}.json"
        pending = path.with_suffix(".tmp")
        pending.write_text(json.dumps({"kind": "test-progress"}))
        pending.replace(path)
        done = path.with_suffix(".done")
        try:
            assert wait_for(done.exists, seconds=seconds), "service did not finish its duties"
            assert done.read_text().strip() == "unknown request 'test-progress'"
        finally:
            path.unlink(missing_ok=True)
            done.unlink(missing_ok=True)
