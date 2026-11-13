"""Synthetic service decision presentation; no conversation or native process access."""

from contextlib import nullcontext
import json
from types import SimpleNamespace

import pytest

from quarterdeck import idle, move_hold, post, quota, watch


@pytest.fixture
def context(tmp_path, monkeypatch):
    clock = dict(wall=10_000_000_000, monotonic=20_000_000_000)
    monkeypatch.setattr(move_hold, 'time', SimpleNamespace(
        time_ns=lambda: clock['wall'], monotonic_ns=lambda: clock['monotonic']))
    launch = SimpleNamespace(pane='%pane-maple', pid=101002,
                             at='2032-04-05T06:07:00+00:00', harness='fake')

    def no_record(*args):
        raise AssertionError('status must not read a conversation')

    seat = SimpleNamespace(
        name='seat-maple', folder=tmp_path / 'seats' / 'seat-maple',
        ship=SimpleNamespace(runtime=tmp_path), session=SimpleNamespace(human=True),
        launched=lambda: launch, record=no_record, join=no_record,
        harness=SimpleNamespace(state=no_record), is_up=True,
        spec=SimpleNamespace(pools=('pool-willow', 'pool-oak')),
    )
    return seat, clock, launch


def publish(seat, category='tools-unknown', detail=None):
    move_hold.publish(seat, move_hold.generation(seat),
                      idle.Hold(category, detail or idle.REASONS[category]))


def test_human_fact_contains_category_and_generation_only(context):
    seat, _, _ = context
    publish(seat, 'tools-pending', 'tools pending: Bash since 2032-04-05T06:07:00+00:00')
    data = json.loads(move_hold.path(seat).read_text())
    assert data['category'] == 'tools-pending'
    assert data['detail'] is None
    assert set(data) == {'version', 'generation', 'category', 'detail', 'wall', 'monotonic'}
    assert move_hold.reading(seat) == 'tools are pending'


def test_crew_fact_can_show_call_label_and_original_start(context):
    seat, _, _ = context
    seat.session.human = False
    detail = 'tools pending: Bash since 2032-04-05T06:07:00+00:00'
    publish(seat, 'tools-pending', detail)
    assert move_hold.reading(seat) == detail


def test_changed_launch_expires_prior_hold(context):
    seat, _, launch = context
    publish(seat)
    launch.at = '2032-04-05T06:08:00+00:00'
    assert move_hold.reading(seat) == 'move proof unknown'


def test_changed_watch_generation_expires_prior_hold(context):
    seat, _, _ = context
    publish(seat)
    target = post.watch_file(seat.ship.runtime, seat.name)
    target.write_text('synthetic watch generation willow\n')
    assert move_hold.reading(seat) == 'move proof unknown'
    publish(seat)
    assert move_hold.reading(seat) == 'tool state unknown'
    target.unlink()
    assert move_hold.reading(seat) == 'move proof unknown'


@pytest.mark.parametrize('change', ['stale', 'clock-backwards', 'clock-disagrees'])
def test_expiry_and_clock_uncertainty_are_visible(context, change):
    seat, clock, _ = context
    publish(seat)
    if change == 'stale':
        clock['wall'] += 6_000_000_000
        clock['monotonic'] += 6_000_000_000
    elif change == 'clock-backwards':
        clock['wall'] -= 1
    else:
        clock['wall'] += 1_000_000_000
    assert move_hold.reading(seat) == 'move proof unknown'


def test_clear_decision_replaces_hold_without_a_lifecycle_action(context):
    seat, _, _ = context
    publish(seat)
    move_hold.publish(seat, move_hold.generation(seat), None)
    assert move_hold.reading(seat) is None


def test_unchanged_decisions_refresh_at_bounded_frequency(context, monkeypatch):
    seat, clock, _ = context
    writes = []
    real_replace = move_hold.durable.replace_file

    def replace(target, text):
        writes.append(target)
        real_replace(target, text)

    monkeypatch.setattr(move_hold.durable, 'replace_file', replace)
    publish(seat)
    clock['wall'] += 1_000_000_000
    clock['monotonic'] += 1_000_000_000
    publish(seat)
    assert len(writes) == 1
    clock['wall'] += 2_000_000_000
    clock['monotonic'] += 2_000_000_000
    publish(seat)
    assert len(writes) == 2


@pytest.mark.parametrize('bad', ['missing', 'json', 'category', 'version'])
def test_missing_or_malformed_fact_cannot_be_a_clear_decision(context, bad):
    seat, _, _ = context
    if bad != 'missing':
        publish(seat)
        target = move_hold.path(seat)
        if bad == 'json':
            target.write_text('{')
        else:
            data = json.loads(target.read_text())
            data[bad] = 'unrecognized' if bad == 'category' else True
            target.write_text(json.dumps(data))
    assert move_hold.reading(seat) == 'move proof unknown'


def test_walled_human_with_available_destination_shows_tools_hold_without_record(context, monkeypatch):
    seat, _, _ = context
    publish(seat)
    monkeypatch.setattr(watch.ledger.Ledger, 'reading', lambda runtime: nullcontext(object()))
    monkeypatch.setattr(quota, 'current_walled', lambda *args: True)
    monkeypatch.setattr(quota, 'candidate_pools', lambda target: ['pool-oak'])
    monkeypatch.setattr(quota, 'cached_open', lambda *args: True)
    held, reasons = watch.limit_holds(seat.ship, [seat])
    assert held == []
    assert reasons == ['seat-maple: move held: tool state unknown']


def test_overflow_keeps_seat_and_missing_dimension(context):
    facts = watch.Facts(service='down', waiting=0, oldest=0, blind=0,
                        routes=['synthetic route ' * 30],
                        move_holds=['seat-maple: move held: tool state unknown'])
    line = facts.line()
    assert 'SERVICE DOWN' in line
    assert 'seat-maple: move held: tool state unknown' in line
