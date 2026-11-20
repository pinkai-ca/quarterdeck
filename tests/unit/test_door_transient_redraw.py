"""A redraw may recover within the existing bound; an unknown frame never sends keys."""
from types import SimpleNamespace
import pytest
from quarterdeck.door import Door
from quarterdeck.harness.types import Box, Screen


@pytest.fixture
def frames(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr('quarterdeck.door._clock', lambda: clock[0])
    monkeypatch.setattr('quarterdeck.door._sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    calls = []
    seat = SimpleNamespace(harness=SimpleNamespace(exact_input_text=True, keys=SimpleNamespace(submit='Enter')),
        tmux=SimpleNamespace(pane=lambda _: None, keys=lambda *args: calls.append(args)))
    seat.harness.queues_while_working = lambda _: True
    door = Door(seat)
    monkeypatch.setattr(door, 'adapter_obstruction', lambda _: None)
    monkeypatch.setattr(door, 'matches', lambda box, screen: box.text == screen.box)
    return door, clock, calls


@pytest.mark.parametrize('operation', ['paste', 'submit', 'now'])
def test_unreadable_redraw_recovers_without_a_blind_delivery(frames, monkeypatch, operation):
    door, clock, calls = frames
    ready = Screen('', box='payload', working=False)
    stream = iter([None, Screen('', box=None), ready])
    monkeypatch.setattr(door, 'screen', lambda: next(stream))
    if operation == 'paste':
        assert door.box_shows(Box('payload')) is ready
    elif operation == 'submit':
        assert door.submit('%synthetic') is None
        assert calls == [('%synthetic', 'Enter')]
    else:
        assert door.activity_screen() is ready
    assert 0 < clock[0] <= 2


@pytest.mark.parametrize('operation', ['paste', 'submit', 'now'])
def test_persistent_unknown_expires_without_input(frames, monkeypatch, operation):
    door, clock, calls = frames
    monkeypatch.setattr(door, 'screen', lambda: None)
    if operation == 'paste':
        assert door.box_shows(Box('payload')) is None
    elif operation == 'submit':
        assert 'no key sent' in door.submit('%synthetic')
    else:
        assert door.activity_screen() is None
    assert 2 <= clock[0] < 2.2
    assert calls == []


def test_a_named_adapter_fault_is_not_waited_away(frames, monkeypatch):
    door, clock, calls = frames
    fault = Screen('', box=None)
    monkeypatch.setattr(door, 'screen', lambda: fault)
    monkeypatch.setattr(door, 'adapter_obstruction', lambda _: 'named adapter fault')
    assert door.box_shows(Box('payload')) is fault
    assert door.submit('%synthetic') == 'named adapter fault'
    assert clock[0] == 0 and calls == []
