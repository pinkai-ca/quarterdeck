"""A refresh a program left in its seat's home reaches the pool before the seat's launch is forgotten.

Synthetic Claude credentials in a synthetic pool folder, a fake tmux and no service running: a restart
and a check's temporary seat each end with the pool holding the newer credential, and a home is never
built over a detached credential newer than its pool's.
"""

import io
import json
import os
import stat
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
role="worker"
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
        assert f"home held for credential review: qd credential release {seat.name}" in line
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
        assert f"home held for credential review: qd credential release {seat.name}" in line


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


def with_systems(ship):
    systems = replace(ship.seat("muse-worker"), name="systems", role="systems")
    return replace(ship, seats={**ship.seats, systems.name: systems})


def release_cli(seat, monkeypatch, caller, choice):
    ship = with_systems(seat.ship) if caller == "systems" else seat.ship
    monkeypatch.setattr(cli, "handoff", lambda *_: None)
    monkeypatch.setattr(cli, "load_ship", lambda _: ship)
    monkeypatch.setattr(cli.Tmux, "for_ship", lambda _: seat.tmux)
    if caller is None:
        monkeypatch.delenv("QD_SEAT", raising=False)
    else:
        monkeypatch.setenv("QD_SEAT", caller)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(f"{choice}\n"))
    return ["--ship", str(seat.ship.folder), "credential", "release", seat.name]


@pytest.mark.parametrize("caller", [None, "systems"], ids=["operator", "systems"])
def test_release_command_displays_metadata_and_clears_the_hold_before_the_service_pass(
    muse_ship, monkeypatch, capsys, caller,
):
    seat, (main, nested), service = held_muse(muse_ship, "attached-file")
    before = auth_state(main, nested)
    runtime = seat.ship.runtime
    # Read an already-written review from before the release command existed.
    path = runtime / credentialmod.HELD / f"{seat.name}.json"
    path.write_text(json.dumps(dict(seat=seat.name, pool=seat.pool.name, review=True)))
    line = watch.facts(seat.ship, seat.tmux).line()
    assert f"qd credential release {seat.name}" in line
    assert "systems keep; operator archives" in line
    arguments = release_cli(seat, monkeypatch, caller, "keep")

    with monkeypatch.context() as patch:
        for method in ("open", "read_bytes", "read_text"):
            read = getattr(Path, method)

            def metadata_only(self, *args, read=read, **kwargs):
                assert self.name != "auth.json", "release opened a sign-in file"
                return read(self, *args, **kwargs)

            patch.setattr(Path, method, metadata_only)
        assert cli.main(arguments) == 0
    out = capsys.readouterr()
    assert "auth.json: file" in out.out
    assert f"config/muse/auth.json: link -> {str(main)!r}" in out.out
    assert "synthetic detached file" not in out.out
    assert ("[keep/remove]" if caller is None else "[keep]") in out.err
    assert "home kept in place; credential review cleared" in out.out
    assert auth_state(main, nested) == before
    assert not credentialmod.held_for_review(seat.ship, seat.name)
    assert "credential review" not in watch.facts(seat.ship, seat.tmux).line()
    acknowledged = path.read_text()
    assert json.loads(acknowledged)["keep"]["names"].keys() == {"auth.json", "config/muse/auth.json"}
    act = acts.last(seat)
    assert (act["verb"], act["choice"], act["outcome"], act["caller"]) == (
        "credential-release", "keep", "keep", caller or seat.ship.operator or "the operator")
    assert "credential review" in acts.describe(act)
    service.credentials()
    assert auth_state(main, nested) == before
    assert not credentialmod.held_for_review(seat.ship, seat.name)
    assert path.read_text() == acknowledged
    assert "credential review" not in watch.facts(seat.ship, seat.tmux).line()


@pytest.mark.parametrize("caller", ["muse-worker", "unknown", ""])
def test_release_refuses_an_unauthorized_seat_before_inspection(muse_ship, monkeypatch, capsys, caller):
    seat, names, _ = held_muse(muse_ship, "attached-file")
    before = auth_state(*names)
    arguments = release_cli(seat, monkeypatch, caller, "keep")
    monkeypatch.setattr(credentialmod, "_release_view", lambda *a, **k: pytest.fail("inspected the held home"))
    assert cli.main(arguments) == 2
    out = capsys.readouterr()
    assert not out.out
    assert "only a systems seat may keep" in out.err
    assert "auth.json" not in out.err
    assert credentialmod.held_for_review(seat.ship, seat.name)
    assert acts.last(seat) is None
    assert auth_state(*names) == before


def test_release_refuses_systems_remove(muse_ship, monkeypatch, capsys):
    seat, names, _ = held_muse(muse_ship, "attached-file")
    before = auth_state(*names)
    assert cli.main(release_cli(seat, monkeypatch, "systems", "remove")) == 2
    out = capsys.readouterr()
    assert "[keep]" in out.err and "[keep/remove]" not in out.err
    assert "only the operator may choose remove" in out.err
    assert credentialmod.held_for_review(seat.ship, seat.name)
    assert acts.last(seat) is None
    assert auth_state(*names) == before
    assert not (seat.ship.runtime / credentialmod.RECOVERY).exists()


@pytest.mark.parametrize("caller, choice, refused", [
    (None, "keep", "an unreadable credential name"),
    ("systems", "keep", "an unreadable credential name"),
    ("systems", "remove", "only the operator may choose remove"),
    (None, "remove", None),
])
def test_release_of_a_home_with_an_unreadable_credential_name(muse_ship, monkeypatch, capsys, caller, choice, refused):
    seat, (main, nested), _ = held_muse(muse_ship, "attached-file")
    local = seat.home / "local-data"
    local.write_bytes(b"synthetic home data\n")
    paths = main, nested, local
    before = auth_state(*paths)
    identity = seat.home.stat().st_ino
    name_info = credentialmod._name_info

    def inspect(path):
        if path == nested:
            raise PermissionError("synthetic metadata failure")
        return name_info(path)

    monkeypatch.setattr(credentialmod, "_name_info", inspect)
    status = cli.main(release_cli(seat, monkeypatch, caller, choice))
    out = capsys.readouterr()
    assert "  config/muse/auth.json: unreadable" in out.out
    if refused:
        assert status == 2 and refused in out.err
        assert credentialmod.held_for_review(seat.ship, seat.name)
        assert acts.last(seat) is None
        assert not (seat.ship.runtime / credentialmod.RECOVERY).exists()
        assert auth_state(*paths) == before
    else:
        assert status == 0
        assert not credentialmod.held_for_review(seat.ship, seat.name)
        assert not seat.home.exists()
        act = acts.last(seat)
        archive = Path(act["planned_recovery"])
        assert act["outcome"] == "remove"
        assert archive.stat().st_ino == identity
        assert auth_state(*(archive / path.relative_to(seat.home) for path in paths)) == before


def test_release_refuses_systems_keep_for_a_human_session_before_inspection(muse_ship, monkeypatch):
    seat, _, _ = held_muse(muse_ship, "attached-file")
    human = replace(seat.session, human=True)
    ship = with_systems(replace(seat.ship, sessions={**seat.ship.sessions, human.name: human}))
    monkeypatch.setattr(credentialmod, "_snapshot", lambda *_: pytest.fail("inspected the held home"))
    with pytest.raises(ShipError, match="only the operator may release a home in a human's session"):
        credentialmod.release(ship, seat.tmux, seat.name, "systems", lambda _: pytest.fail("offered a choice"))
    assert credentialmod.held_for_review(ship, seat.name)
    assert acts.last(seat) is None


def test_keeping_a_temporary_home_clears_both_the_hold_and_leftover_alert(muse_ship):
    ship, tmux, source = muse_ship
    spec = SeatSpec("check-muse-0001", "qd-check-muse-0001", "worker", "muse", ("muse-primary",),
                    model="muse-wonder-1.0", cwd=ship.workspace, sandbox=True)
    with temporary_seat(ship, tmux, spec, [], "qd check") as seat:
        names = setup_muse_auth(seat.home, source, "foreign-link")
        before = auth_state(*names)
    credentialmod.release(ship, tmux, seat.name, None, lambda _: "keep")
    assert not credentialmod.held_for_review(ship, seat.name)
    Service(ship, tmux).credentials()
    assert auth_state(*names) == before
    assert not credentialmod.held_for_review(ship, seat.name)
    assert not any(seat.name in line for line in seatmod.leftover_seats(ship))


@pytest.mark.parametrize("temporary", [False, True], ids=["declared", "temporary"])
def test_release_moves_the_held_home_and_keeps_the_recovery_visible(muse_ship, temporary, monkeypatch, capsys):
    ship, tmux, source = muse_ship
    if temporary:
        spec = SeatSpec("check-muse-0002", "qd-check-muse-0002", "worker", "muse", ("muse-primary",),
                        model="muse-wonder-1.0", cwd=ship.workspace)
        with temporary_seat(ship, tmux, spec, [], "qd check") as seat:
            main, link = setup_muse_auth(seat.home, source, "foreign-link")
    else:
        seat, (main, link), _ = held_muse(muse_ship, "foreign-link")
    (seat.folder / "other-seat-data").write_bytes(b"synthetic seat data\n")
    (seat.home / "local-data").write_bytes(b"synthetic home data\n")
    unrelated = seat.folder.parent / "unrelated"
    unrelated.write_bytes(b"synthetic unrelated data\n")
    shared = ship.runtime / "evidence"
    shared.mkdir()
    shared.chmod(0o775)  # Other evidence writers use the operator's usual umask.
    (shared / "unrelated").write_bytes(b"synthetic unrelated evidence\n")
    assert credentialmod.recovery_words(ship.runtime) == []
    main.unlink()
    sources = source, source.parent / "other.json"
    before = auth_state(*sources)
    moved = seat.folder if temporary else seat.home
    identity = moved.stat().st_dev, moved.stat().st_ino
    home_before = auth_state(seat.home / "local-data", link)
    target = SimpleNamespace(ship=ship, name=seat.name, tmux=tmux)
    assert cli.main(release_cli(target, monkeypatch, None, "remove")) == 0
    out = capsys.readouterr()
    assert "  auth.json: missing" in out.out
    assert f"  config/muse/auth.json: link -> {str(sources[1])!r}" in out.out
    start, done = [json.loads(row) for row in acts.path(seat).read_text().splitlines()]
    assert start["outcome"] == "pending" and start["choice"] == "remove"
    assert start["act"] == done["act"] and done["outcome"] == "remove"
    recovery = Path(done["recovery"])
    assert start["planned_recovery"] == str(recovery)
    assert f"held home moved to {recovery}" in out.out
    assert recovery.parent.parent == ship.runtime / credentialmod.RECOVERY
    assert stat.S_IMODE(recovery.parent.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(recovery.parent.stat().st_mode) == 0o700
    assert (recovery.stat().st_dev, recovery.stat().st_ino) == identity
    assert not seat.home.exists()
    if temporary:
        assert not seat.folder.exists()
        assert (recovery / "other-seat-data").read_bytes() == b"synthetic seat data\n"
        recovered_home = recovery / "home"
    else:
        assert (seat.folder / "other-seat-data").read_bytes() == b"synthetic seat data\n"
        recovered_home = recovery
    assert auth_state(recovered_home / "local-data", recovered_home / link.relative_to(seat.home)) == home_before
    assert unrelated.read_bytes() == b"synthetic unrelated data\n"
    assert stat.S_IMODE(shared.stat().st_mode) == 0o775
    assert (shared / "unrelated").read_bytes() == b"synthetic unrelated evidence\n"
    assert auth_state(*sources) == before
    Service(ship, tmux).credentials()
    assert not credentialmod.held_for_review(ship, seat.name)
    relative = recovery.relative_to(ship.folder)
    assert f"recovery at {relative}" in watch.facts(ship, tmux).line()


def test_release_refuses_an_up_seat_before_offering_a_choice(muse_ship):
    seat, names, _ = held_muse(muse_ship, "nested-file")
    before = auth_state(*names)
    pane = Pane("%1", "crew", seat.name, 101, 80, 24, False, False, None)
    seat.tmux.panes[pane.id] = pane
    seat.trusted.mkdir(parents=True, exist_ok=True)
    Launched(pane.id, pane.pid, post.now().isoformat(), seat.pool.name, None, ["muse"], "synthetic", "muse").write(
        seat.trusted / "launch.json")
    with pytest.raises(ShipError, match="still up"):
        credentialmod.release(seat.ship, seat.tmux, seat.name, None, lambda _: pytest.fail("offered a choice"))
    assert auth_state(*names) == before
    assert credentialmod.held_for_review(seat.ship, seat.name)
    assert acts.last(seat) is None


@pytest.mark.parametrize("choice", ["keep", "remove"])
def test_release_refuses_a_home_that_changed_during_the_choice(muse_ship, choice):
    seat, _, _ = held_muse(muse_ship, "attached-file")

    def choose(_):
        seat.home.rename(seat.home.with_name("previous-home"))
        seat.home.mkdir()
        return choice

    with pytest.raises(ShipError, match="held home changed"):
        credentialmod.release(seat.ship, seat.tmux, seat.name, None, choose)
    assert seat.home.is_dir()
    assert seat.home.with_name("previous-home").is_dir()
    assert credentialmod.held_for_review(seat.ship, seat.name)
    assert acts.last(seat) is None


def test_failed_removal_preserves_the_review_hold_and_records_the_failure(muse_ship, monkeypatch):
    seat, names, _ = held_muse(muse_ship, "attached-file")
    before = auth_state(*names)

    def refuse(*_args, **_kwargs):
        raise PermissionError("synthetic write failure")

    monkeypatch.setattr(os, "rename", refuse)
    with pytest.raises(ShipError, match="write failure.*review hold stays"):
        credentialmod.release(seat.ship, seat.tmux, seat.name, None, lambda _: "remove")
    assert auth_state(*names) == before
    assert credentialmod.held_for_review(seat.ship, seat.name)
    assert acts.last(seat)["outcome"] == "failed"
    assert list((seat.ship.runtime / credentialmod.RECOVERY).iterdir()) == []


def test_release_rechecks_the_source_directory_at_the_move(muse_ship, monkeypatch):
    seat, names, _ = held_muse(muse_ship, "attached-file")
    before = auth_state(*names)
    former = seat.home.with_name("former-home")
    write = acts.write

    def record(audited, act):
        write(audited, act)
        if act["outcome"] == "pending":
            seat.home.rename(former)
            seat.home.mkdir()

    monkeypatch.setattr(acts, "write", record)
    with pytest.raises(ShipError, match="held home changed before the move.*review hold stays"):
        credentialmod.release(seat.ship, seat.tmux, seat.name, None, lambda _: "remove")
    assert seat.home.is_dir()
    assert auth_state(*(former / name.relative_to(seat.home) for name in names)) == before
    assert credentialmod.held_for_review(seat.ship, seat.name)
    assert acts.last(seat)["outcome"] == "failed"


@pytest.mark.parametrize("failure", ["hold", "outcome"])
def test_release_reports_the_recovery_even_when_recording_fails(muse_ship, monkeypatch, failure):
    seat, names, service = held_muse(muse_ship, "attached-file")
    before = auth_state(*names)
    record = seat.ship.runtime / credentialmod.HELD / f"{seat.name}.json"
    write, unlink = acts.write, Path.unlink

    def refuse_outcome(audited, entry):
        if entry["outcome"] == "remove":
            raise PermissionError("synthetic outcome write failure")
        write(audited, entry)

    def refuse_unlink(path, *args, **kwargs):
        if path == record:
            raise PermissionError("synthetic hold update failure")
        unlink(path, *args, **kwargs)

    if failure == "hold":
        monkeypatch.setattr(Path, "unlink", refuse_unlink)
    else:
        monkeypatch.setattr(acts, "write", refuse_outcome)
    with pytest.raises(ShipError, match="held home moved to .*synthetic") as error:
        credentialmod.release(seat.ship, seat.tmux, seat.name, None, lambda _: "remove")
    act = acts.last(seat)
    recovery = Path(act["planned_recovery"])
    assert str(recovery) in str(error.value)
    assert not seat.home.exists()
    assert auth_state(*(recovery / name.relative_to(seat.home) for name in names)) == before
    service.credentials()
    assert credentialmod.held_for_review(seat.ship, seat.name) is (failure == "hold")
    assert act["outcome"] == ("failed" if failure == "hold" else "pending")
    line = f"recovery at {recovery.relative_to(seat.ship.folder)}"
    facts = watch.facts(seat.ship, seat.tmux)
    assert line in facts.credential_recoveries
    assert line in facts.line()


def move_with_stale_hold(muse_ship, monkeypatch, *, temporary=False):
    ship, tmux, source = muse_ship
    if temporary:
        spec = SeatSpec("check-muse-0003", "qd-check-muse-0003", "worker", "muse", ("muse-primary",),
                        model="muse-wonder-1.0", cwd=ship.workspace)
        with temporary_seat(ship, tmux, spec, [], "qd check") as seat:
            paths = setup_muse_auth(seat.home, source, "attached-file")
    else:
        seat, paths, _ = held_muse(muse_ship, "attached-file")
    original = auth_state(*paths)
    record = ship.runtime / credentialmod.HELD / f"{seat.name}.json"
    unlink = Path.unlink

    def refuse_hold(path, *args, **kwargs):
        if path == record:
            raise PermissionError("synthetic hold update failure")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", refuse_hold)
        with pytest.raises(ShipError, match="held home moved to .*synthetic hold update failure"):
            credentialmod.release(ship, tmux, seat.name, None, lambda _: "remove")
    assert record.is_file() and not seat.home.exists()
    archive = Path(acts.last(seat)["planned_recovery"])
    home = archive / "home" if temporary else archive
    saved = [home / path.relative_to(seat.home) for path in paths]
    assert auth_state(*saved) == original
    return seat, record, archive, saved, original


@pytest.mark.parametrize("history", ["current", "temporary", "landed-failed", "landed-pending"])
def test_operator_can_finish_a_proved_prior_move(muse_ship, monkeypatch, history):
    temporary = history == "temporary"
    ship, tmux, _ = muse_ship
    seat, record, archive, saved, original = move_with_stale_hold(muse_ship, monkeypatch, temporary=temporary)
    audit = acts.path(seat)
    start, failed = [json.loads(line) for line in audit.read_text().splitlines()]
    before = archive.stat()
    if history.startswith("landed-"):
        del start["source_identity"], start["review_identity"], failed["recovery"]
        previous = [start] if history == "landed-pending" else [start, failed]
        audit.write_text("".join(json.dumps(row) + "\n" for row in previous))

    def choose(words):
        assert any("source directory is missing" in line for line in words)
        return "remove"

    with monkeypatch.context() as patch:
        patch.setattr(credentialmod, "_move_home", lambda *a, **k: pytest.fail("repeated the move"))
        patch.setattr(os, "listdir", lambda *a, **k: pytest.fail("listed evidence"))
        detail = credentialmod.release(ship, tmux, seat.name, None, choose)
    assert f"earlier move verified at {archive}" in detail
    assert not record.exists()
    assert not seat.home.exists()
    registered = json.loads(audit.read_text().splitlines()[-1])
    assert (registered["act"], registered["outcome"], registered["recovery"]) == (start["act"], "remove", str(archive))
    assert archive.stat() == before
    assert auth_state(*saved) == original


@pytest.mark.parametrize("choice, reason", [("keep", "held home is missing"), ("remove", "only the operator")])
def test_systems_cannot_finish_a_prior_move(muse_ship, monkeypatch, choice, reason):
    seat, record, archive, saved, original = move_with_stale_hold(muse_ship, monkeypatch)
    audit = acts.path(seat).read_bytes()
    monkeypatch.setattr(credentialmod, "_release_attempts", lambda *a: pytest.fail("inspected recovery acts"))
    with pytest.raises(ShipError, match=reason):
        credentialmod.release(with_systems(seat.ship), seat.tmux, seat.name, "systems", lambda _: choice)
    assert record.exists() and archive.exists()
    assert auth_state(*saved) == original
    assert acts.path(seat).read_bytes() == audit


@pytest.mark.parametrize("problem, reason", [
    ("missing", "No such file"),
    ("different inode", "directory identity differs"),
    ("ambiguous", "more than one recorded move"),
    ("different hold", "no matching recorded move"),
    ("landed temporary", "no matching recorded move"),
])
def test_operator_cannot_finish_an_unproved_move(muse_ship, monkeypatch, problem, reason):
    ship, tmux, _ = muse_ship
    seat, record, archive, saved, original = move_with_stale_hold(muse_ship, monkeypatch,
                                                                  temporary=problem == "landed temporary")
    act_path = acts.path(seat)
    start, failed = [json.loads(line) for line in act_path.read_text().splitlines()]
    if problem in {"missing", "different inode"}:
        displaced = archive.rename(archive.with_name("displaced"))
        saved = [displaced / path.relative_to(archive) for path in saved]
        if problem == "different inode":
            archive.mkdir()
    elif problem == "ambiguous":
        acts.write(seat, {**start, "act": "another-start"})
    elif problem == "different hold":
        credentialmod.durable.replace_file(record, record.read_text())
    else:
        del start["source_identity"], start["review_identity"], failed["recovery"]
        act_path.write_text(json.dumps(start) + "\n" + json.dumps(failed) + "\n")
    audit = act_path.read_bytes()
    hold = record.read_bytes()

    with pytest.raises(ShipError, match=f"{reason}.*review hold stays"):
        credentialmod.release(ship, tmux, seat.name, None, lambda _: "remove")
    assert not seat.home.exists()
    assert record.read_bytes() == hold
    assert act_path.read_bytes() == audit
    assert auth_state(*saved) == original


def test_release_requires_an_explicit_choice(muse_ship):
    seat, names, _ = held_muse(muse_ship, "attached-file")
    before = auth_state(*names)
    with pytest.raises(ShipError, match="choose keep or remove"):
        credentialmod.release(seat.ship, seat.tmux, seat.name, None, lambda _: "")
    assert auth_state(*names) == before
    assert credentialmod.held_for_review(seat.ship, seat.name)
    assert acts.last(seat) is None


def test_failed_keep_snapshot_preserves_the_review_hold_and_records_the_failure(muse_ship, monkeypatch):
    seat, names, service = held_muse(muse_ship, "attached-file")
    before = auth_state(*names)
    record = seat.ship.runtime / credentialmod.HELD / f"{seat.name}.json"
    previous = record.read_text()
    replace_file = credentialmod.durable.replace_file

    def refuse_keep(path, data, **kwargs):
        if path == record:
            raise PermissionError("synthetic snapshot write failure")
        return replace_file(path, data, **kwargs)

    monkeypatch.setattr(credentialmod.durable, "replace_file", refuse_keep)
    with pytest.raises(ShipError, match="snapshot write failure.*review hold stays"):
        credentialmod.release(seat.ship, seat.tmux, seat.name, None, lambda _: "keep")
    assert record.read_text() == previous
    assert acts.last(seat)["outcome"] == "failed"
    service.credentials()
    assert credentialmod.held_for_review(seat.ship, seat.name)
    assert auth_state(*names) == before


def test_failed_keep_outcome_restores_the_review_hold(muse_ship, monkeypatch):
    seat, names, service = held_muse(muse_ship, "attached-file")
    before = auth_state(*names)
    record = seat.ship.runtime / credentialmod.HELD / f"{seat.name}.json"
    previous = record.read_text()
    write = acts.write

    def refuse_outcome(audited, entry):
        if entry["outcome"] == "keep":
            assert not credentialmod.held_for_review(seat.ship, seat.name)
            raise PermissionError("synthetic outcome write failure")
        write(audited, entry)

    monkeypatch.setattr(acts, "write", refuse_outcome)
    with pytest.raises(ShipError, match="outcome write failure.*review hold stays"):
        credentialmod.release(seat.ship, seat.tmux, seat.name, None, lambda _: "keep")
    assert record.read_text() == previous
    assert acts.last(seat)["outcome"] == "pending"
    service.credentials()
    assert credentialmod.held_for_review(seat.ship, seat.name)
    assert auth_state(*names) == before


def test_service_holds_changed_metadata_again_after_keep(muse_ship):
    seat, (main, _), service = held_muse(muse_ship, "attached-file")
    credentialmod.release(seat.ship, seat.tmux, seat.name, None, lambda _: "keep")
    assert not credentialmod.held_for_review(seat.ship, seat.name)
    main.unlink()
    main.write_bytes(b"synthetic later file")
    service.credentials()
    assert credentialmod.held_for_review(seat.ship, seat.name)
    assert main.read_bytes() == b"synthetic later file"
