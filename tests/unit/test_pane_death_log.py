"""A seat's dead pane is logged once, with tmux's death time and exit status, from the pane already read."""

from datetime import datetime, timezone
from types import SimpleNamespace

from quarterdeck import service, tmux as tmuxmod
from quarterdeck.tmux import Pane


def test_a_dead_pane_is_logged_once_per_launch_with_its_time_and_status():
    logs = []
    svc = service.Service.__new__(service.Service)
    svc.log = lambda event, **data: logs.append((event, data))
    seat = SimpleNamespace(name="worker")
    died = int(datetime(2031, 1, 1, 3, 20, 1, tzinfo=timezone.utc).timestamp())
    pane = Pane("%7", "Crew", "worker", 4242, 80, 24, False, True, 1, died)
    for _ in range(3):
        svc.pane_died(seat, pane)
    (event, data), = logs
    assert event == "pane-dead" and data["pane"] == "%7" and data["pid"] == 4242 and data["status"] == 1
    assert datetime.fromisoformat(data["died"]).timestamp() == died
    svc.pane_died(seat, Pane("%9", "Crew", "worker", 4343, 80, 24, False, True, None, None))
    assert len(logs) == 2 and logs[-1][1]["died"] is None, "the next launch's death is its own line; unknown time says so"


def test_the_pane_line_carries_the_death_time_and_a_nine_field_line_still_parses():
    assert tmuxmod._parse("%1\tCrew\tw\t9\t80\t24\t0\t1\t2\t1790000000").dead_time == 1790000000
    assert tmuxmod._parse("%1\tCrew\tw\t9\t80\t24\t0\t0\t\t").dead_time is None
    assert tmuxmod._parse("%1\tCrew\tw\t9\t80\t24\t0\t0\t").dead_time is None, "the format before the death time"
