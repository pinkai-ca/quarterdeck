"""A recipient's completed obligations never return through carriage's receipt fallbacks."""

import pytest

from quarterdeck import post
from .test_pending_delivery import setup  # noqa: F401


def active(carriage):
    return [identifier for row in carriage.rows for identifier in row.active]


@pytest.mark.parametrize('kind', ['order', 'question'])
@pytest.mark.parametrize('result', [None, 'launch', 'queued', 'pending', 'blind', 'withheld', 'unacted', 'delivered', 'watch'])
@pytest.mark.parametrize('destination', ['issuer', post.SHIP])
def test_closed_obligation_is_neither_active_nor_carried_after_fresh_start(setup, kind, result, destination):
    svc, seat, live, record = setup
    message = svc.store.new(kind, 'issuer', seat.name, 'Synthetic completed work')
    if result is not None:
        attempt = svc.attempts.open(message, seat.name, '%1', 123, seat.launched().at)
        svc.attempts.close(attempt, result)
    reply_kind = 'report' if kind == 'order' else 'answer'
    closing = svc.store.new(reply_kind, seat.name, destination, 'Synthetic completion', re=message.id)
    assert not svc.store.is_open(message)
    assert svc.store.closing(message) == closing
    before = {p: p.read_bytes() for p in svc.attempts.dir.glob('*.json')}
    for _ in range(2):
        carriage = seat.carriage()
        assert message.id not in active(carriage)
        assert message not in carriage.carried
        assert not carriage.rows and carriage.text is None
    assert {p: p.read_bytes() for p in svc.attempts.dir.glob('*.json')} == before
    assert live['pasted'] == live['submitted'] == 0


@pytest.mark.parametrize('result', [None, 'launch', 'delivered'])
def test_open_order_remains_active_despite_ack_or_another_seats_report(setup, result):
    svc, seat, live, record = setup
    message = svc.store.new('order', 'issuer', seat.name, 'Synthetic unfinished work')
    svc.store.new('ack', seat.name, 'issuer', 'Synthetic claim', re=message.id)
    svc.store.new('report', 'other', 'issuer', 'Someone else reported', re=message.id)
    if result is not None:
        attempt = svc.attempts.open(message, seat.name, '%1', 123, seat.launched().at)
        svc.attempts.close(attempt, result)
    assert svc.store.is_open(message)
    carriage = seat.carriage()
    assert active(carriage) == [message.id] and carriage.carried == [message]
    assert live['pasted'] == live['submitted'] == 0


def test_unread_report_still_arrives_once_even_under_a_closed_order(setup):
    svc, seat, live, record = setup
    old = svc.store.new('order', 'issuer', seat.name, 'Synthetic old task')
    svc.store.new('report', seat.name, 'issuer', 'Synthetic done', re=old.id)
    message = svc.store.new('report', 'issuer', seat.name, 'Synthetic new information', re=old.id)
    carriage = seat.carriage()
    assert active(carriage) == [message.id] and carriage.carried == [message]
    attempt = svc.attempts.open(message, seat.name, '%1', 123, seat.launched().at)
    svc.attempts.close(attempt, 'watch')
    assert active(seat.carriage()) == [] and seat.carriage().carried == []
    assert live['pasted'] == live['submitted'] == 0
