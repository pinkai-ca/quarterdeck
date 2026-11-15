"""A fresh confirmation affects unknown-screen alarms, never Door authority."""

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from quarterdeck import pending, service, ship as shipmod, watch
from quarterdeck.door import Door
from quarterdeck.harness.base import Interruption
from quarterdeck.seat import Launched, Seat
from quarterdeck.tmux import Pane
from tests.conftest import FIXTURES


@pytest.fixture
def observed(drydock, monkeypatch):
    ship = shipmod.load(drydock)
    spec = replace(ship.seat('worker-1'), harness='pi')
    ship.seats[spec.name] = spec
    whole = (FIXTURES / 'pi/0.74.2/fresh.txt').read_text()
    partial = whole.replace('qwen3.8-27b • medium', '')
    frames, calls = [partial, whole], []
    pane = Pane('%1', 'Crew', 'worker', 123, 120, 40, False, False, None)
    def forbidden(*args, **kwargs):
        pytest.fail('alarm collection must never paste, submit or control')
    def packet(*args):
        calls.append(args)
        return pane, frames.pop(0) if len(frames) > 1 else frames[0]
    tmux = SimpleNamespace(read_pane=packet, pane=lambda _: pane, keys=forbidden, paste=forbidden)
    seat = Seat(ship, spec, tmux)
    seat.folder.mkdir(parents=True)
    seat.trusted.mkdir(parents=True, exist_ok=True)
    launched = Launched('%1', 123, datetime.now(timezone.utc).isoformat(),
                        spec.pools[0], None, ['pi'], '0.74.2', 'pi')
    launched.write(seat.trusted / 'launch.json')
    monkeypatch.setattr(watch, 'seats', lambda *_: [seat])
    monkeypatch.setattr(service, 'seats', lambda *_: [seat])
    svc = service.Service(ship, tmux)
    def alert(route):
        if route == 'status':
            facts = watch.facts(ship, tmux, walls=[], login=[])
            return seat.name in facts.needs_human
        svc.deliveries()
        return bool(svc.store.all())
    try:
        yield SimpleNamespace(seat=seat, frames=frames, calls=calls, partial=partial,
                              whole=whole, alert=alert, launched=launched, svc=svc)
    finally:
        svc.ledger.close()


@pytest.mark.parametrize('route', ['status', 'service'])
@pytest.mark.parametrize('next_frame,expected', [('whole', False), ('same', True), ('different', False)])
def test_unknown_alert_requires_the_same_fresh_frame(observed, route, next_frame, expected):
    w = observed
    assert w.seat.harness.screen(w.partial).unknown
    assert w.seat.harness.screen(w.whole).box == ''
    w.frames[:] = [w.partial, {'whole': w.whole, 'same': w.partial,
                              'different': w.partial + '\nchanged unknown frame'}[next_frame]]
    assert w.alert(route) is expected
    assert len(w.calls) == 2


@pytest.mark.parametrize('route', ['status', 'service'])
@pytest.mark.parametrize('kind', ['draft', 'dialog', 'pending'])
def test_known_obstructions_do_not_wait_for_a_second_frame(observed, monkeypatch, route, kind):
    w = observed
    if kind == 'draft':
        lines = w.whole.splitlines()
        rules = [i for i, line in enumerate(lines) if line.startswith('─' * 20)]
        lines[rules[-2] + 1] = 'a human draft'
        w.frames[:] = ['\n'.join(lines)]
    elif kind == 'dialog':
        monkeypatch.setattr(w.seat.harness, 'interruptions',
                            lambda: (Interruption('synthetic login', 'LOGIN'),))
        w.frames[:] = ['LOGIN']
    else:
        monkeypatch.setattr(pending, 'held', lambda *args: ('uncertain submission', 'needs a human', False))
    assert w.alert(route)
    assert len(w.calls) == 1, 'only unknown-screen alarms require another capture'


@pytest.mark.parametrize('route', ['status', 'service'])
@pytest.mark.parametrize('change', ['pid', 'launch', 'missing'])
def test_another_or_missing_launch_cannot_confirm_an_unknown_alarm(observed, monkeypatch, route, change):
    w = observed
    w.frames[:] = [w.partial]
    original = w.seat.tmux.read_pane
    def changed(*args):
        found = original(*args)
        if len(w.calls) == 2:
            if change == 'pid':
                return replace(found[0], pid=999), found[1]
            if change == 'missing':
                return None
            replace(w.launched, at='2000-01-01T00:00:00+00:00').write(w.seat.trusted / 'launch.json')
        return found
    monkeypatch.setattr(w.seat.tmux, 'read_pane', changed)
    assert not w.alert(route)
    assert len(w.calls) == 2


def test_first_unknown_frame_still_refuses_delivery_without_confirmation(observed):
    w = observed
    outcome = Door(w.seat).deliver('never type through a partial frame')
    assert outcome.result == 'withheld' and 'unknown dialog' in outcome.detail
    assert len(w.calls) == 1
    assert not w.svc.attempts.all() and not w.svc.store.all()


@pytest.mark.parametrize('route', ['status', 'service'])
def test_a_healthy_frame_stays_one_fresh_packet(observed, route):
    w = observed
    w.frames[:] = [w.whole, w.partial]
    assert not w.alert(route)
    assert len(w.calls) == 1


def test_unconfirmed_alarm_keeps_the_original_unknown_closed(observed):
    w = observed
    reading, reason = Door(w.seat).human_obstruction()
    assert reason is None
    assert not reading.open and reading.clears == 'needs a human'
    assert 'unknown dialog' in reading.reason
    assert len(w.calls) == 2


def test_stable_unknown_report_is_still_deduplicated(observed):
    w = observed
    w.frames[:] = [w.partial]
    assert w.alert('service')
    w.svc.deliveries()
    assert len(w.svc.store.all()) == 1 and len(w.calls) == 4
