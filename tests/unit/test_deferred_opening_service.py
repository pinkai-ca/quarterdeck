"""Startup is observed across passes, with one journal and no service polling."""
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest
from quarterdeck import launch_opening, pending, recovery
from quarterdeck.door import Delivery
from quarterdeck.harness.types import Screen
from quarterdeck.seat import Launched, Seat
from quarterdeck.service import Service


@pytest.fixture
def opening(tmp_path, monkeypatch):
    launched = Launched('%synthetic', 123, datetime.now(timezone.utc).isoformat(),
                        'synthetic', None, ['synthetic'], 'synthetic')
    folder = tmp_path / 'seat'
    folder.mkdir()
    launched.write(folder / 'launch.json')
    adapter = SimpleNamespace(name='synthetic', start_seconds=60,
        opening_readiness=lambda home, screen: (not screen.starting, 'synthetic startup proof'),
        opening_current=lambda home: True)
    seat = SimpleNamespace(folder=folder, home=folder/'home', harness=adapter,
        name='synthetic', ship=SimpleNamespace(runtime=tmp_path), defer_opening=True)
    def current():
        return Launched(**json.loads((folder/'launch.json').read_text()))
    seat.launched = current
    monkeypatch.setattr(pending, 'current_identity', lambda _: launch_opening.binding(seat, current()))
    screen = [Screen('', box=None, starting=True)]
    sent, confirmations = [], []
    class Door:
        def __init__(self, seat):
            self.since = datetime.now(timezone.utc)
        def screen(self):
            return screen[0]
        def deliver(self, text, *, verify, confirm):
            assert verify() and confirm is False and self.observation_seconds == 0
            sent.append(text)
            return Delivery('typed')
        def confirm(self, text, *, seconds):
            assert seconds == 0
            return confirmations.pop(0)
    monkeypatch.setattr('quarterdeck.door.Door', Door)
    monkeypatch.setattr('quarterdeck.launch_opening.time.sleep', lambda _: pytest.fail('service must not poll'))
    launch_opening.prepare(seat, launched, 'synthetic opening')
    return seat, screen, sent, confirmations


def test_service_relaunch_returns_before_startup_is_ready(opening, monkeypatch):
    seat, screen, sent, confirmations = opening
    seat.pane = lambda: None
    seat.defer_opening = False
    monkeypatch.setattr(recovery, 'held', lambda _: None)
    seat.restart = lambda: Seat._deliver_opening(seat, seat.launched())
    seat.fresh = lambda: pytest.fail('expected resume')
    result = Service.relaunch(SimpleNamespace(), seat, True)
    assert result == seat.launched()
    assert seat.defer_opening and sent == []
    assert json.loads(launch_opening.path_for(seat, result).read_text())['phase'] == 'prepared'


def test_passes_wait_then_send_once_and_confirm_without_retyping(opening, monkeypatch):
    seat, screen, sent, confirmations = opening
    settled = []
    monkeypatch.setattr(launch_opening, 'settle_carried', lambda owner: settled.append(owner.data['phase']))
    launch_opening.advance(seat)
    launch_opening.advance(seat)
    assert sent == [] and settled == []
    screen[0] = Screen('', box='', working=False)
    launch_opening.advance(seat)
    assert sent == ['synthetic opening'] and settled == []
    confirmations.append(Delivery('blind'))
    launch_opening.advance(seat)
    assert sent == ['synthetic opening'] and settled == []
    confirmations.append(Delivery('delivered'))
    launch_opening.advance(seat)
    assert sent == ['synthetic opening'] and settled == ['delivered']


def test_expired_startup_stays_held_without_a_transport(opening):
    seat, screen, sent, confirmations = opening
    old = seat.launched()
    stale = Launched(**{**old.__dict__, 'at': (old.when - timedelta(seconds=61)).isoformat()})
    stale.write(seat.folder/'launch.json')
    launch_opening.prepare(seat, stale, 'synthetic opening')
    with pytest.raises(Exception, match='synthetic startup proof'):
        launch_opening.advance(seat)
    launch_opening.advance(seat)
    assert sent == []
    assert json.loads(launch_opening.path_for(seat, stale).read_text())['phase'] == 'held'
