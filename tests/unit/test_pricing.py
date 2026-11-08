"""Stated endpoint rates are explicit and reach only the launch that used them."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from quarterdeck import endpoint
from quarterdeck.harness import pi, opencode
from quarterdeck.harness.types import Tokens, Turn
from quarterdeck.ledger import Ledger
from quarterdeck.pricing import Price
from quarterdeck.provenance import remember_launch
from quarterdeck.seat import Launched
from quarterdeck.ship import Pool, SeatSpec, ShipError, load

AT = datetime(2032, 1, 1, tzinfo=timezone.utc)
RATES = {"currency": "USD", "unit": "million_tokens", "input": 2, "output": 4, "cache_read": 1, "cache_write": 3}


@pytest.mark.parametrize("change", [{"input": True}, {"output": -1}, {"cache_read": float("nan")},
                                    {"cache_write": float("inf")}, {"input": None}, {"unit": "tokens"},
                                    {"currency": "EUR"}, {"extra": 1}])
def test_invalid_or_unsupported_prices_are_not_guessed(change):
    with pytest.raises(ValueError):
        Price.parse({**RATES, **change})
    with pytest.raises(ValueError):
        Price.parse({k: v for k, v in RATES.items() if k != "cache_write"})


def test_ship_prices_are_explicit_and_refusals_name_the_pool(tmp_path):
    file = tmp_path / "ship.toml"
    header = '[ship]\nname="synthetic"\n[pools.local]\nprovider="local"\nendpoint="http://127.0.0.1:1/v1"\n'
    fields = '[pools.local.price]\ncurrency="USD"\nunit="million_tokens"\ninput=0\noutput=0\ncache_read=0\ncache_write=0\n'
    file.write_text(header)
    assert load(tmp_path).pools["local"].price is None
    file.write_text(header + fields)
    assert Price.parse(load(tmp_path).pools["local"].price).input == 0
    for bad in (fields.replace("input=0", "input=true"), fields.replace("cache_write=0", "")):
        file.write_text(header + bad)
        with pytest.raises(ShipError, match="pool local:"):
            load(tmp_path)


@pytest.mark.parametrize("reader", [pi.PiHarness(), opencode.OpenCodeHarness()])
@pytest.mark.parametrize("stated", [None, RATES, {**RATES, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0}])
def test_generated_configs_omit_unknown_rates_and_carry_actual_declared_rates(tmp_path, monkeypatch, reader, stated):
    monkeypatch.setattr(endpoint, "choose", lambda pool, model: endpoint.Choice(pool.name, "synthetic-model", 4096, 1024))
    home = tmp_path / "runtime" / "seats" / "worker" / "home"
    pool = Pool("local", "local", endpoint="http://127.0.0.1:1/v1", price=stated)
    spec = SeatSpec("worker", "crew", "worker", reader.name, ("local",))
    reader.build_home(home, spec, pool, None)
    if reader.name == "pi":
        model = json.loads((home / "agent" / "models.json").read_text())["providers"]["local"]["models"][0]
        cache_names = ("cacheRead", "cacheWrite")
    else:
        model = json.loads((home / "config" / "opencode" / "opencode.json").read_text())["provider"]["local"]["models"]["synthetic-model"]
        cache_names = ("cache_read", "cache_write")
    if stated is None:
        assert "cost" not in model and endpoint.Choice.read(home).price is None
    else:
        assert model["cost"] == {"input": stated["input"], "output": stated["output"],
                                  cache_names[0]: stated["cache_read"], cache_names[1]: stated["cache_write"]}
    launch = reader.launch(spec, pool, home, tmp_path / "brief", None, None)
    assert launch.price == (Price.parse(stated).snapshot("synthetic-model") if stated else None)
    # Rebuilding with no price removes the previous generated rates rather than retaining a stale configuration.
    reader.build_home(home, spec, replace(pool, price=None), None)
    assert reader.launch(spec, pool, home, tmp_path / "brief", None, None).price is None


def test_price_intervals_survive_restart_without_repricing_earlier_turns(tmp_path):
    record = tmp_path / "record"
    book = Ledger(tmp_path / "ledger.sqlite")
    first = Price.parse(RATES).snapshot("m")
    second = Price.parse({**RATES, "input": 9}).snapshot("m")
    assert book.provenance.remember(record, "seat", "a", AT, first)
    assert book.provenance.remember(record, "seat", "a", AT + timedelta(hours=1), second)
    assert book.provenance.remember(record, "seat", "b", AT + timedelta(hours=2), None)
    assert book.provenance.price(record, AT + timedelta(minutes=1), AT + timedelta(minutes=2)) == first
    assert book.provenance.price(record, AT + timedelta(minutes=61), AT + timedelta(minutes=62)) == second
    assert book.provenance.price(record, AT + timedelta(minutes=119), AT + timedelta(minutes=121)) is None
    assert book.provenance.price(record, AT + timedelta(minutes=59), AT + timedelta(minutes=61)) is None
    assert book.provenance.price(record, None, AT) is None
    assert book.provenance.price(record, AT - timedelta(seconds=1), AT) is None
    book.close()
    with Ledger(tmp_path / "ledger.sqlite") as book:
        assert book.provenance.price(record, AT + timedelta(minutes=1), AT + timedelta(minutes=2)) == first
        assert book.provenance.price(record, AT + timedelta(minutes=121), AT + timedelta(minutes=122)) is None
        assert not book.provenance.remember(record, "seat", "a", AT, first)
        fresh = tmp_path / "fresh"
        book.provenance.remember(fresh, "seat", "b", AT + timedelta(hours=3), second)
        assert book.provenance.price(fresh, AT + timedelta(hours=4), AT + timedelta(hours=5)) == second
        assert book.provenance.price(record, AT + timedelta(minutes=1), AT + timedelta(minutes=2)) == first


def test_launch_journal_keeps_price_snapshot_and_conflicts_are_unknown(tmp_path):
    price = Price.parse(RATES).snapshot("m")
    launched = Launched("%1", 2, AT.isoformat(), "pool", None, [], "synthetic", "pi", price=price)
    record = tmp_path / "record"
    remember_launch(tmp_path, record, launched)
    remember_launch(tmp_path, record, launched)
    rows = (tmp_path / "record-launches.jsonl").read_text().splitlines()
    assert len(rows) == 1 and json.loads(rows[0])["price"] == price
    with Ledger(tmp_path / "ledger.sqlite") as book:
        book.provenance.remember(record, "seat", "pool", AT, price)
        book.provenance.remember(record, "seat", "pool", AT, {**price, "model": "different"})
        assert book.provenance.price(record, AT, AT) == {}, "conflicting metadata cannot establish a price"


@pytest.mark.parametrize("reader", [pi.PiHarness(), opencode.OpenCodeHarness()])
def test_default_zero_needs_price_evidence_but_real_cost_is_not_erased(reader):
    turn = Turn(0, AT, AT, "in", "out", "m", Tokens(read_uncached=1_000_000), cost=None, recorded_cost=0, price_model="m")
    assert reader.price_turn(turn, None).cost is None
    price = Price.parse(RATES).snapshot("m")
    assert reader.price_turn(turn, price).cost == 2
    zero = Price.parse({**RATES, "input": 0}).snapshot("m")
    assert reader.price_turn(turn, zero).cost == 0
    assert reader.price_turn(turn, {**price, "model": "other"}).cost is None
    assert reader.price_turn(replace(turn, tokens=Tokens.unknown()), price).cost is None
    assert reader.price_turn(replace(turn, cost=3, recorded_cost=3, cost_basis="record"), None).cost == 3


@pytest.mark.parametrize("reader", [pi.PiHarness(), opencode.OpenCodeHarness()])
def test_mixed_model_turn_cannot_use_last_models_price(tmp_path, reader):
    from tests.unit.test_opencode_record import database, session, user, message, MS

    if reader.name == 'pi':
        path = tmp_path / 'record.jsonl'
        path.write_text('\n'.join(json.dumps({'type': 'message', 'message': {
            'role': 'assistant', 'model': model, 'content': [], 'stopReason': stop,
            'usage': {'input': 1_000_000, 'cost': {'total': 0}}}})
            for model, stop in [('other', 'toolUse'), ('m', 'stop')]))
    else:
        path = tmp_path / 'record.db'
        with database(path) as con:
            session(con)
            user(con, 'u', 'root', 10, 'synthetic')
            for index, model in enumerate(('other', 'm')):
                message(con, f'a{index}', 'root', 20 + index, {'role': 'assistant', 'parentID': 'u',
                    'modelID': model, 'time': {'created': MS+20+index, 'completed': MS+21+index},
                    'finish': 'tool-calls' if not index else 'stop', 'cost': 0,
                    'tokens': {'total': 1_000_000, 'input': 1_000_000}})
    turn = reader.turns(path)[0]
    assert turn.model == 'm' and turn.tokens.read_uncached == 2_000_000
    assert turn.price_model is None and turn.recorded_cost == 0
    assert reader.price_turn(turn, Price.parse(RATES).snapshot('m')).cost is None


@pytest.mark.parametrize("reader", [pi.PiHarness(), opencode.OpenCodeHarness()])
def test_selected_template_cost_cannot_override_ship_price(tmp_path, monkeypatch, reader):
    monkeypatch.setattr(endpoint, 'choose', lambda pool, model: endpoint.Choice(pool.name, 'm', 4096, 1024))
    template = tmp_path / 'template'
    template.mkdir()
    if reader.name == 'pi':
        (template / 'models.json').write_text(json.dumps({'providers': {'local': {'models': [
            {'id': 'm', 'cost': {'input': 999, 'output': 999, 'cacheRead': 999, 'cacheWrite': 999}}]}}}))
    else:
        (template / 'opencode.json').write_text(json.dumps({'provider': {'local': {'models': {
            'm': {'cost': {'input': 999, 'output': 999}}}}}}))
    home = tmp_path / 'runtime' / 'seats' / 'worker' / 'home'
    spec = SeatSpec('worker', 'crew', 'worker', reader.name, ('local',))
    pool = Pool('local', 'local', endpoint='http://127.0.0.1:1/v1', price=RATES)
    reader.build_home(home, spec, pool, template)
    assert endpoint.Choice.read(home).price == Price.parse(RATES).snapshot('m')
    config = home / ('agent/models.json' if reader.name == 'pi' else 'config/opencode/opencode.json')
    assert '999' not in config.read_text()
    reader.build_home(home, spec, replace(pool, price=None), template)
    assert '"cost"' not in config.read_text() and endpoint.Choice.read(home).price is None
