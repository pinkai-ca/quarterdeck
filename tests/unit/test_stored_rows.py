"""Named source rows retain provenance, partial cost evidence and machine data on refresh."""

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from quarterdeck import ledger
from quarterdeck.harness.types import CostStep, Tokens, ToolCall, Turn
from quarterdeck.post import Store


@pytest.mark.parametrize("reordered_projection", [False, True])
def test_late_refresh_retains_named_fields_and_public_tuples(tmp_path, monkeypatch, reordered_projection):
    columns, tool_columns = ledger.TURN_COLUMNS, ledger.TOOL_COLUMNS
    if reordered_projection:
        monkeypatch.setattr(ledger, "TURN_COLUMNS", tuple(reversed(columns)))
        monkeypatch.setattr(ledger, "TOOL_COLUMNS", tuple(reversed(tool_columns)))
    store = Store(tmp_path)
    order = store.new("order", "desk", "worker", "synthetic task")
    at = datetime.now(timezone.utc)
    record = tmp_path / "synthetic-record"
    steps = (CostStep("unpriced", Tokens(read_uncached=11), 0), CostStep("priced", Tokens(written=7), 2))
    initial = Turn(8, at, at+timedelta(seconds=3), "in", "out", "priced", Tokens(1, 11, 3, 7),
                   (ToolCall("child", None, None),), cost=2, recorded_cost=2, cost_basis="record",
                   cost_steps=steps, unpriced_steps=1)
    path = tmp_path / "ledger.sqlite"
    with ledger.Ledger(path) as book:
        book.provenance.remember(record, "worker", "pool-a", at-timedelta(seconds=1))
        assert book.ingest("worker", "pool-a", record, [initial], store,
                           {8: ledger.Machine(1.25, 8192, 3, 512)}) == 1
        assert tuple(r[1] for r in book.db.execute("PRAGMA table_info(turns)")) == columns
        assert tuple(r[1] for r in book.db.execute("PRAGMA table_info(tool_calls)")) == tool_columns
    refreshed = replace(initial, ended=at+timedelta(seconds=5), tools=(ToolCall("child", 1.5, False),
                        ToolCall("shell", 2, True)), subagent_turns=2)
    with ledger.Ledger(path) as book:
        book.provenance.remember(record, "worker", "pool-b", at+timedelta(seconds=10))
        assert book.ingest("foreign", "pool-b", record, [refreshed], store, {8: ledger.Machine(99)}) == 1
        assert book.ingest("foreign", "pool-b", record, [refreshed], store) == 0
        assert book.db.row_factory is None
        assert book.db.execute("SELECT seat,pool,order_id,seconds,cpu_seconds,peak_rss,max_processes,write_bytes,"
                               "subagent_turns,cost FROM turns").fetchone() == (
                                   "worker", "pool-a", order.id, 5, 1.25, 8192, 3, 512, 2, 2)
        assert book.tools() == [("child", 1, 1.5, 0, 0, 0), ("shell", 1, 2, 1, 0, 0)]
        evidence = book.costs.get(record, 8)
        detail = book.costs.details(record, 8)
        assert (evidence.recorded_cost, evidence.cost_basis, evidence.valid) == (2, "record", 1)
        assert detail.unpriced_steps == 1 and json.loads(detail.steps) == [asdict(step) for step in steps]
        for by in ("seat", "pool", "order"):
            row = book.rollup(by)[0]
            assert (row.cost, row.unpriced, row.cpu_seconds, row.write_bytes) == (2, 1, 1.25, 512)
        # An older writer's refresh invalidates certification without erasing the raw evidence.
        book.db.execute("UPDATE turns SET cost=0 WHERE record=? AND turn=8", (str(record),))
        book.db.commit()
        assert book.costs.get(record, 8) == (2, "record", 0)
        assert book.costs.details(record, 8) == detail
        assert book.rollup("pool")[0].cost is None
        assert book.ingest("foreign", "pool-b", record, [refreshed], store) == 1
        assert book.ingest("foreign", "pool-b", record, [refreshed], store) == 0
        # A sparse earlier turn still resolves from the original launch after reopening.
        assert book.ingest("worker", "pool-b", record, [replace(refreshed, index=0), refreshed], store) == 1
        assert book.db.execute("SELECT turn,pool FROM turns ORDER BY turn").fetchall() == [(0, "pool-a"), (8, "pool-a")]
