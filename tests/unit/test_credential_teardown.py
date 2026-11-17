"""A refresh a program left in its seat's home reaches the pool before the seat's launch is forgotten.

Synthetic Claude credentials in a synthetic pool folder, a fake tmux and no service running: a restart
and a check's temporary seat each end with the pool holding the newer credential, and a home is never
built over a detached credential newer than its pool's.
"""

import json
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest

from quarterdeck import post, seat as seatmod
from quarterdeck.harness.claude import ClaudeHarness
from quarterdeck.harness.home import credential
from quarterdeck.harness.synthetic_record import ClaudeShape
from quarterdeck.harness.types import Screen
from quarterdeck.seat import Launched, Seat, temporary_seat
from quarterdeck.ship import Pool, SeatSpec, ShipError, load
from quarterdeck.tmux import Pane


def live(token: str, expires_in: float) -> str:
    expiry = int((time.time() + expires_in) * 1000)
    return json.dumps({"claudeAiOauth": {"accessToken": token, "refreshToken": token, "expiresAt": expiry}})


def token(path) -> str:
    return json.loads(path.read_text())["claudeAiOauth"]["refreshToken"]


class Tmux:
    """Panes and sessions in memory: a window opened by a launch, closed by a retirement."""

    def __init__(self):
        self.panes, self.sessions = {}, {}

    def pane(self, ident):
        return self.panes.get(ident)

    def kill_window(self, ident, *, expected_pid=None):
        self.panes.pop(ident, None)

    def capture(self, *args, **kwargs):
        return ""

    def has_session(self, name):
        return name in self.sessions

    def new_session(self, name, **kwargs):
        self.sessions[name] = f"${len(self.sessions) + 1}"
        return self.sessions[name]

    def kill_session_id(self, ident):
        self.sessions = {n: i for n, i in self.sessions.items() if i != ident}

    def status_line(self, *args):
        pass


@pytest.fixture
def ship(tmp_path, monkeypatch):
    pool = tmp_path / "pool"
    pool.mkdir()
    (pool / ".credentials.json").write_text(live("older", 60))
    (tmp_path / "ship.toml").write_text(f'''[ship]
name="synthetic"
[pools.claude-primary]
provider="claude"
config_dir="{pool}"
[sessions.crew]
[seats.worker]
session="crew"
harness="claude"
pool="claude-primary"
model="synthetic-model"
''')
    tmux = Tmux()

    def open_window(self, argv, env, path):
        ident = f"%{len(tmux.panes) + 10}"
        tmux.panes[ident] = Pane(ident, "crew", self.name, 200 + len(tmux.panes), 80, 24, False, False, None)
        return tmux.panes[ident]

    monkeypatch.setattr(Seat, "_open_window", open_window)
    monkeypatch.setattr(Seat, "_survives", lambda *a: None)
    monkeypatch.setattr(seatmod.shutil, "which", lambda *a, **k: "/usr/bin/sh")
    monkeypatch.setattr(ClaudeHarness, "version", lambda self: "synthetic")
    monkeypatch.setattr(ClaudeHarness, "screen", lambda *a: Screen("", ""))
    monkeypatch.setattr(ClaudeHarness, "usage", lambda *a: pytest.fail("test queried a real provider"))
    return SimpleNamespace(ship=load(tmp_path), tmux=tmux, source=pool / ".credentials.json")


def refreshed(home):
    """The program refreshed by atomic rename: the seat's link is now a private file holding the only live token."""
    link = home / ".credentials.json"
    link.unlink(missing_ok=True)
    link.write_text(live("newer", 3600))
    return link


def test_a_restart_with_the_service_down_leaves_the_pool_holding_the_newer_credential(ship):
    seat = Seat(ship.ship, ship.ship.seat("worker"), ship.tmux)
    seat.home.mkdir(parents=True)
    seat.trusted.mkdir(parents=True, exist_ok=True)
    record = ClaudeShape(seat.home, ship.ship.folder, "synthetic-session", "synthetic-model")
    record.begin("synthetic input", "synthetic-model", "none")
    record.end("synthetic reply")
    pane = Pane("%1", "crew", "worker", 101, 80, 24, False, False, None)
    ship.tmux.panes["%1"] = pane
    Launched("%1", 101, (post.now() - timedelta(minutes=1)).isoformat(), "claude-primary", None,
             ["claude"], "synthetic", "claude").write(seat.trusted / "launch.json")
    (seat.trusted / "record").write_text(str(record.path))
    link = refreshed(seat.home)
    seat.restart()
    assert token(ship.source) == "newer", "the pool holds the refresh"
    assert link.is_symlink() and link.resolve() == ship.source.resolve()


def test_a_checks_temporary_seat_with_the_service_down_leaves_the_pool_holding_the_newer_credential(ship):
    spec = SeatSpec("check-claude-0000", "qd-check-claude-0000", "worker", "claude", ("claude-primary",),
                    model="synthetic-model", cwd=ship.ship.workspace)
    with temporary_seat(ship.ship, ship.tmux, spec, [], "qd check") as seat:
        seat.up(resume=False)
        refreshed(seat.home)
        folder = seat.folder
    assert not folder.exists(), "the temporary home is gone"
    assert token(ship.source) == "newer", "the pool holds the refresh"


def test_a_temporary_home_whose_seat_would_not_go_down_keeps_its_refresh_until_qd_down_hands_it_to_the_pool(ship, monkeypatch):
    spec = SeatSpec("check-claude-0001", "qd-check-claude-0001", "worker", "claude", ("claude-primary",),
                    model="synthetic-model", cwd=ship.ship.workspace)

    def stuck(self, launched):
        raise ShipError("synthetic: the window would not close")

    with monkeypatch.context() as patch:
        with pytest.raises(ShipError, match="would not close"):
            with temporary_seat(ship.ship, ship.tmux, spec, [], "qd check") as seat:
                seat.up(resume=False)
                link = refreshed(seat.home)
                patch.setattr(Seat, "_retire_window", stuck)
    # Nothing proved its program ended: its home, and the refresh in it, stay; the status line names it.
    assert token(link) == "newer" and token(ship.source) == "older"
    assert seatmod.leftover_seats(ship.ship)[0].startswith("check-claude-0001 outlived its qd check")
    proof = seatmod.down_leftover(ship.ship, ship.tmux, "check-claude-0001")
    assert "home removed" in proof and not seat.folder.exists()
    assert token(ship.source) == "newer", "removing the home reseeded the pool first"


def test_a_home_is_not_built_over_a_detached_credential_newer_than_its_pool(ship):
    pool = ship.ship.pools["claude-primary"]
    home = ship.ship.runtime / "seats" / "worker" / "home"
    home.mkdir(parents=True)
    link = refreshed(home)
    with pytest.raises(ShipError, match="newer than pool claude-primary"):
        credential(pool, link, "ANTHROPIC_API_KEY")
    assert not link.is_symlink() and token(link) == "newer", "the only live token is kept"
    assert token(ship.source) == "older"


def test_an_api_key_home_preserves_a_detached_subscription_refresh_without_guessing_its_source(tmp_path):
    pool = Pool("synthetic-key-destination", "claude", api_key="env:SYNTHETIC_NOT_READ")
    link = tmp_path / ".credentials.json"
    link.write_text(live("only-refresh", 3600))
    before = link.read_bytes()
    with pytest.raises(ShipError, match="API key.*move it aside"):
        credential(pool, link, "ANTHROPIC_API_KEY")
    assert not link.is_symlink() and link.read_bytes() == before, "the unknown-origin refresh is preserved exactly"
    assert sorted(p.name for p in tmp_path.iterdir()) == [link.name], "no guessed source or destination was written"


@pytest.mark.parametrize("left", ["link", "stub", "missing"])
def test_an_api_key_home_still_removes_old_links_and_stubs_without_reading_a_key(tmp_path, left):
    pool = Pool("synthetic-key-destination", "claude", api_key="env:SYNTHETIC_NOT_READ")
    link = tmp_path / ".credentials.json"
    source = tmp_path / "subscription"
    source.write_text(live("source-refresh", 3600))
    before = source.read_bytes()
    if left == "link":
        link.symlink_to(source)
    elif left == "stub":
        link.write_text("{}")
    result = credential(pool, link, "ANTHROPIC_API_KEY")
    assert result.variable == "ANTHROPIC_API_KEY" and result.link is None
    assert not link.exists() and not link.is_symlink()
    assert source.read_bytes() == before, "removing the old link must never change its subscription source"


@pytest.mark.parametrize("left", ["{}", "older-refresh"], ids=["logged-out-stub", "older-than-pool"])
def test_a_detached_credential_no_newer_than_the_pool_is_replaced_by_the_link(ship, left):
    pool = ship.ship.pools["claude-primary"]
    ship.source.write_text(live("current", 3600))
    home = ship.ship.runtime / "seats" / "worker" / "home"
    home.mkdir(parents=True)
    link = home / ".credentials.json"
    link.write_text("{}" if left == "{}" else live("older", 60))
    credential(pool, link, "ANTHROPIC_API_KEY")
    assert link.is_symlink() and link.resolve() == ship.source.resolve()
    assert token(ship.source) == "current"
