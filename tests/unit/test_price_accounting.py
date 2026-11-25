"""Real accounting uses saved endpoint launches, not the seat's latest pool settings."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from quarterdeck import cli, service
from quarterdeck.harness import key_prices
from quarterdeck.harness.grok import GrokHarness
from quarterdeck.harness.types import Tokens, Turn
from quarterdeck.pricing import Price
from quarterdeck.provenance import remember_launch
from quarterdeck.seat import Launched, Seat
from quarterdeck.service import Service
from quarterdeck.ship import Pool, SeatSpec, Session, Ship, load

AT = datetime(2032, 1, 1, tzinfo=timezone.utc)
RATES = dict(currency='USD', unit='million_tokens', input=2, output=4, cache_read=1, cache_write=3)


def append_turn(path, minutes):
    rows = [{'type': 'message', 'timestamp': (AT + timedelta(minutes=minutes)).isoformat(),
             'message': {'role': 'user', 'content': 'synthetic input'}},
            {'type': 'message', 'timestamp': (AT + timedelta(minutes=minutes, seconds=1)).isoformat(),
             'message': {'role': 'assistant', 'model': 'm', 'content': [], 'stopReason': 'stop',
                         'usage': {'input': 1_000_000, 'cost': {'total': 0}}}}]
    with path.open('a') as stream:
        stream.write(''.join(json.dumps(row) + '\n' for row in rows))


def test_account_prices_resume_fresh_pool_edit_and_restart_from_launch_evidence(tmp_path, monkeypatch):
    pool = Pool('a', 'local', endpoint='http://127.0.0.1:1/v1', price=RATES)
    spec = SeatSpec('worker', 'crew', 'worker', 'pi', ('a', 'b'), cwd=tmp_path)
    ship = Ship(tmp_path, 'synthetic', 'operator', tmp_path, {'a': pool, 'b': replace(pool, name='b')},
                {'crew': Session('crew', 'crew')}, {'worker': spec})
    seat = Seat(ship, spec, SimpleNamespace(pane=lambda ident: None))
    folder = seat.home / 'sessions'
    folder.mkdir(parents=True)
    record = folder / 'first.jsonl'
    record.touch()
    launched = Launched('%1', 2, AT.isoformat(), 'a', None, ['pi'], 'synthetic', 'pi')

    def pin(path, launch):
        seat.trusted.mkdir(parents=True, exist_ok=True)
        (seat.trusted / 'record').write_text(str(path))
        launch.write(seat.trusted / 'launch.json')
        remember_launch(seat.trusted, path, launch)

    pin(record, launched)  # old launch had no stated price
    append_turn(record, 1)
    monkeypatch.setattr(service, 'seats', lambda *args: [seat])
    monkeypatch.setattr(service.machine, 'processes', lambda: {})
    svc = Service(ship, seat.tmux)
    svc.account()
    assert svc.ledger.rollup('pool')[0].unpriced == 1
    # Editing a pool does not retroactively price this launch, even after restart.
    ship.pools['a'] = replace(pool, price={**RATES, 'input': 99})
    svc.ledger.close()
    svc = Service(ship, seat.tmux)
    svc.account()
    assert svc.ledger.db.execute('SELECT t.cost,c.recorded_cost FROM turns t JOIN turn_costs c USING(record,turn)').fetchall() == [(None, 0)]
    resumed = replace(launched, at=(AT + timedelta(minutes=2)).isoformat(), resume='synthetic',
                      price=Price.parse(RATES).snapshot('m'))
    pin(record, resumed)
    append_turn(record, 3)
    svc.account()
    zero = replace(resumed, at=(AT + timedelta(minutes=4)).isoformat(), pool='b',
                   price=Price.parse({**RATES, 'input': 0}).snapshot('m'))
    pin(record, zero)
    append_turn(record, 5)
    svc.account()
    assert svc.ledger.db.execute('SELECT turn,pool,cost FROM turns ORDER BY turn').fetchall() == [(0,'a',None),(1,'a',2),(2,'b',0)]
    fresh = folder / 'fresh.jsonl'
    pin(fresh, replace(zero, at=(AT + timedelta(minutes=6)).isoformat(), resume=None, price=None))
    append_turn(fresh, 7)
    svc.account()
    svc.ledger.close()
    svc = Service(ship, seat.tmux)
    svc.account()
    assert svc.ledger.db.execute('SELECT t.pool,t.cost,c.recorded_cost FROM turns t JOIN turn_costs c USING(record,turn) ORDER BY record,turn').fetchall() == [
        ('a',None,0),('a',2,0),('b',0,0),('b',None,0)]
    before = svc.ledger.db.total_changes
    svc.account()
    assert svc.ledger.db.total_changes == before
    svc.ledger.close()


@pytest.mark.parametrize("pool,version", [
    (Pool("subscription", "xai"), "1.0.77"),
    (Pool("other-provider", "openai", api_key="keys:other"), "1.0.77"),
    (Pool("endpoint", "xai", api_key="keys:xai", endpoint="https://api.x.ai/v1"), "1.0.77"),
    (Pool("unmeasured-release", "xai", api_key="keys:xai"), "1.0.92"),
    (Pool("unmeasured-release", "xai", api_key="keys:xai"), None),
])
def test_grok_stated_cost_requires_a_measured_native_xai_key_launch(pool, version):
    adapter = GrokHarness()
    snapshot = adapter.recorded_cost_snapshot(pool, version)
    assert snapshot is None
    turn = Turn(0, AT, AT + timedelta(seconds=1), "", "", "grok", Tokens(20, 60, 0, 12),
                recorded_cost=1.25)
    assert adapter.price_turn(turn, snapshot).cost is None


def test_grok_native_cost_precedes_rates_and_missing_cost_keeps_the_fallback():
    adapter = GrokHarness()
    pool = Pool("key", "xai", api_key="keys:xai", price=RATES)
    rates = key_prices.snapshot("grok", pool)
    snapshot = {**rates, **adapter.recorded_cost_snapshot(pool, "1.0.77")}
    turn = Turn(0, AT, AT + timedelta(seconds=1), "", "", "grok", Tokens(0, 1_000_000, 0, 0),
                recorded_cost=1.25)
    assert key_prices.priced(adapter.price_turn(turn, snapshot), snapshot).cost == 1.25
    for older_or_missing, saved in ((turn, rates), (replace(turn, recorded_cost=None), snapshot)):
        priced = key_prices.priced(adapter.price_turn(older_or_missing, saved), saved)
        assert (priced.cost, priced.cost_basis) == (2.0, "declared pool price")


def test_grok_key_spend_uses_the_turns_saved_launch_in_ledger_and_status(drydock, monkeypatch, capsys):
    declaration = drydock / "ship.toml"
    with declaration.open("a") as stream:
        stream.write("""
[pools.reused]
provider = "xai"
api_key = "keys:synthetic-old"
budget = "20 USD from 2020-01-01 until 2099-01-01"

[pools.grok-key]
provider = "xai"
api_key = "keys:synthetic-xai"
budget = "20 USD from 2020-01-01 until 2099-01-01"

[seats.grok-worker]
role = "worker"
session = "crew"
harness = "grok"
pool = "grok-key"
""")
    ship = load(drydock)
    spec = ship.seats["grok-worker"]
    seat = Seat(ship, spec, SimpleNamespace(pane=lambda ident: None))
    record = seat.home / "sessions" / "synthetic-date" / "synthetic-session" / "updates.jsonl"
    record.parent.mkdir(parents=True)
    now = datetime.now(timezone.utc) - timedelta(minutes=10)

    def row(second, event, method="_x.ai/session/update"):
        return {"timestamp": (now + timedelta(seconds=second)).timestamp(), "method": method,
                "params": {"sessionId": "synthetic-session", "update": event}}

    def prompt(index, ticks):
        terminal = {"sessionUpdate": "turn_completed", "prompt_id": f"prompt-{index}",
                    "stop_reason": "end_turn" if ticks is not None else "cancelled"}
        if ticks is not None:
            terminal["usage"] = {"costUsdTicks": ticks, "modelCalls": 1, "numTurns": 1}
        return [
            row(index * 10, {"sessionUpdate": "user_message_chunk",
                              "content": {"type": "text", "text": "synthetic input"}}, "session/update"),
            row(index * 10 + 1, terminal),
        ]

    def append(rows):
        with record.open("a") as stream:
            stream.writelines(json.dumps(item) + "\n" for item in rows)

    snapshot = seat.harness.recorded_cost_snapshot(ship.pools["grok-key"], "1.0.77")
    historical = Launched("%1", 42, (now + timedelta(seconds=5)).isoformat(), "reused", None,
                          ["grok"], "1.0.77", "grok")
    current = replace(historical, at=(now + timedelta(seconds=15)).isoformat(), pool="grok-key", price=snapshot)
    seat.trusted.mkdir(parents=True, exist_ok=True)
    launch_file = seat.trusted / "launch.json"
    (seat.trusted / "record").write_text(str(record))
    remember_launch(seat.trusted, record, historical)  # subscription or an older key without a saved marker
    current.write(launch_file)
    assert seat.launched().price == snapshot
    remember_launch(seat.trusted, record, current)

    append(prompt(0, 9_000_000_000))  # predates the first saved launch
    append(prompt(1, 8_000_000_000))  # the pool is a key now, but its old launch does not prove that
    first = prompt(2, 301_331_000)
    second = prompt(3, 49_009_000)
    append([*first, first[-1], *second, *prompt(4, None)])
    # The terminal updates omit modelUsage. The separate summary is cumulative
    # and only includes the two completed prompts from the current launch.
    total = {"costUsdTicks": 350_340_000, "modelCalls": 2, "turnCount": 2}
    (record.parent / "usage.json").write_text(json.dumps({
        "sessionId": "synthetic-session",
        "session": {**total, "modelUsage": {"synthetic-grok": total}},
        "turns": [
            {"turnNumber": 1, "costUsdTicks": 301_331_000},
            {"turnNumber": 2, "costUsdTicks": 49_009_000},
        ],
    }))
    monkeypatch.setattr(service, "seats", lambda *args: [seat])
    monkeypatch.setattr(service.machine, "processes", lambda: {})
    svc = Service(ship, seat.tmux)
    svc.account()

    def charges():
        return svc.ledger.db.execute("SELECT pool,cost FROM turns ORDER BY turn").fetchall()

    assert charges() == [("-", None), ("reused", None), ("grok-key", 0.0301331),
                         ("grok-key", 0.0049009), ("grok-key", None)]
    svc.ledger.close()
    # A restarted service reads the same launch companion and counts each prompt once.
    svc = Service(ship, seat.tmux)
    svc.account()
    spent, unpriced = svc.ledger.spend("grok-key", now)
    assert spent == pytest.approx(0.035034)
    assert unpriced == 1
    for name in ("QD_SHIP", "QD_SEAT", "QD_TMUX_SOCKET"):
        monkeypatch.delenv(name, raising=False)

    assert cli.main(["--ship", str(drydock), "--json", "ledger"]) == 0
    ledger = json.loads(capsys.readouterr().out)
    pools = {pool["name"]: pool for pool in ledger["pools"]}
    assert pools["grok-key"]["spent"] == pytest.approx(0.035034)
    assert pools["grok-key"]["unpriced"] == 1
    assert (pools["reused"]["spent"], pools["reused"]["unpriced"]) == (None, 1)
    assert ledger["unknown_pool_turns"] == 1
    assert cli.main(["--ship", str(drydock), "status"]) == 0
    status = capsys.readouterr().out
    key_status = next(line for line in status.splitlines() if "key synthetic-xai:" in line)
    assert "0.04+ USD spent" in key_status
    assert "(1 turns with no price)" in key_status
    assert "key synthetic-old: no cost stated" in status

    # A later conflicting completion invalidates only that prompt's recorded cost.
    append([prompt(2, 301_331_001)[-1]])
    svc.account()
    assert charges()[2:4] == [("grok-key", None), ("grok-key", 0.0049009)]
    assert svc.ledger.spend("grok-key", now) == (0.0049009, 2)
    svc.ledger.close()
