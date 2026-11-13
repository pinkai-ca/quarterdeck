"""Adopt a proved automatic successor before ordinary resume replaces its launch."""

import pytest

from quarterdeck import compaction
from .test_compaction_protocol import world, ready


@pytest.mark.parametrize('successor_count', [0, 1])
def test_immediate_resume_keeps_proved_successor(world, monkeypatch, successor_count):
    w = world
    request, hand, _ = ready(w)
    predecessor = compaction.load(w.svc.runtime, w.seat.name)['generation']
    w.source[0].result()
    w.source[0].finish()
    compaction.tick(w.seat)
    assert w.calls == ['down', 'fresh']
    assert compaction.load(w.svc.runtime, w.seat.name)['transition'] is not None
    successor = w.source[0]
    for _ in range(successor_count):
        successor.compact()
    order = w.svc.store.new('order', 'operator', w.seat.name, 'Synthetic next order')
    unread = w.svc.store.new('answer', 'operator', w.seat.name, 'Synthetic unread answer')
    # No compaction tick between automatic startup and the explicit lifecycle boundary.
    at_retirement = []
    down = w.seat._down
    def retire():
        at_retirement.append(compaction.load(w.svc.runtime, w.seat.name))
        return down()
    monkeypatch.setattr(w.seat, '_down', retire)
    w.seat.restart()
    adopted = at_retirement[0]
    assert adopted['generation'] != predecessor
    assert adopted['source']['conversation'] == successor.conversation
    assert adopted['count'] == successor_count
    assert adopted['transition'] is None
    assert {hand, order, unread} <= set(w.carried[-1])
    for _ in range(3):
        compaction.tick(w.seat)
    data = compaction.load(w.svc.runtime, w.seat.name)
    assert data['generation'] == adopted['generation'] and data['count'] == successor_count
    assert not data['reading_unknown'] and data['request'] is None
    assert request.id in data['protocol_requests']
    assert w.svc.store.is_open(hand) and w.svc.store.is_open(order)
    assert w.calls == ['down', 'fresh', 'down', 'resume']


@pytest.mark.parametrize('damage', ['missing-record', 'unproved-origin'])
def test_unproved_successor_stays_unknown_but_explicit_resume_remains_available(world, damage):
    w = world
    _, hand, _ = ready(w)
    predecessor = compaction.load(w.svc.runtime, w.seat.name)['generation']
    w.source[0].result()
    w.source[0].finish()
    compaction.tick(w.seat)
    if damage == 'missing-record':
        w.source[0].path.unlink()
    else:
        # The record already existed before this fresh launch, so it cannot prove its origin.
        from dataclasses import replace
        launch = w.seat.launched()
        replace(launch, records_before=[str(w.source[0].path)]).write(w.seat.folder/'launch.json')
    w.seat.restart()
    compaction.tick(w.seat)
    data = compaction.load(w.svc.runtime, w.seat.name)
    assert data['generation'] == predecessor and data['reading_unknown']
    assert 'count unknown' in compaction.status(w.seat)
    assert data['transition'] is None and data['ready'] is None
    assert hand in w.carried[-1] and w.svc.store.is_open(hand)
    assert w.calls == ['down', 'fresh', 'down', 'resume']
