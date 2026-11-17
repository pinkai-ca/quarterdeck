"""Synthetic retained-record ownership across a down seat's program change."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from quarterdeck import record_owner, service, watch
from quarterdeck.harness.synthetic_record import CodexShape
from tests.antigravity_shapes import AntigravityShape
from quarterdeck.harness.antigravity import AntigravityHarness
from quarterdeck.harness.base import Launch, RecordError
from quarterdeck.harness.codex import CodexHarness
from quarterdeck.ledger import Count, table
from quarterdeck.post import Store, Attempts, typed
from quarterdeck.seat import Launched, Seat
from quarterdeck.service import Service
from quarterdeck.ship import Pool, SeatSpec, Session, Ship
from quarterdeck.tmux import Pane
from .test_quota_walls import append_error


def setup_seat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pools = {"google": Pool("google", "antigravity"), "codex": Pool("codex", "codex")}
    specs = {"worker": SeatSpec("worker", "crew", "worker", "antigravity", ("google",), cwd=tmp_path),
             "neighbor": SeatSpec("neighbor", "crew", "worker", "codex", ("codex",), up=False)}
    ship = Ship(tmp_path, "example", "operator", tmp_path, pools, {"crew": Session("crew", "crew")}, specs)
    active = {"pane": None}
    tmux = SimpleNamespace(pane=lambda ident: active["pane"], kill_window=lambda ident, *, expected_pid=None: active.update(pane=None))
    seat = Seat(ship, specs["worker"], tmux)
    seat.home.mkdir(parents=True)
    seat.trusted.mkdir(parents=True, exist_ok=True)
    old = AntigravityShape(seat.home, tmp_path, "synthetic-old", "synthetic-model")
    old.begin("synthetic old input", "synthetic-model", "high")
    old.end("synthetic old reply")
    launch = Launched("%1", 12345, (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
                      "google", None, ["agy"], "synthetic", harness="antigravity")
    launch.write(seat.trusted / "launch.json")
    (seat.trusted / "record").write_text(str(old.path))
    monkeypatch.setattr(service.machine, "processes", lambda: {})
    return seat, old, active


@pytest.fixture
def local_midday():
    """Run at local noon, in a fixed zone, so turns stamped minutes ago share today's local date.

    tokens_today counts the turns of date.today(), and this test stamps its old turn a minute
    before now; in the first minute after local midnight that turn falls on the day before.
    """
    previous = os.environ.get("TZ")
    offset = int(time.time() % 86400) - 12 * 3600  # local = UTC - offset puts now at 12:00 local
    sign = "+" if offset >= 0 else "-"
    os.environ["TZ"] = f"QDNOON{sign}{abs(offset) // 3600:02d}:{abs(offset) % 3600 // 60:02d}:{abs(offset) % 60:02d}"
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


def switch(seat):
    seat.down()  # archives the reader and actual launch before removing launch.json
    spec = replace(seat.spec, harness="codex", pools=("codex",))
    seat.ship.seats[seat.name] = spec
    return Seat(seat.ship, spec, seat.tmux)


def test_down_switch_fresh_join_and_history_use_original_reader_pool_and_quota(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                              local_midday) -> None:
    seat, old, active = setup_seat(tmp_path, monkeypatch)
    assert datetime.now().hour == 12 and (datetime.now() - timedelta(minutes=5)).date() == datetime.now().date()
    at = datetime.now(timezone.utc) - timedelta(minutes=1)
    append_error(old, "Individual quota reached. Resets in 2h.\nError ID: synthetic-old-error", at)
    old_turns = seat.harness.turns(old.path)
    seat = switch(seat)
    snapshot = old.path.read_bytes()
    codex_read = CodexHarness.read
    seen = []

    def only_codex(path):
        assert path != old.path, "the selected text reader must never receive old SQLite"
        seen.append(path)
        return codex_read(path)
    monkeypatch.setattr(CodexHarness, "read", staticmethod(only_codex))
    assert seat.record() is None and "reader changed" in seat.join().reason
    assert watch.tokens_today(seat) == Count(old_turns[0].tokens.total), "the old record is still the seat\'s, read by its own reader"
    shown = "\n".join(watch.table(seat.ship, seat.tmux))
    assert "worker" in shown and "neighbor" in shown and str(old_turns[0].tokens.total) in shown, "the old record counts for its seat"
    assert "record absent: record reader changed" in "\n".join(watch.full(seat))
    svc = Service(seat.ship, seat.tmux)
    svc.account()
    original = svc.ledger.db.execute("SELECT record,pool,read_uncached FROM turns").fetchall()
    assert original == [(str(old.path), "google", old_turns[0].tokens.read_uncached)]
    assert svc.ledger.walled("google") and not svc.ledger.walled("codex")
    assert not seen and old.path.read_bytes() == snapshot
    journal = [json.loads(line) for line in (seat.trusted / "record-launches.jsonl").read_text().splitlines()]
    assert journal[0]["harness"] == "antigravity" and journal[0]["pool"] == "google"

    # Real fresh lifecycle; only opening the synthetic process and its version are substituted.
    created = []

    def open_window(*args):
        rec = CodexShape(seat.home, tmp_path, "synthetic-new", "synthetic-model")
        rec.begin("new synthetic input", "synthetic-model", "low")
        rec.end("new synthetic reply")
        created.append(rec)
        active["pane"] = Pane("%2", "crew", "worker", 12346, 80, 24, False, False, None)
        return active["pane"]
    monkeypatch.setattr(seat, "_open_window", open_window)
    monkeypatch.setattr(seat, "_survives", lambda *args: None)
    monkeypatch.setattr(CodexHarness, "version", lambda self: "synthetic")
    monkeypatch.setattr(CodexHarness, "launch", lambda *args: Launch(("sh", "-c", "exit 0"), {}))
    assert "fresh" in seat.fresh()
    assert seat.launched().harness == "codex" and seat.record() == created[0].path
    svc.account()
    expected = [(str(old.path), "google"), (str(created[0].path), "codex")]
    assert svc.ledger.db.execute("SELECT record,pool FROM turns ORDER BY started").fetchall() == expected
    svc.ledger.close()
    svc = Service(seat.ship, seat.tmux)
    svc.account()
    svc.account()
    assert svc.ledger.db.execute("SELECT record,pool FROM turns ORDER BY started").fetchall() == expected
    assert svc.ledger.walled("google") and not svc.ledger.walled("codex")
    assert old.path.read_bytes() == snapshot
    # Missing historical data is still represented, never filtered out of ownership.
    old.path.unlink()
    svc.account()
    assert any(row[0] == str(old.path) and row[2] == "google" for row in svc.ledger.incomplete_records())
    assert svc.ledger.db.execute("SELECT COUNT(*) FROM turns").fetchone() == (2,)
    assert watch.tokens_today(seat).unmeasured == 1, "the figure stands, and the missing record puts a + beside it"
    old.path.write_bytes(snapshot)
    svc.account()
    assert not svc.ledger.incomplete_records()
    assert svc.ledger.db.execute("SELECT COUNT(*) FROM turns").fetchone() == (2,)
    svc.ledger.close()


def test_saved_argv_proves_reader_but_untyped_down_history_does_not_guess(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seat, old, _ = setup_seat(tmp_path, monkeypatch)
    launch = json.loads((seat.trusted / "launch.json").read_text())
    launch.pop("harness")  # old launch.json still has exact argv evidence
    (seat.trusted / "launch.json").write_text(json.dumps(launch))
    spec = replace(seat.spec, harness="codex", pools=("codex",))
    changed = Seat(seat.ship, spec, seat.tmux)
    assert changed.record() is None
    assert record_owner.records(changed)[0].name == "antigravity"
    # A down seat: no saved program identity, only owned path and pool boundary.
    (seat.trusted / record_owner.REGISTRY).unlink()
    (seat.trusted / "launch.json").unlink()
    event = {"record": str(old.path), "at": launch["at"], "pool": "google"}
    (seat.trusted / "record-launches.jsonl").write_text(json.dumps(event) + "\n")
    monkeypatch.setattr(AntigravityHarness, "read", lambda *args: pytest.fail("unproven reader invoked"))
    monkeypatch.setattr(CodexHarness, "read", lambda *args: pytest.fail("foreign record decoded"))
    assert changed.record() is None and "reader unknown" in changed.join().reason
    assert changed.join().reason.endswith("inspect with qd watch worker")
    assert "qd fresh" not in changed.join().reason
    owned = record_owner.records(changed)
    assert len(owned) == 1 and owned[0].reader is None
    assert old.path.name in owned[0].reason
    assert "historical harness evidence required" in owned[0].reason
    assert owned[0].reason.endswith("inspect with qd watch worker")
    seat.ship.seats[seat.name] = spec
    svc = Service(seat.ship, seat.tmux)
    svc.account()
    assert svc.ledger.incomplete_records()[0][2] == "google"
    assert "reader unknown" in "\n".join(table(svc.ledger, "pool"))
    assert old.path.exists()
    svc.ledger.close()


def owned_codex_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data):
    seat, old, _ = setup_seat(tmp_path, monkeypatch)
    seat = switch(seat)
    path = seat.home / "sessions/2030/01/01/rollout-synthetic-bad.jsonl"
    path.parent.mkdir(parents=True)
    if data is not None:
        path.write_bytes(data)
    record_owner.register(seat.trusted, path, "codex")
    (seat.trusted / "record").write_text(str(path))
    return seat, old, path


@pytest.mark.parametrize("data", [b"SQLite format 3\x00\xff", b"broken\n", b"[]\n", b"{}\n",
                                  b'{"type":"user","message":{"content":"other program"}}\n', None])
def test_a_missing_or_foreign_owned_record_is_noted_and_marks_the_figures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data) -> None:
    seat, old, path = owned_codex_record(tmp_path, monkeypatch, data)
    if data is not None:
        with pytest.raises(OSError):
            CodexHarness().read(path)
    today = watch.tokens_today(seat)
    assert today.unmeasured == 1, "what the old record holds is counted; the unread one adds a +"
    assert "neighbor" in "\n".join(watch.table(seat.ship, seat.tmux))
    svc = Service(seat.ship, seat.tmux)
    svc.account()
    assert any(row[0] == str(path) for row in svc.ledger.incomplete_records())
    assert svc.ledger.db.execute("SELECT pool FROM turns").fetchall() == [("google",)]
    assert "+" in "\n".join(table(svc.ledger, "seat"))
    svc.ledger.close()


@pytest.mark.parametrize("data", [b'{"type":"event_msg","payload":[]}\n',
                                  b'{"type":"event_msg","payload":{"type":"task_started"},"timestamp":"bad"}\n'])
def test_an_odd_line_in_an_owned_record_is_skipped_not_a_blind_seat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data) -> None:
    seat, old, path = owned_codex_record(tmp_path, monkeypatch, data)
    state, turns = CodexHarness().read(path)
    assert turns == [] and state.turns == 0, "the line is skipped and the record reads as what it is: nothing yet"
    assert watch.tokens_today(seat).unmeasured == 0
    svc = Service(seat.ship, seat.tmux)
    svc.account()
    assert not any(row[0] == str(path) for row in svc.ledger.incomplete_records())
    svc.ledger.close()


def test_queued_history_uses_original_reader_and_conflicts_remain_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seat, old, _ = setup_seat(tmp_path, monkeypatch)
    store = Store(seat.ship.runtime)
    message = store.new("order", "desk", "worker", "synthetic queued task")
    attempts = Attempts(seat.ship.runtime)
    attempt = attempts.open(message, seat.name, "%1", 12345, seat.launched().at)
    attempts.close(attempt, "queued")
    old.begin(typed([message]), "synthetic-model", "high")
    old.end("synthetic reply")
    seat = switch(seat)
    assert seat._queued_unread(store, attempts) == []
    svc = Service(seat.ship, seat.tmux)
    svc.reconcile()
    assert attempts.latest()[message.id] == "delivered", "the common reconciliation path uses the retained reader"
    svc.ledger.close()
    again = attempts.open(message, seat.name, "%2", 12346, None)
    attempts.close(again, "queued")
    assert seat._queued_unread(store, attempts) == [message], "an old identical input cannot consume a new queue attempt"
    svc = Service(seat.ship, seat.tmux)
    svc.reconcile()
    svc.reconcile()
    assert attempts.latest()[message.id] == "queued", "a restart does not reset the original receipt time"
    svc.ledger.close()
    record_owner.register(seat.trusted, old.path, "codex")  # conflicting explicit history is not guessed
    assert record_owner.records(seat)[0].reader is None
    assert seat._queued_unread(store, attempts) == [message]
    assert old.path.exists()


def test_saved_launch_disagreement_cannot_add_a_wrong_pool_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seat, old, _ = setup_seat(tmp_path, monkeypatch)
    assert seat.record() == old.path
    previous = (seat.trusted / "record-launches.jsonl").read_bytes()
    changed = replace(seat.launched(), harness="codex", pool="codex", argv=["codex"])
    changed.write(seat.trusted / "launch.json")
    assert seat.record() is None and "disagree" in seat.join().reason
    svc = Service(seat.ship, seat.tmux)
    svc.account()
    assert svc.ledger.db.execute("SELECT pool FROM turns").fetchall() == [("google",)]
    assert (seat.trusted / "record-launches.jsonl").read_bytes() == previous
    svc.ledger.close()


def test_stale_sibling_pin_does_not_resolve_shared_record_ambiguity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seat, old, _ = setup_seat(tmp_path, monkeypatch)
    (seat.trusted / "record").unlink()
    neighbor = replace(seat.ship.seats["neighbor"], harness="antigravity", pools=("google",))
    seat.ship.seats["neighbor"] = neighbor
    sibling = Seat(seat.ship, neighbor, seat.tmux)
    sibling.home.mkdir(parents=True)
    sibling.trusted.mkdir(parents=True, exist_ok=True)
    stale = sibling.home / "synthetic-codex-record"
    stale.write_bytes(b"foreign synthetic bytes must never be opened")
    record_owner.register(sibling.trusted, stale, "codex")
    (sibling.trusted / "record").write_text(str(stale))
    launch = replace(seat.launched(), pane="%2", pid=45678)
    launch.write(sibling.trusted / "launch.json")
    seat.tmux.pane = lambda ident: Pane("%2", "crew", "neighbor", 45678, 80, 24, False, False, None)
    monkeypatch.setattr(AntigravityHarness, "_appeared", lambda *a: pytest.fail("ambiguous shared candidates enumerated"))
    monkeypatch.setattr(AntigravityHarness, "state", lambda *a: pytest.fail("foreign candidate decoded"))
    assert seat.join().record is None and seat.join().reason.startswith("ambiguous:")  # law: one-record


def test_new_program_without_a_joined_record_has_nothing_to_count_yet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seat, _, _ = setup_seat(tmp_path, monkeypatch)
    fresh = Seat(seat.ship, seat.ship.seats["neighbor"], seat.tmux)
    fresh.home.mkdir(parents=True)
    fresh.trusted.mkdir(parents=True, exist_ok=True)
    launch = replace(seat.launched(), harness="codex", pool="codex", argv=["codex"])
    launch.write(fresh.trusted / "launch.json")
    assert fresh.join().reason == "no record yet"
    assert watch.tokens_today(fresh) == Count(), "no record, nothing spent, nothing unmeasured"
    assert "record absent: no record yet" in "\n".join(watch.full(fresh))
