"""Endpoint costs keep each step's amount and model evidence through accounting."""

from datetime import datetime, timedelta, timezone
import json

import pytest

from quarterdeck import cli, ledger_reading
from quarterdeck.harness import pi, opencode
from quarterdeck.ledger import Ledger
from quarterdeck.post import Store
from quarterdeck.pricing import Price
from tests.unit.test_opencode_record import database, session, user, message, MS
from tests.unit.test_price_rollback import old

AT = datetime.fromtimestamp(MS / 1000, timezone.utc)
RATES = dict(currency='USD', unit='million_tokens', input=2, output=4, cache_read=1, cache_write=3)


def source(path, name, steps):
    if path.exists():
        path.unlink()
    if name == 'pi':
        rows = [{'type': 'message', 'timestamp': AT.isoformat(),
                 'message': {'role': 'user', 'content': 'synthetic input'}}]
        for index, (model, cost) in enumerate(steps):
            data = {'role': 'assistant', 'content': [], 'stopReason': 'toolUse' if index < len(steps)-1 else 'stop',
                    'usage': {'input': 1_000_000}}
            if model is not None:
                data['model'] = model
            if cost is not None:
                data['usage']['cost'] = {'total': cost}
            rows.append({'type': 'message', 'timestamp': (AT+timedelta(seconds=index+1)).isoformat(), 'message': data})
        path.write_text('\n'.join(json.dumps(row) for row in rows))
        return pi.PiHarness()
    with database(path) as con:
        session(con)
        user(con, 'u', 'root', 10, 'synthetic input')
        for index, (model, cost) in enumerate(steps):
            data = {'role': 'assistant', 'parentID': 'u', 'time': {'created': MS+20+index, 'completed': MS+21+index},
                    'finish': 'tool-calls' if index < len(steps)-1 else 'stop',
                    'tokens': {'total': 1_000_000, 'input': 1_000_000}}
            if model is not None:
                data['modelID'] = model
            if cost is not None:
                data['cost'] = cost
            message(con, f'a{index}', 'root', 20+index, data)
    return opencode.OpenCodeHarness()


@pytest.mark.parametrize('name', ['pi', 'opencode'])
@pytest.mark.parametrize('steps,expected', [([('unpriced', 0), ('m', 2)], 2),
                                          ([('m', 0), (None, 0)], 2)])
def test_reproduced_step_evidence_blockers_reach_the_ledger(tmp_path, name, steps, expected):
    path = tmp_path / 'record'
    reader = source(path, name, steps)
    turn = reader.price_turn(reader.turns(path)[0], Price.parse(RATES).snapshot('m'))
    with Ledger(tmp_path / 'ledger.sqlite') as book:
        book.ingest('worker', 'pool', path, [turn], Store(tmp_path))
        assert book.rollup('pool')[0].cost == expected
        assert book.rollup('pool')[0].unpriced == 1


@pytest.mark.parametrize('name', ['pi', 'opencode'])
@pytest.mark.parametrize('steps,rate,expected,missing', [
    ([('unpriced', 0), ('m', 2)], 2, 2, 1),
    ([('unpriced', None), ('m', 2)], 2, 2, 1),
    ([('unpriced', 'invalid'), ('m', 2)], 2, 2, 1),
    ([('m', 0), (None, 0)], 2, 2, 1),
    ([('m', 0), ([], 0)], 2, 2, 1),
    ([('m', 0), ('m', 0)], 2, 4, 0),
    ([('m', None), ('m', None)], 2, 4, 0),
    ([('m', 0), ('m', 0)], 0, 0, 0),
    ([('m', 0), ('unpriced', 0)], 0, 0, 1),
    ([('unpriced', None), (None, 0)], 2, None, 2),
    ([('other', 2), ('m', 3)], 2, 5, 0),
])
def test_step_amounts_and_uncertainty_reach_each_rollup_spend_and_json(tmp_path, monkeypatch, capsys,
                                                                     name, steps, rate, expected, missing):
    monkeypatch.delenv('QD_SHIP', raising=False)
    monkeypatch.delenv('QD_SEAT', raising=False)
    (tmp_path / 'ship.toml').write_text('[ship]\nname="synthetic"\n[pools.pool]\nprovider="claude"\n'
                                     'api_key="keys:synthetic-unused"\nbudget="100 USD/month"\n')
    monkeypatch.setattr(ledger_reading.Budget, 'period_start', lambda self, now: AT-timedelta(seconds=1))
    path = tmp_path / 'record'
    reader = source(path, name, steps)
    raw = reader.turns(path)[0]
    priced = reader.price_turn(raw, Price.parse({**RATES, 'input': rate}).snapshot('m'))
    assert priced.cost == expected and priced.unpriced_steps == missing
    if steps[1][0] is None or isinstance(steps[1][0], list):
        assert priced.price_model is None
        assert priced.cost_steps[1].model is None
        if steps[0][0] == 'm':
            assert priced.model == 'm', 'the display may retain the preceding model'
    with Ledger(tmp_path / 'runtime' / 'ledger.sqlite') as book:
        book.ingest('worker', 'pool', path, [priced], Store(tmp_path / 'runtime'))
        assert book.costs.get(path, 0)[0] == priced.recorded_cost, 'computed cost is not invented raw record evidence'
        assert book.spend('pool', AT-timedelta(seconds=1)) == (expected, int(missing > 0))
        evidence = json.loads(book.costs.details(path, 0)[1])
        assert len(evidence) == 2 and evidence[1]['recorded'] == priced.cost_steps[1].recorded
        for by in ('pool', 'seat', 'order'):
            row = book.rollup(by)[0]
            assert row.cost == expected and row.unpriced == int(missing > 0)
            text = '\n'.join(ledger_reading.render(ledger_reading.collect(book, by=by)))
            if expected is not None:
                assert f'{expected:.2f}' + ('+' if missing else '') in text
    for by in ('pool', 'seat', 'order'):
        assert cli.main(['--ship', str(tmp_path), '--json', 'ledger', '--by', by]) == 0
        row = json.loads(capsys.readouterr().out)['rows'][0]
        assert row['cost'] == expected and row['unpriced'] == int(missing > 0)
    assert cli.main(['--ship', str(tmp_path), '--json', 'ledger']) == 0
    pool = json.loads(capsys.readouterr().out)['pools'][0]
    assert pool['spent'] == expected and pool['unpriced'] == int(missing > 0)


@pytest.mark.parametrize('name', ['pi', 'opencode'])
def test_late_source_evidence_can_introduce_and_resolve_uncertainty(tmp_path, name):
    path = tmp_path / 'record'
    price = Price.parse(RATES).snapshot('m')
    cases = [([('m', 2), ('m', 2)], 4, 0),
             ([('unpriced', 0), ('m', 2)], 2, 1),
             ([('unpriced', 0), (None, 0)], None, 1),
             ([('m', 0), ('m', 2)], 4, 0)]
    for steps, cost, unpriced in cases:
        reader = source(path, name, steps)
        turn = reader.price_turn(reader.turns(path)[0], price)
        with Ledger(tmp_path / 'ledger.sqlite') as book:
            book.ingest('worker', 'pool', path, [turn], Store(tmp_path))
            assert book.rollup('pool')[0].cost == cost and book.rollup('pool')[0].unpriced == unpriced
            assert book.ingested(path) == 1
            assert book.ingest('worker', 'other', path, [turn], Store(tmp_path)) == 0


@pytest.mark.parametrize('name', ['pi', 'opencode'])
def test_old_refresh_invalidates_step_certification_but_retains_raw_evidence(tmp_path, old, name):
    path = tmp_path / 'record'
    reader = source(path, name, [('unpriced', 0), ('m', 2)])
    turn = reader.price_turn(reader.turns(path)[0], Price.parse(RATES).snapshot('m'))
    with Ledger(tmp_path / 'ledger.sqlite') as book:
        book.ingest('worker', 'pool', path, [turn], Store(tmp_path))
        detail = book.costs.details(path, 0)
    old('''
import sys
from datetime import datetime
from quarterdeck.ledger import Ledger
from quarterdeck.harness.types import Turn,Tokens
from quarterdeck.post import Store
p=Path(sys.argv[1]); start=datetime.fromisoformat(sys.argv[2]); end=datetime.fromisoformat(sys.argv[3])
with Ledger(p/'ledger.sqlite') as b:
    t=Turn(0,start,end,'synthetic input','','m',Tokens(read_uncached=2000000),cost=0)
    b.ingest('worker','pool',p/'record',[t],Store(p))
''', tmp_path, turn.started.isoformat(), turn.ended.isoformat())
    with Ledger(tmp_path / 'ledger.sqlite') as book:
        assert book.costs.details(path, 0) == detail
        assert book.costs.get(path, 0)[2] == 0
        assert book.rollup('pool')[0].cost is None and book.rollup('pool')[0].unpriced == 1
        book.ingest('worker', 'pool', path, [turn], Store(tmp_path))
        assert book.rollup('pool')[0].cost == 2 and book.rollup('pool')[0].unpriced == 1
        assert book.ingest('worker', 'pool', path, [turn], Store(tmp_path)) == 0
