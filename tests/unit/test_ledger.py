"""The Ledger's arithmetic and attribution, and the machine meter, on records the fake writes."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from quarterdeck import ledger as ledgermod, machine, post
from quarterdeck.harness.synthetic_record import ClaudeShape
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
    assert book.today("captain", datetime.now().astimezone().date().isoformat()) == ledgermod.Count(expected.total)
    assert [(name, count) for name, count, *_ in book.tools()] == [("Bash", 2), ("Read", 2)]
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
    assert lines[1].startswith("claude-primary 5h") and "WALL" not in lines[1]
    assert "LAST KNOWN" in lines[1], "invalid reset retains the prior dated tuple"
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
    assert book.today("captain", ended.astimezone().date().isoformat()) == ledgermod.Count(10)
    book.close()


def test_what_the_record_did_not_measure_is_counted_and_marked_with_a_plus(tmp_path: Path):
    from quarterdeck.harness.base import Tokens, ToolCall, Turn

    book = Ledger(tmp_path / "ledger.sqlite")
    store = Store(tmp_path)
    ended = datetime.now(timezone.utc)
    day = ended.astimezone().date().isoformat()
    assert book.today("scott", day) == ledgermod.Count(0, 0), "no turns on that day: 0 tokens, nothing unmeasured"

    # A turn measured in every class the provider reports, with one tool call the record did not time
    t0 = Turn(0, ended - timedelta(seconds=10), ended - timedelta(seconds=5), "in0", "out0", "gemini-4-argon-high",
              Tokens(read_cached=500, read_uncached=100, cache_written=0, written=40),
              tools=(ToolCall("run_command", 1.5, False), ToolCall("run_command", None, True)), subagent_turns=None)
    book.ingest("scott", "google", tmp_path / "agy.db", [t0], store)
    row = book.rollup("seat")[0]
    assert (row.read_cached, row.read_uncached, row.cache_written, row.written, row.unmeasured) == (500, 100, 0, 40, 0)
    assert book.today("scott", day) == ledgermod.Count(640, 0) and str(book.today("scott", day)) == "640"
    assert book.tools() == [("run_command", 2, 1.5, 1, 1, 0)], "the call without a duration is counted, and the seconds carry its mark"
    assert ledgermod.tools_table(book)[1].split() == ["run_command", "2", "1.5+", "1"]

    # A second turn whose usage the record did not hold at all: the sums stand, and every figure built on them says +
    t1 = Turn(1, ended - timedelta(seconds=4), ended, "in1", "out1", "gemini-4-argon-high", Tokens.unknown())
    book.ingest("scott", "google", tmp_path / "agy.db", [t0, t1], store)
    row = book.rollup("seat")[0]
    assert (row.turns, row.read_cached, row.written, row.unmeasured) == (2, 500, 40, 1), "the known part is kept, the gap counted"
    assert str(book.today("scott", day)) == "640+"
    rendered = ledgermod.table(book, "seat")
    assert "500+" in rendered[1] and "40+" in rendered[1] and ledgermod.PLUS_NOTE in rendered
    book.close()


def test_a_record_that_would_not_read_is_noted_and_marks_its_seats_figures(tmp_path: Path):
    book = Ledger(tmp_path / "ledger.sqlite")
    store = Store(tmp_path)
    bad_rec = tmp_path / "corrupt.db"
    day = datetime.now().astimezone().date().isoformat()

    # 1. An unreadable record with no turn read yet: its seat shows 0+ and the note names the file
    book.mark_record(bad_rec, "scott", "google", "corrupt sqlite header")
    book.close()
    book = Ledger(tmp_path / "ledger.sqlite")
    assert book.incomplete_records() == [(str(bad_rec), "scott", "google", "corrupt sqlite header")]
    seat_rows = book.rollup("seat")
    assert len(seat_rows) == 1 and seat_rows[0].key == "scott" and seat_rows[0].turns == 0 and seat_rows[0].unmeasured == 1
    assert str(book.today("scott", day)) == "0+"
    seat_table = "\n".join(ledgermod.table(book, "seat"))
    assert "0+" in seat_table and "unread record: corrupt.db (scott, google) · corrupt sqlite header" in seat_table
    assert "unread record: corrupt.db (scott, google) · corrupt sqlite header" in "\n".join(ledgermod.pools_table(book))

    # 2. Once the record reads, the note goes and the figures stand plain
    now = datetime.now(timezone.utc)
    t0 = claude.Turn(0, now - timedelta(seconds=5), now, "in", "out", "m", claude.Tokens(1, 2, 3, 4))
    book.ingest("scott", "google", bad_rec, [t0], store)
    book.mark_record(bad_rec, "scott", "google", None)
    assert book.incomplete_records() == []
    assert book.rollup("seat")[0].turns == 1 and book.rollup("seat")[0].unmeasured == 0
    assert book.today("scott", day) == ledgermod.Count(10) and ledgermod.PLUS_NOTE not in ledgermod.table(book, "seat")
    book.close()


def test_the_meter_charges_a_turn_with_its_tree(tmp_path: Path):
    me = os.getpid()
    meter = machine.Meter(me)
    table = machine.processes()
    assert me in table and table[me].cpu_seconds >= 0
    meter.sample(table)
    busy = sum(range(2_000_000))  # spend some CPU
    meter.sample()
    result = meter.result()
    assert result.max_processes >= 1 and result.peak_rss > 0 and result.cpu_seconds >= 0 and busy
    assert machine.tree(me)[0].pid == me


def test_late_metadata_refreshes_tools_and_times_without_moving_pool_or_machine(tmp_path):
    from dataclasses import replace
    from quarterdeck.harness.base import ToolCall, Tokens, Turn
    from quarterdeck.ledger import Machine

    record = tmp_path / 'synthetic.db'
    book, store = Ledger(tmp_path / 'ledger.sqlite'), Store(tmp_path)
    stamp = datetime.now(timezone.utc)
    initial = Turn(8, stamp, None, 'question', 'answer', None, Tokens.unknown(),
                   (ToolCall('run_command',None,False),))
    book.provenance.remember(record, 'worker', 'pool-a', stamp - timedelta(seconds=10))
    sample = Machine(3.5,4096,2,800)
    assert book.ingest('worker','pool-a',record,[initial],store,{8:sample}) == 1
    duration = replace(initial,tools=(ToolCall('run_command',1.5,False),))
    assert book.ingest('worker','pool-b',record,[duration],store,{8:Machine(99)}) == 1
    assert book.tools() == [('run_command',1,1.5,0,0,0)]
    renamed = replace(duration,tools=(ToolCall('shell',1.5,False),))
    assert book.ingest('worker','pool-b',record,[renamed],store) == 1
    assert book.tools() == [('shell',1,1.5,0,0,0)]
    final = replace(renamed,ended=stamp+timedelta(seconds=4),tokens=Tokens(1,2,3,4),model='model')
    assert book.ingest('worker','pool-b',record,[final],store) == 1
    assert book.ingest('worker','pool-b',record,[final],store) == 0
    row = book.db.execute('SELECT pool,seconds,read_uncached,cpu_seconds,peak_rss,max_processes,write_bytes FROM turns').fetchone()
    assert row == ('pool-a',4,2,3.5,4096,2,800)
    book.provenance.remember(record, 'worker', 'pool-b', stamp + timedelta(seconds=10))
    book.mark_record(record,'worker','pool-b','temporary failure')
    assert {r[2] for r in book.incomplete_records()} == {'pool-a', 'pool-b'}
    book.close()
    book = Ledger(tmp_path / 'ledger.sqlite')
    # A delayed earlier turn uses the historical launch after service restart.
    earlier = replace(final,index=0)
    book.ingest('worker','pool-b',record,[earlier,final],store)
    assert book.db.execute('SELECT DISTINCT pool FROM turns').fetchall() == [('pool-a',)]
    book.close()


def test_an_unjudged_tool_call_and_an_unread_record_are_marked_not_hidden(tmp_path):
    from quarterdeck.harness.base import ToolCall, Tokens, Turn

    book, store = Ledger(tmp_path / 'ledger.sqlite'), Store(tmp_path)
    order = store.new('order','captain','worker','work')
    stamp = datetime.now(timezone.utc)
    t = Turn(0,stamp,stamp,'question','answer','m',Tokens(1,2,3,4),(ToolCall('tool',None,None),))
    record = tmp_path / 'synthetic.db'
    book.ingest('worker','pool-a',record,[t],store)
    book.attributions.replay(store,tmp_path)
    assert book.tools() == [('tool',1,0.0,0,1,1)], "a call with no duration and no verdict is counted, and both figures carry its mark"
    assert book.rollup('order')[0].key == order.id and book.rollup('order')[0].unjudged == 1
    book.mark_record(record,'worker','pool-a','unknown earlier history')
    assert all(r.unmeasured for r in book.rollup('order'))
    assert all('+' in '\n'.join(ledgermod.table(book,by)) and 'unread record' in '\n'.join(ledgermod.table(book,by)) for by in ('seat','pool','order'))
    book.mark_record(record,'worker','pool-a',None)
    assert book.rollup('order')[0].turns == 1 and book.rollup('order')[0].unmeasured == 0
    book.close()
