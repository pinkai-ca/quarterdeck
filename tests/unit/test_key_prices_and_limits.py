"""A key's turns priced where the program states no cost, and a provider's refusal in the measured forms."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import quota, ship as shipmod
from quarterdeck.harness import key_limits, key_prices
from quarterdeck.harness.costs import costed
from quarterdeck.harness.types import CostStep, Tokens, Turn, TurnFailure
from quarterdeck.ledger import Ledger
from quarterdeck.post import Store
from quarterdeck.ship import Pool, ShipError

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
ANTHROPIC = Pool('paid', 'anthropic', api_key='keys:anthropic')


def turn(model, cached=0, uncached=0, written_cache=0, output=0, cost=None):
    return Turn(0, NOW - timedelta(minutes=1), NOW, 'x', 'y', model,
                Tokens(read_cached=cached, read_uncached=uncached, cache_written=written_cache, written=output), cost=cost)


@pytest.mark.parametrize('model,expected', [
    ('claude-fable-5-2', 8 + 10 + 0.20 + 40), ('claude-fable-5-1', 10 + 12.50 + 0.25 + 50),
    ('claude-opus-5-5', 4 + 5 + 0.20 + 20), ('claude-sonnet-5-5', 2 + 2.50 + 0.10 + 10),
])
def test_each_model_is_priced_at_its_listed_rates(model, expected):
    million = 1_000_000
    priced = key_prices.priced(turn(model, million, million, million, million), key_prices.snapshot('claude', ANTHROPIC))
    assert priced.cost == pytest.approx(expected) and priced.cost_basis == 'Anthropic list of 2026-11-21'


def test_haiku_is_priced_by_its_prompt_length():
    launch = key_prices.snapshot('claude', ANTHROPIC)
    short = key_prices.priced(turn('claude-haiku-5-5', 10_000, 40_000, 40_000, 1_000), launch)
    assert short.cost == pytest.approx((10_000 * 0.01 + 40_000 * 0.10 + 40_000 * 0.125 + 1_000 * 0.50) / 1e6)
    long = key_prices.priced(turn('claude-haiku-5-5', 60_000, 40_000, 40_000, 1_000), launch)
    assert long.cost == pytest.approx((60_000 * 0.05 + 40_000 * 0.50 + 40_000 * 0.625 + 1_000 * 2.50) / 1e6)


def test_a_declared_price_wins_and_applies_to_any_model():
    rates = dict(currency='USD', unit='million_tokens', input=1.0, output=2.0, cache_read=0.5, cache_write=1.5)
    pool = Pool('paid', 'anthropic', api_key='keys:anthropic', price=rates)
    priced = key_prices.priced(turn('some-other-model', uncached=1_000_000), key_prices.snapshot('claude', pool))
    assert priced.cost == pytest.approx(1.0) and priced.cost_basis == 'declared pool price'
    assert key_prices.snapshot('codex', pool)['key_rates'] == {'*': rates}


def test_what_has_no_rate_stays_unpriced_and_a_stated_cost_stands():
    launch = key_prices.snapshot('claude', ANTHROPIC)
    assert key_prices.priced(turn('unlisted-model', uncached=5), launch).cost is None
    assert key_prices.priced(turn('claude-opus-5-5', uncached=5, cost=9.0), launch).cost == 9.0
    assert key_prices.priced(turn('claude-opus-5-5', uncached=5), None).cost is None
    assert key_prices.snapshot('codex', Pool('p', 'openai', api_key='keys:openai')) is None


def test_a_priced_turn_counts_toward_the_budget_and_walls_the_pool(tmp_path: Path):
    pool = Pool('paid', 'anthropic', api_key='keys:anthropic', budget='1 USD from 2026-11-01 until 2099-01-01')
    ship = shipmod.Ship(tmp_path, 't', 'operator', tmp_path, {'paid': pool}, {}, {})
    book = Ledger(tmp_path / 'ledger.sqlite')
    now = datetime.now(timezone.utc)
    spent = Turn(0, now - timedelta(minutes=2), now - timedelta(minutes=1), 'x', 'y', 'claude-opus-5-5', Tokens(written=100_000))
    priced = key_prices.priced(spent, key_prices.snapshot('claude', pool))
    book.ingest('worker', 'paid', tmp_path / 'r.jsonl', [priced], Store(tmp_path))
    assert book.spend('paid', now - timedelta(days=1)) == (pytest.approx(2.0), 0)
    assert book.walls(ship) == ['paid budget']
    book.close()


@pytest.mark.parametrize('address,listed', [('https://api.anthropic.com/v1', True), ('https://openrouter.ai/api/v1', False),
                                            ('https://api.anthropic.com.example.invalid/v1', False)])
def test_an_endpoint_program_is_priced_only_on_anthropics_own_host(address, listed):
    pool = Pool('paid', 'anthropic', endpoint=address, api_key='keys:anthropic')
    assert bool(key_prices.snapshot('pi', pool)) is listed


def step_turn(*steps):
    return replace(turn('claude-haiku-5-5'), cost=0.0, cost_steps=tuple(steps))


def test_an_endpoint_programs_stated_zero_is_priced_request_by_request():
    """Through its own price_turn first, as the service runs it: a launch in the key form proves no rate there."""
    launch = key_prices.snapshot('pi', Pool('paid', 'anthropic', endpoint='https://api.anthropic.com/v1', api_key='keys:anthropic'))
    short, long = Tokens(read_uncached=50_000, written=1_000), Tokens(read_uncached=150_000, written=1_000)
    read = costed(step_turn(CostStep('claude-haiku-5-5', short, 0.0), CostStep('claude-haiku-5-5', long, 0.0)), launch)
    assert read.cost is None and read.unpriced_steps == 2
    priced = key_prices.priced(read, launch)
    assert priced.cost == pytest.approx((50_000 * 0.10 + 1_000 * 0.50 + 150_000 * 0.50 + 1_000 * 2.50) / 1e6)
    assert priced.unpriced_steps == 0 and priced.cost_basis == 'Anthropic list of 2026-11-21'


def test_a_stated_positive_cost_stands_and_an_unlisted_model_stays_unpriced():
    launch = key_prices.snapshot('pi', Pool('paid', 'anthropic', endpoint='https://api.anthropic.com/v1', api_key='keys:anthropic'))
    tokens = Tokens(read_uncached=1_000)
    read = costed(step_turn(CostStep('claude-opus-5-5', tokens, 0.5), CostStep('unlisted-model', tokens, 0.0)), launch)
    priced = key_prices.priced(read, launch)
    assert priced.cost == pytest.approx(0.5) and priced.unpriced_steps == 1


def test_a_price_on_a_pool_with_neither_key_nor_endpoint_is_refused(tmp_path):
    (tmp_path / 'ship.toml').write_text('[ship]\nname="t"\n[pools.sub]\nprovider="anthropic"\n[pools.sub.price]\ncurrency="USD"\n'
                                        'unit="million_tokens"\ninput=1\noutput=1\ncache_read=1\ncache_write=1\n')
    with pytest.raises(ShipError, match='supported only for an endpoint pool or a pool paid by a key'):
        shipmod.load(tmp_path)


# -- a provider's refusal ------------------------------------------------------------------------------------------------

def row(error=None, at='2026-11-21T11:59:00.000Z'):
    entry = {'type': 'assistant', 'timestamp': at, 'message': {'content': [{'type': 'text', 'text': 'x'}]}}
    return json.dumps({**entry, 'isApiErrorMessage': True, 'error': error} if error else entry) + '\n'


@pytest.mark.parametrize('error,form', [('billing_error', key_limits.SPEND), ('rate_limit', key_limits.RATE)])
def test_claude_codes_measured_refusals_are_read(tmp_path, error, form):
    record = tmp_path / 'r.jsonl'
    record.write_text(row() + json.dumps({'type': 'user'}) + '\n' + row(error) + json.dumps({'type': 'last-prompt'}) + '\n')
    assert key_limits.claude_form(record) == (form, datetime(2026, 11, 21, 11, 59, tzinfo=timezone.utc))


@pytest.mark.parametrize('text', [row(), row('authentication_failed'), row('rate_limit') + row(), ''])
def test_anything_else_in_a_claude_record_is_no_refusal(tmp_path, text):
    record = tmp_path / 'r.jsonl'
    record.write_text(text)
    assert key_limits.claude_form(record) is None


@pytest.mark.parametrize('code,message,form', [
    ('usage_limit_exceeded', 'Quota exceeded. Check your plan and billing details.', key_limits.SPEND),
    (None, 'exceeded retry limit, last status: 429 Too Many Requests', key_limits.RATE),
    ('other', 'exceeded retry limit, last status: 429', None), (None, 'something else', None),
])
def test_codex_measured_refusals_are_read(code, message, form):
    found = key_limits.codex_form(TurnFailure(message, NOW, code=code))
    assert (found[0] if found else None) == form


def seat_on(tmp_path, record, launched_at):
    ship = SimpleNamespace(pools={'paid': ANTHROPIC})
    return SimpleNamespace(name='worker', harness=SimpleNamespace(name='claude'), ship=ship, record=lambda: record,
                           launched=lambda: SimpleNamespace(pool='paid', at=launched_at))


def test_a_refusal_walls_the_key_for_its_hold_and_then_lets_it_be_tried(tmp_path):
    record = tmp_path / 'r.jsonl'
    record.write_text(row('rate_limit'))
    book = Ledger(tmp_path / 'ledger.sqlite')
    seat = seat_on(tmp_path, record, '2026-11-21T11:00:00+00:00')
    quota.observe_key_refusal(book, seat, NOW)
    quota.observe_key_refusal(book, seat, NOW)
    assert len(book.quota.walls(NOW, pool='paid')) == 1
    assert book.quota.walls(NOW + timedelta(minutes=5), pool='paid') == []
    book.close()


def test_a_refusal_from_before_this_launch_is_not_counted(tmp_path):
    record = tmp_path / 'r.jsonl'
    record.write_text(row('billing_error'))
    book = Ledger(tmp_path / 'ledger.sqlite')
    quota.observe_key_refusal(book, seat_on(tmp_path, record, '2026-11-21T11:59:30+00:00'), NOW)
    assert book.quota.walls(NOW, pool='paid') == []
    book.close()
