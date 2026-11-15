"""Grok's shared framework paths through their real callers.

Synthetic records, credentials, panes and programs only: no Grok process, provider,
model or tmux server is used, and no operator path is read.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import credential, pending, quota, seat as seat_module
from quarterdeck.harness import grok
from quarterdeck.harness.base import Usage
from quarterdeck.harness.grok import GrokHarness
from quarterdeck.seat import Launched, Seat
from quarterdeck.service import Service
from quarterdeck.ship import Pool, SeatSpec, Session, Ship
from quarterdeck.tmux import Pane, Tmux

RECEIPTS = Path(__file__).parents[1] / "fixtures/receipt-time"
PAYLOAD = "qd > answer from synthetic · ans-a1b2\nReturn a synthetic greeting."
SECOND = datetime(2040, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
READER = "usage reader for synthetic-plan"


@pytest.fixture(autouse=True)
def synthetic_native_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(grok, "SYSTEM_CONFIG", tmp_path / "synthetic-system")
    for name in grok.INHERITED_REDIRECTS:
        monkeypatch.delenv(name, raising=False)


def grant(token: str, expires: str) -> str:
    return json.dumps({"synthetic-grant": {"refresh_token": token, "expires_at": expires}})


def grok_ship(tmp_path, seats=None):
    pool_dir = tmp_path / "synthetic-pool"
    pool_dir.mkdir()
    (pool_dir / "auth.json").write_text(grant("pool", "2040-01-01T00:00:00Z"))
    pool = Pool("synthetic-plan", "xai", config_dir=pool_dir)
    ship = Ship(tmp_path, "synthetic", "admiral", tmp_path, {pool.name: pool},
                {"crew": Session("crew", "crew")}, seats or {})
    return ship, pool, pool_dir / "auth.json"


def reader_file(ship, pool) -> Path:
    home = credential.usage_home(ship, pool)
    home.mkdir(parents=True)
    return home / "auth.json"


# -- receipt precision through the saved proof that every withheld delivery is judged by -------------

@pytest.mark.parametrize("harness,fixture,micro,expected", [
    ("grok", "grok-same-second", 789123, "delivered"),
    ("grok", "grok-previous-second", 789123, None),
    ("claude", "claude-fractional", 456000, "delivered"),
    ("claude", "claude-fractional", 456001, None),
])
def test_saved_proof_confirms_at_its_adapter_precision_and_keeps_its_attempt_time(tmp_path, harness, fixture, micro, expected):
    since = SECOND.replace(microsecond=micro).isoformat()
    proof = pending.Proof(tmp_path / "proof.json", {"identity": {"harness": harness},
        "record": str(RECEIPTS / f"{fixture}.jsonl"), "payload": PAYLOAD, "since": since})
    assert proof.confirmed(SimpleNamespace()) == expected
    assert proof.data["since"] == since, "the saved attempt time is never floored"
    assert not proof.path.exists(), "confirming an owned record writes nothing"


# -- styled screen evidence is opt-in, in the same atomic identity read ----------------------------------

@pytest.mark.parametrize("styles", [False, True])
def test_tmux_adds_style_escapes_only_when_asked_and_keeps_one_queue(styles):
    calls = []
    tmux = Tmux()
    tmux.run = lambda *args, stdin=None: calls.append(args) or ""
    options = {"styles": True} if styles else {}
    tmux.capture("%3", **options)
    tmux.read_pane("%3", **options)
    capture, atomic = calls
    assert ("-e" in capture) is styles and ("-e" in atomic) is styles
    assert atomic.count("capture-pane") == 1 and atomic[0] == "display-message", "identity and screen in one command"


def launched_seat(tmp_path, tmux):
    pool = Pool("synthetic-fake", "fake")
    spec = SeatSpec("worker", "crew", "worker", "fake", (pool.name,), cwd=tmp_path)
    ship = Ship(tmp_path, "synthetic", "admiral", tmp_path, {pool.name: pool},
                {"crew": Session("crew", "crew")}, {"worker": spec})
    seat = Seat(ship, spec, tmux)
    seat.folder.mkdir(parents=True)
    seat.trusted.mkdir(parents=True, exist_ok=True)
    Launched("%3", 1003, SECOND.isoformat(), pool.name, None, ["fake"], "synthetic").write(seat.trusted / "launch.json")
    return seat


PANE = Pane("%3", "crew", "worker", 1003, 80, 24, False, False, None)


@pytest.mark.parametrize("styled", [False, True])
def test_seat_atomic_read_passes_styles_only_for_an_opted_in_adapter(tmp_path, styled):
    seen = []

    def legacy(pane, history=0):  # an existing double that predates the option
        seen.append(None)
        return PANE, "frame"

    def current(pane, history=0, *, styles=False):
        seen.append(styles)
        return PANE, "frame"

    seat = launched_seat(tmp_path, SimpleNamespace(read_pane=current if styled else legacy))
    if styled:
        seat.harness = GrokHarness()
    assert seat.read_pane() == (PANE, "frame")
    assert seen == [True if styled else None]


@pytest.mark.parametrize("styled", [False, True])
def test_seat_capture_fallback_passes_styles_only_for_an_opted_in_adapter(tmp_path, styled):
    seen = []

    def capture(pane_id, history=0, **options):
        seen.append(options)
        return "frame"

    seat = launched_seat(tmp_path, SimpleNamespace(pane=lambda ident: PANE, capture=capture))
    if styled:
        seat.harness = GrokHarness()
    assert seat.read_pane() == (PANE, "frame")
    assert seen == [{"styles": True} if styled else {}]


# -- the declared usage reader takes part in the one credential reseed --------------------------------

def test_a_newer_usage_reader_refresh_reseeds_the_pool_and_its_copy_stands(tmp_path):
    ship, pool, source = grok_ship(tmp_path)
    link = reader_file(ship, pool)
    link.write_text(grant("reader", "2040-03-01T00:00:00Z"))
    proofs = credential.reseed(ship, [])
    assert len(proofs) == 1 and proofs[0].startswith(f"synthetic-plan reseeded from {READER}'s refresh")
    assert not link.is_symlink() and link.read_bytes() == source.read_bytes()
    assert json.loads(source.read_text())["synthetic-grant"]["refresh_token"] == "reader"
    assert credential.reseed(ship, []) == [], "a copy as fresh as the pool's is left alone on every later pass"


def test_an_older_usage_reader_copy_is_brought_up_to_the_pool_without_reseeding(tmp_path):
    ship, pool, source = grok_ship(tmp_path)
    link = reader_file(ship, pool)
    link.write_text(grant("older", "2039-01-01T00:00:00Z"))
    assert credential.reseed(ship, []) == [f"{READER} copy brought up to synthetic-plan"]
    assert not link.is_symlink() and link.read_bytes() == source.read_bytes()
    assert json.loads(source.read_text())["synthetic-grant"]["refresh_token"] == "pool"


def test_an_unreadable_usage_reader_file_is_retained_while_a_seat_stub_is_brought_up(tmp_path):
    ship, pool, source = grok_ship(tmp_path)
    link = reader_file(ship, pool)
    link.write_text("{}")
    home = tmp_path / "runtime" / "seats" / "worker" / "home"
    home.mkdir(parents=True)
    (home / "auth.json").write_text("{}")
    worker = SimpleNamespace(name="worker", pool=pool, home=home, launched=lambda: SimpleNamespace(pool=pool.name))
    proofs = credential.reseed(ship, [worker])
    assert proofs == ["worker copy brought up to synthetic-plan",
                      f"{READER} credential unreadable; detached file retained for a human"]
    assert not link.is_symlink() and link.read_text() == "{}"
    assert not (home / "auth.json").is_symlink() and (home / "auth.json").read_bytes() == source.read_bytes()
    assert json.loads(source.read_text())["synthetic-grant"]["refresh_token"] == "pool"


def test_a_linked_usage_home_is_not_a_participant(tmp_path):
    ship, pool, source = grok_ship(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "auth.json").write_text(grant("elsewhere", "2040-03-01T00:00:00Z"))
    home = credential.usage_home(ship, pool)
    home.parent.mkdir(parents=True)
    home.symlink_to(elsewhere)
    assert [p.name for p in credential.participants(ship, [])] == []
    assert credential.reseed(ship, []) == []
    assert json.loads(source.read_text())["synthetic-grant"]["refresh_token"] == "pool"
    assert not (elsewhere / "auth.json").is_symlink()


# -- one usage orchestration for every read of a native usage client -----------------------------------

def usage_service(ship, logs):
    # read_usage first completes any refused usage commit; this stand-in's ledger has none.
    return SimpleNamespace(ship=ship, resolved_seats=lambda: [], log=lambda kind, **fields: logs.append((kind, fields)),
                           ledger=SimpleNamespace(prepare_usage=lambda: None))


@pytest.fixture
def events(monkeypatch):
    found = []
    real = credential.reseed
    monkeypatch.setattr(credential, "reseed", lambda ship, seats, **bounds: found.append("reseed") or real(ship, seats, **bounds))
    return found


def test_native_read_reconciles_before_and_after_in_its_owned_home(tmp_path, monkeypatch, events):
    ship, pool, source = grok_ship(tmp_path)
    link = reader_file(ship, pool)
    link.write_text(grant("reader", "2040-03-01T00:00:00Z"))  # a refresh the last read left detached
    expected = Usage((), SECOND)

    def read(self, read_pool, home):
        copy = home / "auth.json"
        events.append(("read", home, not copy.is_symlink() and copy.read_bytes() == source.read_bytes()))
        return expected

    monkeypatch.setattr(GrokHarness, "usage_in_home", read)
    logs = []
    assert quota.read_usage(usage_service(ship, logs), pool, GrokHarness()) is expected
    assert events == ["reseed", ("read", credential.usage_home(ship, pool), True), "reseed"]
    assert json.loads(source.read_text())["synthetic-grant"]["refresh_token"] == "reader"
    assert any(kind == "credential" and fields["proof"].startswith(f"synthetic-plan reseeded from {READER}'s refresh")
               for kind, fields in logs)


def test_a_failed_native_read_still_reconciles_afterwards(tmp_path, monkeypatch, events):
    ship, pool, source = grok_ship(tmp_path)
    reader_file(ship, pool)

    def read(self, read_pool, home):
        events.append("read")
        raise RuntimeError("synthetic billing failure")

    monkeypatch.setattr(GrokHarness, "usage_in_home", read)
    with pytest.raises(RuntimeError, match="synthetic billing failure"):
        quota.read_usage(usage_service(ship, []), pool, GrokHarness())
    assert events == ["reseed", "read", "reseed"]


def test_an_unreadable_detached_reader_credential_makes_no_native_call(tmp_path, monkeypatch, events):
    ship, pool, source = grok_ship(tmp_path)
    reader_file(ship, pool).write_text("{}")
    monkeypatch.setattr(GrokHarness, "usage_in_home", lambda *args: pytest.fail("no native call over a retained file"))
    usage = quota.read_usage(usage_service(ship, []), pool, GrokHarness())
    assert usage.called is False and usage.error == "native usage credential detached and unreadable"
    assert events == ["reseed"]


def test_a_linked_usage_home_makes_no_reseed_and_no_native_call(tmp_path, monkeypatch, events):
    ship, pool, source = grok_ship(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    home = credential.usage_home(ship, pool)
    home.parent.mkdir(parents=True)
    home.symlink_to(elsewhere)
    monkeypatch.setattr(GrokHarness, "usage_in_home", lambda *args: pytest.fail("no native call in a linked home"))
    usage = quota.read_usage(usage_service(ship, []), pool, GrokHarness())
    assert usage.called is False and "ownership unknown" in usage.error
    assert events == [] and list(elsewhere.iterdir()) == []


def test_an_ordinary_reader_keeps_its_existing_call_and_no_reseed(tmp_path, events):
    ship, pool, source = grok_ship(tmp_path)
    expected = Usage((), SECOND)
    reader = SimpleNamespace(usage=lambda read_pool: expected if read_pool is pool else None)
    assert quota.read_usage(usage_service(ship, []), pool, reader) is expected
    assert events == [] and not credential.usage_home(ship, pool).exists()


@pytest.fixture
def routed(monkeypatch):
    calls = []
    monkeypatch.setattr(quota, "read_usage", lambda service, pool, reader: calls.append((pool.name, reader.name)) or Usage((), SECOND))
    monkeypatch.setattr(quota, "record_read", lambda *args: None)
    return calls


def test_a_live_seat_read_goes_through_the_orchestrator(tmp_path, routed):
    ship, pool, source = grok_ship(tmp_path)
    service = SimpleNamespace(ship=ship, usage_hold={}, usage_read={},
                              ledger=SimpleNamespace(quota=SimpleNamespace(
                                  last_attempt=lambda name: None, due=lambda name, now, interval: True)))
    seat = SimpleNamespace(harness=GrokHarness(), pool=pool)
    quota.refresh(service, seat, True, SimpleNamespace(pool=pool.name))
    assert routed == [], "a live Grok seat uses the paced background reading"
    quota.refresh_declared(service)
    assert routed == [(pool.name, "grok")]


def test_a_destination_check_goes_through_the_orchestrator(tmp_path, monkeypatch, routed):
    ship, pool, source = grok_ship(tmp_path)
    monkeypatch.setattr(quota, "model_scope", lambda seat, name, **kwargs: None)
    service = SimpleNamespace(ship=ship, usage_hold={}, log=lambda *args, **kwargs: None,
                              ledger=SimpleNamespace(usage_known=lambda name, scope: False,
                                                     quota=SimpleNamespace(due=lambda name, now, interval: True)))
    assert Service.known_open(service, SimpleNamespace(name="worker"), pool.name) is False
    assert routed == [], "an unread destination stays unknown without a synchronous Grok call"
    quota.refresh_declared(service)
    assert routed == [(pool.name, "grok")]


# -- a normal seat launch reads Grok's version in a fresh home under its runtime ----------------------

def test_seat_launch_reads_the_selected_version_in_a_fresh_runtime_home(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    spec = SeatSpec("worker", "crew", "worker", "grok", ("synthetic-plan",), cwd=work, sandbox=False)
    ship, pool, source = grok_ship(tmp_path, {"worker": spec})
    directory = tmp_path / "bin"
    directory.mkdir()
    program = directory / "grok-8.6.4-linux-x86_64"
    program.write_text("Synthetic selected file; never executed.\n")
    program.chmod(0o700)
    (directory / "grok").symlink_to(program.name)
    monkeypatch.setattr(seat_module, "program_path", lambda: str(directory))
    seat = Seat(ship, spec, SimpleNamespace(pane=lambda ident: None))
    opened, executions = [], []

    def open_window(argv, env, path):
        opened.append((argv[0], path))
        return Pane("%7", "crew", "worker", 1007, 80, 24, False, False, None)

    def version(argv):
        assert argv[:2] == ("env", "-i"), "nothing inherited beyond what the command names"
        environment = dict(value.split("=", 1) for value in argv[2:-2])
        home = Path(environment["HOME"])
        executions.append({"home": home, "grok_home": environment["GROK_HOME"], "program": argv[-2],
                           "empty": home.is_dir() and not home.is_symlink() and list(home.iterdir()) == [],
                           "opened": len(opened), "saved": seat.launched()})
        return "8.6.4"

    monkeypatch.setattr(grok, "program_version", version)
    monkeypatch.setattr(seat, "_open_window", open_window)
    monkeypatch.setattr(seat, "_survives", lambda *args: None)
    seat.up(resume=False)

    run, = executions
    assert run["home"].parent == ship.runtime and run["grok_home"] == str(run["home"]) and run["empty"]
    assert seat.folder not in run["home"].parents and run["home"] != seat.home, "never the seat's sign-in home"
    assert run["program"] == str(program.resolve())
    assert opened == [(str(directory / "grok"), seat._path())], "the launch and the version use one selected program"
    assert seat._path().split(":") == [str(ship.runtime / "bin"), str(directory)]
    assert seat.launched().argv[0] == str(directory / "grok"), "launch.json records the program the seat ran"
    assert run["opened"] == 0 and run["saved"] is None, "read once for this launch, before its window opens and its identity is saved"
    assert not run["home"].exists() and not list(ship.runtime.glob("version-*")), "the fresh home is retired"
    launched = seat.launched()
    assert (launched.pane, launched.pid, launched.version, launched.harness) == ("%7", 1007, "8.6.4", "grok")
    copy = seat.home / "auth.json"
    assert not copy.is_symlink() and copy.read_bytes() == source.read_bytes(), "the seat holds a copy, never a link"
    assert json.loads(source.read_text())["synthetic-grant"]["refresh_token"] == "pool"


def test_doctor_names_the_selected_program_beside_its_pool(tmp_path, monkeypatch):
    from quarterdeck import doctor
    from quarterdeck.harness.base import program_where

    directory = tmp_path / "bin"
    directory.mkdir()
    program = directory / "grok-8.6.4-linux-x86_64"
    program.write_text("Synthetic selected file; never executed.\n")
    program.chmod(0o700)
    (directory / "grok").symlink_to(program.name)
    monkeypatch.setattr(seat_module, "program_path", lambda: str(directory))
    adapter = grok.GrokHarness()
    assert adapter.selected_program() == directory / "grok"
    assert program_where(adapter) == f"{directory / 'grok'} -> {program.resolve()}"
    shown = doctor.selected_program(SimpleNamespace(runtime=tmp_path), "grok", {"grok": "8.6.4"})
    assert shown == f" · runs {directory / 'grok'} -> {program.resolve()} · grok 8.6.4"
    assert doctor.selected_program(SimpleNamespace(runtime=tmp_path), "claude", {}) == "", "only adapters that settle one"
    monkeypatch.setattr(seat_module, "program_path", lambda: str(tmp_path / "empty"))
    assert adapter.selected_program() is None and program_where(adapter) is None
