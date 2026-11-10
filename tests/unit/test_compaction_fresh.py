"""Compaction readiness is an explicit durable protocol, never inferred from idle prose."""

from dataclasses import replace

from .test_fresh_after_order import worker, at
from quarterdeck import post


def test_after_order_never_automatically_freshens_human_owned_worker(worker):
    w = worker
    w.seat.ship.sessions[w.seat.spec.session] = replace(w.seat.session, human=True)
    w.record.begin()
    w.record.finish()
    w.svc.fresh_after_order()
    assert w.fresh == []
