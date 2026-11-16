"""A blocked readiness observation cannot consume an injected settlement guard."""

import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

from quarterdeck import drill, lifecycle


def test_slow_readiness_observation_waits_for_settlement_not_wall_time(tmp_path, monkeypatch):
    exercise = drill.Drill(SimpleNamespace(runtime=tmp_path), Mock(), patience=.01)
    # Attribute injection also lets this exact test run on the unchanged main API.
    exercise.settlement_clock = lambda: 0.0
    order = exercise.store.new('order', 'ship', 'worker', 'Synthetic open work')
    guard = exercise.patience * drill.SETTLEMENT_WINDOWS
    entered, transitions, calls = [], [], []
    def fresh():
        calls.append('claim')
        if not entered:
            entered.append(time.monotonic())
            # Deliberate minimum delay, not a maximum latency expectation. A
            # busier runner only blocks longer; no production sleep is added.
            threading.Event().wait(guard * 2)
            assert time.monotonic() - entered[0] > guard
            raise lifecycle.Busy('Synthetic claim has not settled yet')
        transitions.append('started')
        return 'fresh proof'
    seat = SimpleNamespace(name='worker', session=SimpleNamespace(human=False), fresh=fresh,
                           launched=lambda: SimpleNamespace(at='new launch'))
    monkeypatch.setattr(drill, 'journals', lambda _: ([], []))
    monkeypatch.setattr(drill.service_wait, 'Arrivals', lambda runtime: SimpleNamespace(
        refresh=lambda *args, **kwargs: None, wait=lambda seconds: False, close=lambda: None))
    proof, reason = exercise.fresh_after_delivery(seat, order)
    assert proof == 'fresh proof', reason
    assert reason == 'carried unattempted'
    assert calls == ['claim', 'claim'] and transitions == ['started']
    assert exercise.store.is_open(order)
