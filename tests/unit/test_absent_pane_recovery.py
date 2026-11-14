"""An absent pane is off even when its last launch remains on disk."""
from types import SimpleNamespace

import pytest
from quarterdeck import harness, watch
from quarterdeck.seat import Seat


@pytest.mark.parametrize('program', harness.NAMES)
def test_missing_pane_with_saved_launch_is_off(program):
    seat = SimpleNamespace(harness=harness.get(program), pane=lambda: None,
                           launched=lambda: SimpleNamespace(at='synthetic launch'))
    seat.is_working = lambda: Seat.is_working(seat)
    assert seat.is_working() is False
    reading = watch.activity(seat)
    assert (reading.state, reading.detail) == ('off', 'no pane')
