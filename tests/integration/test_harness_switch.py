"""The fresh launch after a down-seat harness switch, using a private fake process."""

from quarterdeck.tmux import Tmux

from pathlib import Path

from dataclasses import replace
import json

import pytest

from quarterdeck import record_owner
from quarterdeck.harness.antigravity import AntigravityHarness
from quarterdeck.harness.codex import CodexHarness
from quarterdeck.harness.fake import FakeHarness
from quarterdeck.post import Store
from quarterdeck.seat import Seat
from quarterdeck.service import Service
from .waiting import wait_for
from ..unit.test_harness_switch import setup_seat, switch


@pytest.mark.parametrize("untyped", [False, True])
def test_fresh_codex_shape_process_joins_without_reusing_old_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tmux: Tmux, untyped) -> None:
    old_seat, old_record, _ = setup_seat(tmp_path, monkeypatch)
    selected = switch(old_seat)
    if untyped:
        (selected.trusted / record_owner.REGISTRY).unlink()
        journal = selected.trusted / "record-launches.jsonl"
        rows = [json.loads(line) for line in journal.read_text().splitlines()]
        for row in rows:
            row.pop("harness")
        journal.write_text("".join(json.dumps(row) + "\n" for row in rows))
        monkeypatch.setattr(AntigravityHarness, "read", lambda *a: pytest.fail("untyped history was guessed"))
    selected.ship.file.write_text("""[ship]
name = "example"
operator = "operator"
workspace = "."
[pools.codex]
provider = "codex"
[sessions.crew]
title = "crew"
[seats.worker]
session = "crew"
role = "worker"
harness = "codex"
pool = "codex"
""")
    seat = Seat(selected.ship, selected.spec, tmux)
    bad = seat.home / "sessions/2030/01/01/rollout-synthetic-older.jsonl"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"incomplete old record\n")
    record_owner.register(seat.trusted, bad, "codex")
    first_timestamp = CodexHarness.first_timestamp

    def new_candidate_only(path):
        assert path != bad, "a pre-launch record must not be probed to join a later launch"
        return first_timestamp(path)
    monkeypatch.setattr(CodexHarness, "first_timestamp", staticmethod(new_candidate_only))
    fake = FakeHarness()

    def launch(self, spec, pool, home, brief, resume, first):
        assert resume is None, "a changed harness must not resume the previous record"
        return fake.launch(replace(spec, settings={"as": "codex"}), pool, home, brief, resume, first)
    monkeypatch.setattr(CodexHarness, "launch", launch)
    monkeypatch.setattr(CodexHarness, "version", lambda self: "synthetic")
    Store(seat.ship.runtime).new("order", "desk", "worker", "synthetic launch task")
    assert tmux.socket, "every process in this test uses the private server"
    assert "fresh" in seat.up()  # even the normal resume request becomes fresh for another harness

    def finished():
        path = seat.record()
        return path if path and seat.harness.state(path).turns else None
    joined = wait_for(finished)
    assert joined != old_record.path and old_record.path.exists()
    assert seat.launched().harness == "codex" and str(bad) in seat.launched().records_before
    owners = {item.path: item.name for item in record_owner.records(seat)}
    assert owners[old_record.path] == (None if untyped else "antigravity")
    assert owners[joined] == "codex"
    svc = Service(seat.ship, tmux)
    svc.account()
    expected = {"codex"} if untyped else {"google", "codex"}
    assert {r[0] for r in svc.ledger.db.execute("SELECT pool FROM turns")} == expected
    if untyped:
        assert any(row[0] == str(old_record.path) and row[2] == "google" for row in svc.ledger.incomplete_records())
    assert any(row[0] == str(bad) for row in svc.ledger.incomplete_records())
    assert bad.exists(), "malformed retained history is reported rather than deleted"
    svc.ledger.close()
