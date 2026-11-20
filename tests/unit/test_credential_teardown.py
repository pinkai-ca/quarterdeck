"""A refresh a program left in its seat's home reaches the pool before the seat's launch is forgotten.

Synthetic Claude credentials in a synthetic pool folder, a fake tmux and no service running: a restart
and a check's temporary seat each end with the pool holding the newer credential, and a home is never
built over a detached credential newer than its pool's.
"""

import io
import json
import shutil
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import acts, cli, credential as credentialmod, post, seat as seatmod, watch
from quarterdeck.harness.claude import ClaudeHarness
from quarterdeck.harness.home import credential
from quarterdeck.harness.synthetic_record import ClaudeShape
from quarterdeck.harness.types import Screen
from quarterdeck.seat import Launched, Seat, temporary_seat
from quarterdeck.service import Service
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


@pytest.fixture
def muse_ship(tmp_path):
    pool = tmp_path / "pool"
    pool.mkdir()
    source = pool / "auth.json"
    source.write_bytes(b"synthetic pool file\n")
    (pool / "other.json").write_bytes(b"synthetic foreign file\n")
    (tmp_path / "ship.toml").write_text(f'''[ship]
name="synthetic"
[pools.muse-primary]
provider="meta"
config_dir="{pool}"
[sessions.crew]
[seats.muse-worker]
session="crew"
harness="muse"
pool="muse-primary"
model="muse-wonder-1.0"
''')
    return load(tmp_path), Tmux(), source


def setup_muse_auth(home, source, change):
    attached = home / "auth.json"
    nested = home / "config/muse/auth.json"
    nested.parent.mkdir(parents=True)
    if change == "attached-file":
        attached.write_bytes(b"synthetic detached file\n")
    else:
        attached.symlink_to(source)
    if change == "nested-file":
        nested.write_bytes(b"synthetic detached file\n")
    elif change == "foreign-link":
        nested.symlink_to(source.parent / "other.json")
    else:
        nested.symlink_to(attached)
    return attached, nested


def auth_state(*names):
    return [(name.lstat().st_ino, name.readlink() if name.is_symlink() else name.read_bytes()) for name in names]


@pytest.mark.parametrize("jailed", [False, True], ids=["ordinary", "jailed"])
@pytest.mark.parametrize("change", ["attached-file", "nested-file", "foreign-link", "linked"])
def test_muse_check_retains_homes_for_credential_review(muse_ship, change, jailed):
    ship, tmux, source = muse_ship
    spec = SeatSpec("check-muse-0000", "qd-check-muse-0000", "worker", "muse", ("muse-primary",),
                    model="muse-wonder-1.0", cwd=ship.workspace, sandbox=jailed)
    with temporary_seat(ship, tmux, spec, [], "qd check") as seat:
        if jailed:
            assert credentialmod.participants(seat.ship, [seat]) == []
        names = setup_muse_auth(seat.home, source, change)
        sources = source, source.parent / "other.json"
        before = auth_state(*sources, *names)
    Service(ship, tmux).credentials()  # the next service pass must leave review holds in place
    line = watch.facts(ship, tmux).line()
    if change == "linked":
        assert not seat.folder.exists() and not seat.trusted.exists()
        assert seat.name not in line
        assert auth_state(*sources) == before[:2]
    else:
        assert auth_state(*sources, *names) == before
        assert f"{seat.name}: home held for credential review" in line
    assert tmux.sessions == {}


@pytest.mark.parametrize("jailed", [False, True], ids=["ordinary", "jailed"])
@pytest.mark.parametrize("change", ["attached-file", "nested-file", "foreign-link", "linked"])
def test_muse_service_retains_homes_for_credential_review(muse_ship, change, jailed):
    ship, tmux, source = muse_ship
    spec = replace(ship.seat("muse-worker"), sandbox=jailed)
    ship = replace(ship, seats={spec.name: spec})
    seat = Seat(ship, spec, tmux)
    if jailed:
        assert credentialmod.participants(ship, [seat]) == []
    names = setup_muse_auth(seat.home, source, change)
    names = source, source.parent / "other.json", *names
    before = auth_state(*names)
    service = Service(ship, tmux)
    service.credentials()
    service.credentials()
    assert auth_state(*names) == before
    line = watch.facts(ship, tmux).line()
    if change == "linked":
        assert "home held for credential review" not in line
    else:
        assert f"{seat.name}: home held for credential review" in line


def test_hold_home_selects_only_the_seats_own_participant(muse_ship, monkeypatch):
    ship, tmux, source = muse_ship
    seat = Seat(ship, ship.seat("muse-worker"), tmux)
    other = seat.folder.parent / "other" / "home"
    setup_muse_auth(other, source, "attached-file")
    setup_muse_auth(seat.home, source, "linked")
    foreign = credentialmod.Participant("other", other, seat.pool.name)
    usage = credentialmod.Participant(seat.name, other, seat.pool.name, usage_reader=True)
    own = credentialmod.Participant(seat.name, seat.home, seat.pool.name)
    monkeypatch.setattr(credentialmod, "participants", lambda *_: [foreign, usage, own])
    assert not credentialmod.hold_home(ship, seat)
    assert credentialmod.held_words(ship) == []
    own_link = seat.home / "auth.json"
    own_link.unlink()
    own_link.write_text("synthetic own credential")
    assert credentialmod.hold_home(ship, seat)
    assert credentialmod.held_for_review(ship, seat.name)
    assert not credentialmod.held_for_review(ship, foreign.name)


def held_muse(muse_ship, change):
    ship, tmux, source = muse_ship
    seat = Seat(ship, ship.seat("muse-worker"), tmux)
    names = setup_muse_auth(seat.home, source, change)
    service = Service(ship, tmux)
    service.credentials()
    assert credentialmod.held_for_review(ship, seat.name)
    return seat, names, service


def test_release_command_displays_metadata_and_keeps_the_home_after_the_service_pass(muse_ship, monkeypatch, capsys):
    seat, (main, nested), service = held_muse(muse_ship, "attached-file")
    before = auth_state(main, nested)
    runtime = seat.ship.runtime
    # Read an already-written review from before the release command existed.
    path = runtime / credentialmod.HELD / f"{seat.name}.json"
    path.write_text(json.dumps(dict(seat=seat.name, pool=seat.pool.name, review=True)))
    assert f"qd credential release {seat.name}" in watch.facts(seat.ship, seat.tmux).line()
    monkeypatch.setattr(cli, "handoff", lambda *_: None)
    monkeypatch.setattr(cli, "load_ship", lambda _: seat.ship)
    monkeypatch.setattr(cli.Tmux, "for_ship", lambda _: seat.tmux)
    monkeypatch.setenv("QD_SEAT", "synthetic-operator")
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("keep\n"))

    with monkeypatch.context() as patch:
        for method in ("open", "read_bytes", "read_text"):
            read = getattr(Path, method)

            def metadata_only(self, *args, read=read, **kwargs):
                assert self.name != "auth.json", "release opened a sign-in file"
                return read(self, *args, **kwargs)

            patch.setattr(Path, method, metadata_only)
        assert cli.main(["--ship", str(seat.ship.folder), "credential", "release", seat.name]) == 0
    out = capsys.readouterr()
    assert "auth.json: file" in out.out
    assert f"config/muse/auth.json: link -> {str(main)!r}" in out.out
    assert "synthetic detached file" not in out.out
    assert "keep/remove" in out.err
    assert auth_state(main, nested) == before
    assert credentialmod.held_for_review(seat.ship, seat.name)
    service.credentials()
    service.credentials()
    assert not credentialmod.held_for_review(seat.ship, seat.name)
    assert "credential review" not in watch.facts(seat.ship, seat.tmux).line()
    assert auth_state(main, nested) == before
    act = acts.last(seat)
    assert (act["verb"], act["choice"], act["outcome"], act["caller"]) == (
        "credential-release", "keep", "keep", "synthetic-operator")
    assert "credential review" in acts.describe(act)
    main.unlink()
    main.write_bytes(b"synthetic later file")
    service.credentials()
    assert credentialmod.held_for_review(seat.ship, seat.name), "changed metadata needs a new review"


def test_keeping_a_temporary_home_clears_both_the_hold_and_leftover_alert(muse_ship):
    ship, tmux, source = muse_ship
    spec = SeatSpec("check-muse-0001", "qd-check-muse-0001", "worker", "muse", ("muse-primary",),
                    model="muse-wonder-1.0", cwd=ship.workspace, sandbox=True)
    with temporary_seat(ship, tmux, spec, [], "qd check") as seat:
        names = setup_muse_auth(seat.home, source, "foreign-link")
        before = auth_state(*names)
    credentialmod.release(ship, tmux, seat.name, "operator", lambda _: "keep")
    Service(ship, tmux).credentials()
    assert auth_state(*names) == before
    assert not credentialmod.held_for_review(ship, seat.name)
    assert not any(seat.name in line for line in seatmod.leftover_seats(ship))


@pytest.mark.parametrize("temporary", [False, True], ids=["declared", "temporary"])
def test_release_removes_the_held_home_and_keeps_the_audit(muse_ship, temporary):
    ship, tmux, source = muse_ship
    if temporary:
        spec = SeatSpec("check-muse-0002", "qd-check-muse-0002", "worker", "muse", ("muse-primary",),
                        model="muse-wonder-1.0", cwd=ship.workspace)
        with temporary_seat(ship, tmux, spec, [], "qd check") as seat:
            main, _ = setup_muse_auth(seat.home, source, "foreign-link")
    else:
        seat, (main, _), _ = held_muse(muse_ship, "foreign-link")
        (seat.folder / "other-seat-data").touch()
    main.unlink()
    sources = source, source.parent / "other.json"
    before = auth_state(*sources)
    shown = []

    def choose(words):
        shown.extend(words)
        return "remove"

    proof = credentialmod.release(ship, tmux, seat.name, "operator", choose)
    assert "  auth.json: missing" in shown
    assert f"  config/muse/auth.json: link -> {str(sources[1])!r}" in shown
    assert "held home removed" in proof
    assert not seat.home.exists()
    if temporary:
        assert not seat.folder.exists()
    else:
        assert (seat.folder / "other-seat-data").exists()
    assert auth_state(*sources) == before
    assert credentialmod.held_words(ship) == []
    start, done = [json.loads(row) for row in acts.path(seat).read_text().splitlines()]
    assert start["outcome"] == "pending" and start["choice"] == "remove"
    assert start["act"] == done["act"] and done["outcome"] == "remove"


def test_release_refuses_an_up_seat_before_offering_a_choice(muse_ship):
    seat, names, _ = held_muse(muse_ship, "nested-file")
    before = auth_state(*names)
    pane = Pane("%1", "crew", seat.name, 101, 80, 24, False, False, None)
    seat.tmux.panes[pane.id] = pane
    seat.trusted.mkdir(parents=True, exist_ok=True)
    Launched(pane.id, pane.pid, post.now().isoformat(), seat.pool.name, None, ["muse"], "synthetic", "muse").write(
        seat.trusted / "launch.json")
    with pytest.raises(ShipError, match="still up"):
        credentialmod.release(seat.ship, seat.tmux, seat.name, "operator", lambda _: pytest.fail("offered a choice"))
    assert auth_state(*names) == before
    assert credentialmod.held_for_review(seat.ship, seat.name)
    assert acts.last(seat) is None


def test_release_refuses_a_home_that_changed_during_the_choice(muse_ship):
    seat, _, _ = held_muse(muse_ship, "attached-file")

    def choose(_):
        seat.home.rename(seat.home.with_name("previous-home"))
        seat.home.mkdir()
        return "remove"

    with pytest.raises(ShipError, match="held home changed"):
        credentialmod.release(seat.ship, seat.tmux, seat.name, "operator", choose)
    assert seat.home.is_dir()
    assert seat.home.with_name("previous-home").is_dir()
    assert credentialmod.held_for_review(seat.ship, seat.name)
    assert acts.last(seat) is None


def test_failed_removal_preserves_the_review_hold_and_records_the_failure(muse_ship, monkeypatch):
    seat, names, _ = held_muse(muse_ship, "attached-file")
    before = auth_state(*names)

    def refuse(*_args, **_kwargs):
        raise PermissionError("synthetic write failure")

    monkeypatch.setattr(shutil, "rmtree", refuse)
    with pytest.raises(ShipError, match="write failure.*review hold stays"):
        credentialmod.release(seat.ship, seat.tmux, seat.name, "operator", lambda _: "remove")
    assert auth_state(*names) == before
    assert credentialmod.held_for_review(seat.ship, seat.name)
    assert acts.last(seat)["outcome"] == "failed"


def test_release_requires_an_explicit_choice(muse_ship):
    seat, names, _ = held_muse(muse_ship, "attached-file")
    before = auth_state(*names)
    with pytest.raises(ShipError, match="choose keep or remove"):
        credentialmod.release(seat.ship, seat.tmux, seat.name, "operator", lambda _: "")
    assert auth_state(*names) == before
    assert credentialmod.held_for_review(seat.ship, seat.name)
    assert acts.last(seat) is None


def test_keep_does_not_clear_review_if_the_home_changes_before_the_service_pass(muse_ship):
    seat, (main, _), service = held_muse(muse_ship, "attached-file")
    credentialmod.release(seat.ship, seat.tmux, seat.name, "operator", lambda _: "keep")
    main.unlink()
    main.write_bytes(b"synthetic later file")
    service.credentials()
    service.credentials()
    assert credentialmod.held_for_review(seat.ship, seat.name)
    assert main.read_bytes() == b"synthetic later file"
