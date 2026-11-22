"""An explicit up does not resume completed after-order work; synthetic post only."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from quarterdeck import post
from quarterdeck.seat import Seat
from quarterdeck.ship import Pool, SeatSpec, Session, Ship


@pytest.fixture
def down_seat(tmp_path, monkeypatch):
    spec = SeatSpec('builder', 'crew', 'worker', 'fake', ('local',), fresh='after-order')
    pool = Pool('local', 'fake')
    ship = Ship(tmp_path / 'ship', 'synthetic', 'issuer', tmp_path, {'local': pool},
                {'crew': Session('crew', 'Synthetic crew')}, {'builder': spec})
    seat = Seat(ship, spec, SimpleNamespace())
    monkeypatch.setattr(seat, 'launched', lambda: None)
    started = []
    def start(resume=True, *, target=None):
        assert post._READING.get() is None, 'launch/carriage must leave the decision snapshot'
        started.append(resume)
        return 'builder up · resumed ordinary' if resume else 'builder up · fresh'
    monkeypatch.setattr(seat, '_up', start)
    store, attempts = post.Store(ship.runtime), post.Attempts(ship.runtime)
    order = store.new('order', 'issuer', seat.name, 'Ordinary synthetic work')
    attempt = attempts.open(order, seat.name, '%1', 123, 'ordinary-launch')
    attempts.close(attempt, 'delivered', 'Ordinary synthetic receipt')
    return SimpleNamespace(seat=seat, store=store, attempts=attempts, order=order, started=started)


def test_reported_last_delivered_order_starts_fresh(down_seat):
    e = down_seat
    e.store.new('report', e.seat.name, 'issuer', 'Completed', re=e.order.id)
    proof = e.seat.up()
    assert e.started == [False]
    assert 'fresh: its last order is reported' in proof


@pytest.mark.parametrize('closure', ['none', 'ack', 'foreign-report'])
def test_open_or_acknowledged_order_is_resumed(down_seat, closure):
    e = down_seat
    if closure != 'none':
        e.store.new('ack' if closure == 'ack' else 'report',
                    e.seat.name if closure == 'ack' else 'another-builder', 'issuer', 'Ordinary reply', re=e.order.id)
    assert 'fresh: its last order is reported' not in e.seat.up()
    assert e.started == [True]


@pytest.mark.parametrize('kind', ['order', 'question'])
def test_any_open_work_uses_the_same_veto_as_service_fresh_after_order(down_seat, kind):
    e = down_seat
    e.store.new('report', e.seat.name, 'issuer', 'Completed', re=e.order.id)
    pending = e.store.new(kind, 'issuer', e.seat.name, 'Ordinary pending work')
    assert pending.id in {m.id for m in e.store.open_for(e.seat.name)}
    e.seat.up()
    assert e.started == [True]


def test_seat_without_after_order_keeps_resume(down_seat):
    e = down_seat
    e.seat.spec = replace(e.seat.spec, fresh=None)
    e.store.new('report', e.seat.name, 'issuer', 'Completed', re=e.order.id)
    assert 'fresh: its last order is reported' not in e.seat.up()
    assert e.started == [True]


@pytest.mark.parametrize('result', ['launch', 'watch', 'queued', 'withheld', None])
def test_delivery_proof_is_required_and_queued_is_not_delivered(down_seat, result):
    e = down_seat
    e.store.new('report', e.seat.name, 'issuer', 'Completed', re=e.order.id)
    newest = e.attempts.open(e.order, e.seat.name, '%2', 124, 'ordinary-new-launch')
    if result is not None:
        e.attempts.close(newest, result, 'Ordinary synthetic outcome')
    e.seat.up()
    assert e.started == [result not in ('launch', 'watch')]


def test_report_without_any_delivered_order_does_not_invent_completed_work(down_seat):
    e = down_seat
    # A different seat's delivered order cannot count as this seat's history.
    e.seat.spec = replace(e.seat.spec, name='new-builder')
    e.store.new('report', e.seat.name, 'issuer', 'Ordinary unbound report')
    e.seat.up()
    assert e.started == [True]


def test_explicit_fresh_up_does_not_read_or_relabel_the_policy(down_seat, monkeypatch):
    e = down_seat
    monkeypatch.setattr(e.seat, '_last_delivered_order_reported',
                        lambda: pytest.fail('explicit fresh does not need an automatic decision'))
    proof = e.seat.up(resume=False)
    assert e.started == [False] and proof == 'builder up · fresh'


def test_the_open_work_reading_is_the_canonical_store_method(down_seat, monkeypatch):
    e = down_seat
    e.store.new('report', e.seat.name, 'issuer', 'Completed', re=e.order.id)
    read = []
    original = post.Store.open_for
    def opened(store, seat):
        assert post._READING.get() is not None
        read.append(seat)
        return original(store, seat)
    monkeypatch.setattr(post.Store, 'open_for', opened)
    e.seat.up()
    assert read == [e.seat.name] and e.started == [False]
