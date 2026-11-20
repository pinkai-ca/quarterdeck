"""A synthetic program executes the check's one deferred opening through Door."""

from dataclasses import replace
from datetime import datetime, timezone
import json

from quarterdeck import check, harness, post
from quarterdeck.harness.fake import FakeHarness
from quarterdeck.harness.limits import Usage, Window
from quarterdeck.ship import Pool, Ship
from quarterdeck.tmux import Tmux


class TaskOpeningFake(FakeHarness):
    minimal_check_task = "$ printf 'OK\\n' > proof.txt"
    requires_deferred_opening = True
    opening_prompt_turn = True
    check_model = "synthetic-model"
    check_effort = "minimal"

    def __init__(self):
        self.openings = []
        self.readings = []
        self.cwd = None

    def launch(self, seat, pool, home, brief, resume, first):
        assert brief.read_text() == ""
        assert first == self.minimal_check_task
        self.openings.append(first)
        self.cwd = seat.cwd
        launch = super().launch(seat, pool, home, brief, resume, None)
        return replace(launch, deferred_opening=first)

    def opening_readiness(self, home, screen):
        return (bool(screen and screen.working is False and screen.box == ""), 'synthetic idle composer')

    def opening_current(self, home):
        return True

    def check_usage_in_home(self, pool, home):
        self.readings.append(home)
        return Usage((Window('synthetic-window', len(self.readings) / 10,
                             datetime(2030, 1, 2, tzinfo=timezone.utc)),),
                     datetime(2030, 1, 1, tzinfo=timezone.utc))


def test_the_task_only_check_has_one_door_opening_and_no_second_order(tmp_path, monkeypatch):
    folder = tmp_path / 'ship'
    folder.mkdir()
    ship = Ship(folder, 'synthetic-ship', 'synthetic-person', folder,
                {'synthetic-pool': Pool('synthetic-pool', 'synthetic')}, {}, {})
    adapter = TaskOpeningFake()
    original = harness.get
    monkeypatch.setattr(harness, 'get', lambda name: adapter if name == 'fake' else original(name))
    transport = Tmux.private(tmp_path / 'private-transport')
    try:
        checks = check.check(ship, 'fake', transport, minimal=True)
        assert all(step.passed is True for step in checks), checks
        assert adapter.openings == [adapter.minimal_check_task]
        assert len(adapter.readings) == 2 and adapter.readings[0] == adapter.readings[1]
        assert adapter.cwd != ship.workspace
        assert not adapter.cwd.exists()
        evidence = adapter.cwd.parent
        assert (evidence / 'proof.txt').read_text() == 'OK\n'
        assert json.loads((evidence / 'usage-before.json').read_text())['windows'][0]['used'] == .1
        assert json.loads((evidence / 'usage-after.json').read_text())['windows'][0]['used'] == .2
        assert post.Store(ship.runtime).all() == []
        assert not (ship.runtime / 'proven').exists()
        assert [step.name for step in checks].count('record joined, the turn ended and read') == 1
    finally:
        assert transport.close(), transport.held
