"""The Ledger's arithmetic and attribution, and the machine meter, on records the fake writes."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from quarterdeck import ledger as ledgermod, machine, post
from quarterdeck.fakerecord import ClaudeShape
from quarterdeck.harness import claude
from quarterdeck.harness.base import Usage, Window
from quarterdeck.ledger import Ledger, Machine, attribution
from quarterdeck.post import Store


def worked_record(tmp_path: Path, store: Store, inputs: list[str]) -> Path:
    record = ClaudeShape(tmp_path / "home", Path("/work"), "s1", "fake-1")
    for text in inputs:
        record.begin(text, "fake-1", "high")
        record.tool("Bash", "echo", "ok", False)
        record.tool("Read", "f", "x", True)
        record.end("done")
    return record.path


def test_ingestion_is_idempotent_and_sums_the_classes(tmp_path: Path):
    store = Store(tmp_path)
    record = worked_record(tmp_path, store, ["one", "two"])
    turns = claude.turns(record)
    book = Ledger(tmp_path / "ledger.sqlite")
    assert book.ingest("captain", "fake", record, turns, store, {1: Machine(2.5, 1000, 3, 4096)}) == 2
    assert book.ingest("captain", "fake", record, turns, store) == 0, "read twice, counted once"  # law: every-turn-accounted
    rows = book.rollup("seat")
    assert len(rows) == 1 and rows[0].key == "captain" and rows[0].turns == 2
    expected = sum((t.tokens for t in turns), start=turns[0].tokens.__class__())
    assert (rows[0].read_cached, rows[0].read_uncached, rows[0].cache_written, rows[0].written) == (
        expected.read_cached, expected.read_uncached, expected.cache_written, expected.written)
    assert rows[0].tool_calls == 4 and rows[0].tool_failures == 2 and rows[0].cpu_seconds == 2.5 and rows[0].write_bytes == 4096
    assert book.today("captain", datetime.now().astimezone().date().isoformat()) == expected.total
    assert [(name, count) for name, count, _, _ in book.tools()] == [("Bash", 2), ("Read", 2)]
    assert ledgermod.table(book, "seat")[1].startswith("captain")
    # a third turn later: only it is written
    ClaudeShape(tmp_path / "home", Path("/work"), "s1", "fake-1").begin("three", "fake-1", "high")
    record2 = ClaudeShape(tmp_path / "home", Path("/work"), "s1", "fake-1")
    record2.end("done")
    assert book.ingest("captain", "fake", record, claude.turns(record), store) == 1
    book.close()


def test_attribution_claimed_then_only_open_then_closing_turn(tmp_path: Path):
    store = Store(tmp_path)
    now = datetime.now(timezone.utc)
    turn = claude.Turn(0, now - timedelta(minutes=5), now - timedelta(minutes=4), "x", "y", "m", claude.Tokens())
    assert attribution(store, "scott", turn) == ledgermod.UNATTRIBUTED
    a = store.new("order", "desk", "scott", "a")
    turn = claude.Turn(0, now - timedelta(seconds=30), now + timedelta(seconds=1), "x", "y", "m", claude.Tokens())
    assert attribution(store, "scott", turn) == a.id, "the only open order"
    b = store.new("order", "desk", "scott", "b")
    turn = claude.Turn(1, now - timedelta(seconds=10), now + timedelta(seconds=1), "x", "y", "m", claude.Tokens())
    assert attribution(store, "scott", turn) == ledgermod.UNATTRIBUTED, "two open, none claimed"
    store.new("ack", "scott", "desk", "on b", re=b.id)
    turn = claude.Turn(2, now - timedelta(seconds=1), now + timedelta(seconds=2), "x", "y", "m", claude.Tokens())
    assert attribution(store, "scott", turn) == b.id, "the claim"
    report = store.new("report", "scott", "desk", "a done", re=a.id)
    turn = claude.Turn(3, report.at - timedelta(seconds=1), report.at + timedelta(seconds=1), "x", "y", "m", claude.Tokens())
    assert attribution(store, "scott", turn) == a.id, "the closing turn is charged to the order it closes"


def test_pool_windows_and_walls(tmp_path: Path):
    book = Ledger(tmp_path / "ledger.sqlite")
    later = datetime.now(timezone.utc) + timedelta(hours=2)
    book.record_usage("claude-primary", Usage((Window("5h", 1.0, later), Window("7d", 0.42, None)), datetime.now(timezone.utc)))
    book.record_usage("claude-secondary", Usage((Window("5h", 0.1, later),), datetime.now(timezone.utc)))
    assert book.walls() == ["claude-primary 5h"] and book.walled("claude-primary") and not book.walled("claude-secondary")
    earlier = datetime.now(timezone.utc) - timedelta(minutes=1)
    book.record_usage("claude-primary", Usage((Window("5h", 1.0, earlier),), datetime.now(timezone.utc)))
    assert book.walls() == [], "a spent window already reset is no wall"
    lines = ledgermod.pools_table(book)
    assert lines[1].startswith("claude-primary       5h") and "WALL" not in lines[1]
    book.close()


def test_a_key_pools_budget_is_a_wall_when_its_spend_is_stated(tmp_path: Path):
    from quarterdeck.ship import Pool, Ship

    pool = Pool("anthropic-api", "claude", api_key="env:ANTHROPIC_API_KEY", budget="200 USD/month")
    ship = Ship(tmp_path, "t", "operator", tmp_path, {pool.name: pool}, {}, {})
    book = Ledger(tmp_path / "ledger.sqlite")
    now = datetime.now(timezone.utc)
    turn = claude.Turn(0, now - timedelta(minutes=2), now - timedelta(minutes=1), "x", "y", "m", claude.Tokens(), cost=250.0)
    book.ingest("worker", "anthropic-api", tmp_path / "r.jsonl", [turn], Store(tmp_path))
    assert book.spend("anthropic-api", ledgermod.Budget.parse("200 USD/month").period_start(now.astimezone())) == (250.0, 0)
    assert book.walls(ship) == ["anthropic-api budget"] and book.walled("anthropic-api", ship)
    assert book.walls() == [], "no ship, no budgets to know"
    lines = ledgermod.pools_table(book, ship)
    assert lines[1].startswith("anthropic-api        spend    250.00 USD of 200 USD/month") and lines[1].endswith("WALL")
    unpriced = claude.Turn(1, now - timedelta(minutes=1), now, "x", "y", "m", claude.Tokens())
    book.ingest("worker", "anthropic-api", tmp_path / "r2.jsonl", [unpriced], Store(tmp_path))
    assert "1 turns with no price" in ledgermod.pools_table(book, ship)[1]
    book.close()
    import pytest

    from quarterdeck.ship import ShipError

    with pytest.raises(ShipError, match="200 USD/month"):
        ledgermod.Budget.parse("lots")
    assert ledgermod.Budget.parse(None) is None
    assert ledgermod.Budget.parse("50 USD/week").period_start(now.astimezone()).weekday() == 0


def test_turns_are_stored_in_local_time_so_today_is_the_local_day(tmp_path: Path):
    book = Ledger(tmp_path / "ledger.sqlite")
    ended = datetime.now(timezone.utc)
    turn = claude.Turn(0, ended - timedelta(seconds=5), ended, "x", "y", "m", claude.Tokens(1, 2, 3, 4))
    book.ingest("captain", "fake", tmp_path / "r.jsonl", [turn], Store(tmp_path))
    assert book.today("captain", ended.astimezone().date().isoformat()) == 10
    book.close()


def test_the_meter_charges_a_turn_with_its_tree(tmp_path: Path):
    meter = machine.Meter()
    table = machine.processes()
    me = os.getpid()
    assert me in table and table[me].cpu_seconds >= 0
    meter.sample(me, table)
    busy = sum(range(2_000_000))  # spend some CPU
    meter.sample(me, machine.processes())
    result = meter.result()
    assert result.max_processes >= 1 and result.peak_rss > 0 and result.cpu_seconds >= 0 and busy
    assert machine.tree(me)[0].pid == me
