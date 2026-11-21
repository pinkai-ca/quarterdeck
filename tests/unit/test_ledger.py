"""The Ledger's arithmetic and attribution, and the machine meter, on records the fake writes."""

from __future__ import annotations

import os
import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from quarterdeck import ledger as ledgermod, machine, post
from quarterdeck.harness.synthetic_record import ClaudeShape
from quarterdeck.harness import claude, codex
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


def test_codex_accounting_partitions_subscription_tokens_and_prices_key_moves(tmp_path):
    from dataclasses import replace
    import json
    from types import SimpleNamespace

    from quarterdeck.harness.key_prices import priced, snapshot
    from quarterdeck.harness.types import Tokens
    from quarterdeck.seat import Seat
    from quarterdeck.service import Service
    from quarterdeck.ship import Pool, SeatSpec, Session, Ship

    rates = dict(currency="USD", unit="million_tokens", input=7, output=11, cache_read=2, cache_write=13)
    pools = {name: Pool(name, "openai", api_key=f"keys:{name}", price=rates) for name in ("key-a", "key-b")}
    pools["subscription"] = Pool("subscription", "openai")
    spec = SeatSpec("worker", "crew", "worker", "codex", tuple(pools), cwd=tmp_path)
    ship = Ship(tmp_path, "synthetic", "operator", tmp_path, pools, {"crew": Session("crew", "crew")},
                {spec.name: spec})
    service = Service(ship, SimpleNamespace())
    seat = Seat(ship, spec, service.tmux)
    record = seat.home / "sessions/2026/11/21/rollout-synthetic.jsonl"
    record.parent.mkdir(parents=True)
    seat.trusted.mkdir(parents=True, exist_ok=True)
    cases = [
        ("key-a", [(14029, 0, 14026, 5, 0, 14034)]),
        ("key-b", [(14004, 0, 14001, 65, 0, 14069), (14092, 14001, 88, 5, 0, 14097)]),
        ("subscription", [(420394, 419712, 0, 439, 136, 420833)]),
        ("key-b", [(0, 0, 0, 0, 0, 71452)]),
    ]
    fields = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
              "output_tokens", "reasoning_output_tokens", "total_tokens")
    origin = datetime(2026, 11, 21, 12, tzinfo=timezone.utc)
    rows, launches = [], []
    for i, (pool, usages) in enumerate(cases):
        launched = origin + timedelta(minutes=i)
        launches.append(dict(record=str(record), at=launched.isoformat(), pool=pool,
                             harness="codex", price=snapshot("codex", pools[pool])))
        started = launched + timedelta(seconds=1)
        events = [
            ("turn_context", {"model": "synthetic"}),
            ("event_msg", {"type": "task_started"}),
            *(("event_msg", {"type": "token_count", "info": {"last_token_usage": dict(zip(fields, usage))}})
              for usage in usages),
            ("event_msg", {"type": "task_complete", "last_agent_message": "done"}),
        ]
        rows.extend(dict(timestamp=(started + timedelta(seconds=j)).isoformat(), type=kind, payload=payload)
                    for j, (kind, payload) in enumerate(events))
    record.write_text("".join(json.dumps(row) + "\n" for row in rows))
    (seat.trusted / "record-launches.jsonl").write_text("".join(json.dumps(row) + "\n" for row in launches))

    expected = [Tokens(0, 3, 14026, 5), Tokens(14001, 6, 14089, 70), Tokens(419712, 682, 0, 439), Tokens.unknown()]
    turns = codex.turns(record)
    measured = [turn.tokens for turn in turns]
    assert measured == expected
    assert [tokens.total for tokens in measured[:3]] == [14034, 28166, 420833]
    with service.ledger as book:
        for turn, launch in zip(turns, launches):
            tokens = turn.tokens
            old = replace(tokens, read_uncached=tokens.read_uncached + tokens.cache_written, unmeasured=0)
            book.ingest(seat.name, launch["pool"], record,
                        [priced(replace(turn, tokens=old), launch["price"])], service.store)
        service.account_records(process_table={})
        rollups = {row.key: row for row in book.rollup("pool")}
        for row, tokens in zip((rollups["key-a"], rollups["key-b"], rollups["subscription"]), expected):
            assert (row.read_cached, row.read_uncached, row.cache_written, row.written) == (
                tokens.read_cached, tokens.read_uncached, tokens.cache_written, tokens.written)
        assert book.spend("key-a", origin) == (pytest.approx((3 * 7 + 14026 * 13 + 5 * 11) / 1e6), 0)
        assert book.spend("key-b", origin) == (pytest.approx((6 * 7 + 14001 * 2 + 14089 * 13 + 70 * 11) / 1e6), 1)
        assert rollups["key-b"].unmeasured == rollups["key-b"].unpriced == 1
        assert rollups["subscription"].cost is None and rollups["subscription"].unmeasured == 0


def test_attribution_claimed_then_only_open_then_closing_turn(tmp_path: Path):
    store = Store(tmp_path)
    now = datetime.now(timezone.utc)
    turn = claude.Turn(0, now - timedelta(minutes=5), now - timedelta(minutes=4), "x", "y", "m", claude.Tokens())
    assert attribution(store, "maker", turn) == ledgermod.UNATTRIBUTED
    a = store.new("order", "desk", "maker", "a")
    turn = claude.Turn(0, now - timedelta(seconds=30), now + timedelta(seconds=1), "x", "y", "m", claude.Tokens())
    assert attribution(store, "maker", turn) == a.id, "the only open order"
    b = store.new("order", "desk", "maker", "b")
    turn = claude.Turn(1, now - timedelta(seconds=10), now + timedelta(seconds=1), "x", "y", "m", claude.Tokens())
    assert attribution(store, "maker", turn) == ledgermod.UNATTRIBUTED, "two open, none claimed"
    store.new("ack", "maker", "desk", "on b", re=b.id)
    turn = claude.Turn(2, now - timedelta(seconds=1), now + timedelta(seconds=2), "x", "y", "m", claude.Tokens())
    assert attribution(store, "maker", turn) == b.id, "the claim"
    report = store.new("report", "maker", "desk", "a done", re=a.id)
    turn = claude.Turn(3, report.at - timedelta(seconds=1), report.at + timedelta(seconds=1), "x", "y", "m", claude.Tokens())
    assert attribution(store, "maker", turn) == a.id, "the closing turn is charged to the order it closes"


def test_pool_windows_and_walls(tmp_path: Path):
    book = Ledger(tmp_path / "ledger.sqlite")
    later = datetime.now(timezone.utc) + timedelta(hours=2)
    book.record_usage("claude-primary", Usage((Window("5h", 1.0, later), Window("7d", 0.42, None)), datetime.now(timezone.utc)))
    book.record_usage("claude-secondary", Usage((Window("5h", 0.1, later),), datetime.now(timezone.utc)))
    assert book.walls() == ["claude-primary 5h"] and book.walled("claude-primary") and not book.walled("claude-secondary")
    earlier = datetime.now(timezone.utc) - timedelta(minutes=1)
    book.record_usage("claude-primary", Usage((Window("5h", 1.0, earlier),), datetime.now(timezone.utc)))
    # A reading rejected as invalid (its reset is not after its own read) is not a newer good
    # reading, so it neither clears nor moves the wall; the next valid reading with room clears it.
    assert book.walls() == ["claude-primary 5h · last known"], "a rejected read keeps the retained wall"
    lines = ledgermod.pools_table(book)
    assert lines[1].startswith("claude-primary 5h") and lines[1].endswith("WALL")
    assert "LAST KNOWN" in lines[1], "invalid reset retains the prior dated tuple"
    book.record_usage("claude-primary", Usage((Window("5h", 0.2, later),), datetime.now(timezone.utc)))
    assert book.walls() == [], "the next valid reading with room clears it"
    book.close()


def test_a_key_pools_budget_is_a_wall_when_its_spend_is_stated(tmp_path: Path):
    from quarterdeck.ship import Pool, Ship

    pool = Pool("anthropic-api", "claude", api_key="keys:anthropic-api-key", budget="200 USD/month")
    ship = Ship(tmp_path, "t", "operator", tmp_path, {pool.name: pool}, {}, {})
    book = Ledger(tmp_path / "ledger.sqlite")
    now = datetime.now(timezone.utc)
    turn = claude.Turn(0, now - timedelta(minutes=2), now - timedelta(minutes=1), "x", "y", "m", claude.Tokens(), cost=250.0)
    book.ingest("worker", "anthropic-api", tmp_path / "r.jsonl", [turn], Store(tmp_path))
    assert book.spend("anthropic-api", ledgermod.Budget.parse("200 USD/month").period_start(now.astimezone())) == (250.0, 0)
    assert book.walls(ship) == ["anthropic-api budget"] and book.walled("anthropic-api", ship)
    assert book.walls() == [], "no ship, no budgets to know"
    lines = ledgermod.pools_table(book, ship)
    assert any(line.startswith("anthropic-api        spend    250.00 USD of 200 USD/month") and line.endswith("WALL") for line in lines)
    unpriced = claude.Turn(1, now - timedelta(minutes=1), now, "x", "y", "m", claude.Tokens())
    book.ingest("worker", "anthropic-api", tmp_path / "r2.jsonl", [unpriced], Store(tmp_path))
    assert "1 turns with no price" in "\n".join(ledgermod.pools_table(book, ship))
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
    assert book.today("maker", day) == ledgermod.Count(0, 0), "no turns on that day: 0 tokens, nothing unmeasured"

    # A turn measured in every class the provider reports, with one tool call the record did not time
    t0 = Turn(0, ended - timedelta(seconds=10), ended - timedelta(seconds=5), "in0", "out0", "gemini-4-argon-high",
              Tokens(read_cached=500, read_uncached=100, cache_written=0, written=40),
              tools=(ToolCall("run_command", 1.5, False), ToolCall("run_command", None, True)), subagent_turns=None)
    book.ingest("maker", "google", tmp_path / "agy.db", [t0], store)
    row = book.rollup("seat")[0]
    assert (row.read_cached, row.read_uncached, row.cache_written, row.written, row.unmeasured) == (500, 100, 0, 40, 0)
    assert book.today("maker", day) == ledgermod.Count(640, 0) and str(book.today("maker", day)) == "640"
    assert book.tools() == [("run_command", 2, 1.5, 1, 1, 0)], "the call without a duration is counted, and the seconds carry its mark"
    assert ledgermod.tools_table(book)[1].split() == ["run_command", "2", "1.5+", "1"]

    # A second turn whose usage the record did not hold at all: the sums stand, and every figure built on them says +
    t1 = Turn(1, ended - timedelta(seconds=4), ended, "in1", "out1", "gemini-4-argon-high", Tokens.unknown())
    book.ingest("maker", "google", tmp_path / "agy.db", [t0, t1], store)
    row = book.rollup("seat")[0]
    assert (row.turns, row.read_cached, row.written, row.unmeasured) == (2, 500, 40, 1), "the known part is kept, the gap counted"
    assert str(book.today("maker", day)) == "640+"
    rendered = ledgermod.table(book, "seat")
    assert "500+" in rendered[1] and "40+" in rendered[1] and ledgermod.PLUS_NOTE in rendered
    book.close()


def test_a_record_that_would_not_read_is_noted_and_marks_its_seats_figures(tmp_path: Path):
    book = Ledger(tmp_path / "ledger.sqlite")
    store = Store(tmp_path)
    bad_rec = tmp_path / "corrupt.db"
    day = datetime.now().astimezone().date().isoformat()

    # 1. An unreadable record with no turn read yet: its seat shows 0+ and the note names the file
    book.mark_record(bad_rec, "maker", "google", "corrupt sqlite header")
    book.close()
    book = Ledger(tmp_path / "ledger.sqlite")
    assert book.incomplete_records() == [(str(bad_rec), "maker", "google", "corrupt sqlite header")]
    seat_rows = book.rollup("seat")
    assert len(seat_rows) == 1 and seat_rows[0].key == "maker" and seat_rows[0].turns == 0 and seat_rows[0].unmeasured == 1
    assert str(book.today("maker", day)) == "0+"
    seat_table = "\n".join(ledgermod.table(book, "seat"))
    assert "0+" in seat_table and "unread record: corrupt.db (maker, google) · corrupt sqlite header" in seat_table
    assert "unread record: corrupt.db (maker, google) · corrupt sqlite header" in "\n".join(ledgermod.pools_table(book))

    # 2. Once the record reads, the note goes and the figures stand plain
    now = datetime.now(timezone.utc)
    t0 = claude.Turn(0, now - timedelta(seconds=5), now, "in", "out", "m", claude.Tokens(1, 2, 3, 4))
    book.ingest("maker", "google", bad_rec, [t0], store)
    book.mark_record(bad_rec, "maker", "google", None)
    assert book.incomplete_records() == []
    assert book.rollup("seat")[0].turns == 1 and book.rollup("seat")[0].unmeasured == 0
    assert book.today("maker", day) == ledgermod.Count(10) and ledgermod.PLUS_NOTE not in ledgermod.table(book, "seat")
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


def accounting_example(tmp_path, monkeypatch, count):
    """Real accounting and SQLite, with source values supplied without a program."""
    import json
    from types import SimpleNamespace
    from quarterdeck import record_owner, service
    from quarterdeck.harness.base import State, Tokens, ToolCall, Turn
    from quarterdeck.harness.fake import FakeHarness
    from quarterdeck.harness.limits import Wall
    from quarterdeck.service import Service
    from quarterdeck.ship import Ship

    ship = Ship(tmp_path, 'synthetic', 'operator', tmp_path, {}, {}, {})
    svc = Service(ship, SimpleNamespace())
    folder = ship.runtime / 'seats' / 'worker-z'
    home = folder / 'home'
    home.mkdir(parents=True)
    trusted = ship.runtime / 'trusted' / 'worker-z'
    trusted.mkdir(parents=True)
    record = home / 'record-z'
    record.write_text('synthetic first revision')
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    reader = FakeHarness()
    seat = SimpleNamespace(name='worker-z', home=home, folder=folder, trusted=trusted, harness=reader,
                           session=SimpleNamespace(human=False), pane=lambda: None,
                           launched=lambda: None, record=lambda: record)
    record_owner.register(trusted, record, 'fake')
    launches = [dict(record=str(record), at=(at-timedelta(seconds=i+1)).isoformat(),
                     pool='pool-z', harness='fake') for i in range(count)]
    (trusted / 'record-launches.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in launches))
    turns = [Turn(i, at+timedelta(seconds=i*2), at+timedelta(seconds=i*2+1),
                  'synthetic input', 'synthetic reply', 'model-z', Tokens(1,2,3,4),
                  (ToolCall('synthetic-tool', 0.5, False),), cost=0.25) for i in range(count)]
    walls = tuple(Wall(f'synthetic-wall-z{i}', at, at+timedelta(hours=1), signature=f'synthetic-z{i}')
                  for i in range(count))
    state = State(turns=count, turn_open=False, walls=walls)
    reads = []

    def read(path):
        from dataclasses import replace
        assert not svc.ledger.db.in_transaction, 'program record reads precede the write transaction'
        reads.append(path)
        return replace(state, turns=len(turns)), turns

    monkeypatch.setattr(reader, 'read', read)
    monkeypatch.setattr(FakeHarness, 'attribution_inputs', lambda self, path: [])
    monkeypatch.setattr(service.machine, 'processes', lambda: {})
    monkeypatch.setattr(svc, 'resolved_seats', lambda: [seat])
    monkeypatch.setattr(record_owner, 'records', lambda owner: [record_owner.Owned(record, 'fake', reader)])
    return svc, seat, record, turns, reads, at


def sql_writes(trace):
    return [sql for sql in trace if sql.lstrip().upper().startswith(
        ('BEGIN', 'COMMIT', 'INSERT', 'REPLACE', 'UPDATE', 'DELETE'))]


@pytest.mark.parametrize('count', [1, 48])
def test_account_pass_commits_once_for_history_and_writes_nothing_when_unchanged(tmp_path, monkeypatch, count):
    svc, seat, record, turns, reads, at = accounting_example(tmp_path, monkeypatch, count)
    trace = []
    svc.ledger.db.set_trace_callback(trace.append)
    svc.account()
    assert trace.count('COMMIT') == 1
    assert svc.ledger.db.execute('SELECT COUNT(*) FROM record_launches').fetchone() == (count,)
    assert svc.ledger.db.execute('SELECT COUNT(*) FROM wall_events').fetchone() == (count,)
    assert svc.ledger.db.execute('SELECT COUNT(*),SUM(written),SUM(cost) FROM turns').fetchone() == (count, 4*count, 0.25*count)
    assert svc.ledger.tools() == [('synthetic-tool', count, count*0.5, 0, 0, 0)]
    assert svc.ledger.rollup('pool')[0].key == 'pool-z'
    before = svc.ledger.db.total_changes
    for _ in range(3):
        trace.clear()
        svc.account()
        assert sql_writes(trace) == []
        assert svc.ledger.db.total_changes == before
    assert reads == [record]
    # A growing record with the same completed turns must not rewrite old walls or turns.
    old_checkpoint = svc.ledger.db.execute('SELECT record,evidence FROM record_accounting').fetchall()
    record.write_text('synthetic second revision; only metadata changed')
    trace.clear()
    svc.account()
    changes = sql_writes(trace)
    assert len(changes) == 4 and changes[0] == 'BEGIN ' and changes[-1] == 'COMMIT'
    assert changes[1].startswith('INSERT OR REPLACE INTO record_accounting VALUES ')
    assert changes[2].startswith('INSERT OR REPLACE INTO attribution_records VALUES ')
    assert svc.ledger.db.total_changes == before + 2
    checkpoint = svc.ledger.db.execute('SELECT record,evidence FROM record_accounting').fetchall()
    assert len(checkpoint) == len(old_checkpoint) == 1
    assert checkpoint[0][0] == old_checkpoint[0][0] == str(record)
    assert checkpoint[0][1] != old_checkpoint[0][1]
    assert reads == [record, record]
    trace.clear()
    svc.account()
    assert sql_writes(trace) == [] and svc.ledger.db.total_changes == before + 2
    assert reads == [record, record]
    svc.ledger.close()


def test_standalone_writers_skip_equal_rows_and_keep_committed_results_readable(tmp_path):
    import sqlite3
    from quarterdeck.harness.limits import Wall

    path, record = tmp_path/'ledger.sqlite', tmp_path/'record-z'
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with Ledger(path) as book:
        assert book.db.execute('PRAGMA synchronous').fetchone() == (0,)
        assert book.db.execute('PRAGMA journal_mode').fetchone() == ('delete',)
        wall = Wall('synthetic-wall-z', at, at+timedelta(hours=1), signature='synthetic-z')
        assert book.provenance.remember(record, 'worker-z', 'pool-z', at)
        book.quota.observe(str(record), 'worker-z', 'pool-z', wall, at)
        book.mark_record(record, 'worker-z', 'pool-z', 'synthetic unavailable measurement')
        trace, before = [], book.db.total_changes
        book.db.set_trace_callback(trace.append)
        assert not book.provenance.remember(record, 'worker-z', 'pool-z', at)
        book.quota.observe(str(record), 'worker-z', 'pool-z', wall, at+timedelta(minutes=1))
        book.mark_record(record, 'worker-z', 'pool-z', 'synthetic unavailable measurement')
        book.mark_record(tmp_path/'other-record-z', 'worker-z', 'pool-z', None)
        assert sql_writes(trace) == [] and book.db.total_changes == before
        # A legacy-layout SQL reader can consume the committed existing tables.
        with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as old_reader:
            assert old_reader.execute('SELECT seat,pool FROM record_launches').fetchall() == [('worker-z','pool-z')]
        with Ledger.reading(path) as reading:
            assert reading.incomplete_records() == [(str(record),'worker-z','pool-z','synthetic unavailable measurement')]


def test_account_pass_reconciles_new_turn_metadata_launch_prices_and_posts(tmp_path, monkeypatch):
    import json
    from dataclasses import replace

    svc, seat, record, turns, reads, at = accounting_example(tmp_path, monkeypatch, 1)
    order = post.Message('ord-task-z', 'order', 'issuer-z', 'worker-z', at-timedelta(minutes=1), 'synthetic task')
    svc.store.write(order)
    svc.account()
    assert svc.ledger.db.execute('SELECT order_id FROM turns').fetchone() == (order.id,)
    trace = []
    svc.ledger.db.set_trace_callback(trace.append)
    turns.append(replace(turns[0], index=1, started=at+timedelta(seconds=4), ended=at+timedelta(seconds=5)))
    turns[0] = replace(turns[0], tokens=turns[0].tokens.__class__(1,2,3,9), cost=0.5)
    record.write_text('synthetic new turn and late measurement')
    svc.account()
    assert trace.count('COMMIT') == 1
    assert svc.ledger.db.execute('SELECT COUNT(*),SUM(written),SUM(cost) FROM turns').fetchone() == (2,13,0.75)
    trace.clear()
    # Pricing/launch evidence can change while source bytes remain unchanged.
    journal = seat.trusted/'record-launches.jsonl'
    launch = json.loads(journal.read_text())
    launch['price'] = {'synthetic-model-z': 2}
    journal.write_text(json.dumps(launch)+'\n')
    svc.account()
    assert trace.count('COMMIT') == 1 and reads == [record, record, record]
    assert svc.ledger.db.execute('SELECT price FROM launch_prices').fetchone() == (json.dumps(launch['price'],sort_keys=True),)
    # A new related post replays attribution even though record and launch bytes stand still.
    trace.clear()
    report = post.Message('rep-done-z', 'report', 'worker-z', 'issuer-z', at+timedelta(milliseconds=500), 'synthetic done', order.id)
    svc.store.write(report)
    svc.account()
    assert trace.count('COMMIT') <= 1
    assert svc.ledger.attributions.details()[0]['orders'] == [order.id]
    assert reads == [record, record, record]
    trace.clear()
    svc.account()
    assert sql_writes(trace) == []
    svc.ledger.close()


def test_account_pass_keeps_due_usage_attempt_outside_record_transaction(tmp_path, monkeypatch):
    from quarterdeck import service

    svc, seat, record, turns, reads, at = accounting_example(tmp_path, monkeypatch, 1)
    pending = [Usage((Window('synthetic-window-z', 0.25, at+timedelta(hours=1)),), at, called=False)]

    def refresh(instance, current, live, launched):
        assert not instance.ledger.db.in_transaction
        if pending:
            instance.ledger.record_usage('pool-z', pending.pop(), attempted_at=at)

    monkeypatch.setattr(service.quota, 'refresh', refresh)
    trace = []
    svc.ledger.db.set_trace_callback(trace.append)
    svc.account()
    assert trace.count('COMMIT') == 2, 'one record batch plus one separately owned actual usage attempt'
    assert svc.ledger.db.execute('SELECT at FROM usage_reads WHERE pool=?', ('pool-z',)).fetchone() == (at.isoformat(),)
    trace.clear()
    svc.account()
    assert sql_writes(trace) == []
    svc.ledger.close()
