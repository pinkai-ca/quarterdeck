"""Cost is a measured value with separate uncertainty, including historical zeros."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from pathlib import Path

import pytest

from quarterdeck import cli, ledger_reading, post
from quarterdeck.harness import pi, opencode
from quarterdeck.harness.types import Tokens, Turn
from quarterdeck.ledger import Ledger, SCHEMA
from tests.unit.test_opencode_record import database, session, user, message, MS

AT = datetime.now(timezone.utc)


@pytest.mark.parametrize("cost", [None, [], "bad", True, -1, float("nan"), float("inf"), 0, 2])
@pytest.mark.parametrize("reader", ["pi", "opencode"])
def test_record_cost_absence_invalid_zero_and_nonzero(tmp_path, cost, reader):
    if reader == "pi":
        path = tmp_path / "record.jsonl"
        usage = {"input": 1, "output": 2}
        if cost is not None:
            usage["cost"] = {"total": cost}
        path.write_text(json.dumps({"type": "message", "message": {"role": "assistant", "usage": usage,
                                                                     "content": [], "stopReason": "stop"}}))
        turn = pi.turns(path)[0]
    else:
        path = tmp_path / "record.db"
        with database(path) as con:
            session(con)
            user(con, "u", "root", 10, "synthetic")
            step = {"role": "assistant", "parentID": "u", "time": {"created": MS+20, "completed": MS+21},
                    "finish": "stop", "tokens": {"total": 3, "input": 1, "output": 2}}
            if cost is not None:
                step["cost"] = cost
            message(con, "a", "root", 20, step)
        turn = opencode.OpenCodeHarness().turns(path)[0]
    assert turn.tokens == Tokens(read_uncached=1, written=2)
    expected = cost if type(cost) is int and cost >= 0 else None
    assert turn.recorded_cost == expected
    assert turn.cost == (expected if expected else None), "zero needs stated price evidence"


@pytest.mark.parametrize("by", ["seat", "pool", "order"])
@pytest.mark.parametrize("costs,expected,unpriced", [([None], None, 1), ([0], 0, 0), ([1, None], 1, 1), ([0, None], 0, 1)])
def test_cost_rollups_and_json_keep_value_separate_from_uncertainty(tmp_path, capsys, monkeypatch, by, costs, expected, unpriced):
    monkeypatch.delenv("QD_SHIP", raising=False)
    monkeypatch.delenv("QD_SEAT", raising=False)
    (tmp_path / "ship.toml").write_text('[ship]\nname="synthetic"\n')
    with Ledger(tmp_path / "runtime" / "ledger.sqlite") as book:
        turns = [Turn(i, AT, AT, "in", "out", "m", Tokens(), cost=cost) for i, cost in enumerate(costs)]
        book.ingest("worker", "pool", tmp_path / "record", turns, post.Store(tmp_path / "runtime"))
        row = book.rollup(by)[0]
        assert row.cost == expected and row.unpriced == unpriced
        lines = ledger_reading.render(ledger_reading.collect(book, by=by))
        if expected is not None:
            assert f"{expected:.2f}" + ("+" if unpriced else "") in lines[1]
        else:
            assert "       - " in lines[1]
    assert cli.main(["--ship", str(tmp_path), "--json", "ledger", "--by", by]) == 0
    row = json.loads(capsys.readouterr().out)["rows"][0]
    assert row["cost"] == expected and row["unpriced"] == unpriced


def test_legacy_rows_migrate_without_rewriting_costs_and_refresh_with_evidence(tmp_path):
    path = tmp_path / "ledger.sqlite"
    with sqlite3.connect(path) as con:
        con.executescript(SCHEMA)
        # Original schema has no cost basis. Preserve both numeric values through migration.
        for index, cost in enumerate((0, 3)):
            con.execute('INSERT INTO turns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        ("record", index, "worker", "pool", "-", AT.isoformat(), AT.isoformat(), 0, "m",
                         0, 1, 0, 0, cost, 0, 0, 0, 2, 100, 1, 9, 0))
    with Ledger(path) as book:
        assert book.db.execute("SELECT cost,recorded_cost,cost_basis FROM turns ORDER BY turn").fetchall() == [(0, None, None), (3, None, None)]
        assert book.rollup("pool")[0].cost == 3 and book.rollup("pool")[0].unpriced == 1
        zero = Turn(0, AT, AT, "in", "out", "m", Tokens(read_uncached=1), recorded_cost=0)
        priced = replace(zero, index=1, recorded_cost=None)
        book.ingest("worker", "other-pool", Path("record"), [zero, priced], post.Store(tmp_path))
        rows = book.db.execute("SELECT pool,cost,recorded_cost,cost_basis,cpu_seconds FROM turns ORDER BY turn").fetchall()
        assert rows == [("pool", None, 0, None, 2), ("pool", 3, 3, "record", 2)]
        assert book.ingest("worker", "other-pool", Path("record"), [zero, priced], post.Store(tmp_path)) == 0
        # A subsequent explicit stated zero can improve the same row without moving its pool or machine data.
        book.ingest("worker", "other-pool", Path("record"),
                    [replace(zero, cost=0, cost_basis="stated launch rates"), priced], post.Store(tmp_path))
        assert book.rollup("pool")[0].cost == 3 and book.rollup("pool")[0].unpriced == 0
