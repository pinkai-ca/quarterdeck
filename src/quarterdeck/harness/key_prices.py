"""What a turn on a key costs, where the program states no cost: the pool's declared price, or Anthropic's list.

The table is Anthropic's published list, read from platform.claude.com/docs/en/about-claude/pricing on 2026-11-21, in
USD per million tokens. A cache write is at the five-minute rate, the one Claude Code 2.1.317 asks for on a key
(measured: ``cache_control`` with no ``ttl``). A model not in the table has no price, and its turns count as unpriced.

Claude Haiku 5.5 costs more for a request whose prompt is over 100,000 tokens. OpenCode and pi record each request, so
each is priced on its own. Claude Code's turn sums its requests, so a turn whose prompts together pass that line is
priced at the higher rates: the figure can run high, and the seat then moves early, which is the safe side of a prepaid
credit. No record reader needs this module, and it is in no reader's stamp set.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from urllib.parse import urlsplit

from ..pricing import Price, amount

LONG_PROMPT = 100_000
LISTED_ON = '2026-11-21'


def _rates(input_rate, write, read, output) -> dict:
    return asdict(Price('USD', 'million_tokens', input_rate, output, read, write))


LISTED = {
    'claude-fable-5-2': _rates(8, 10, 0.20, 40),
    'claude-fable-5-1': _rates(10, 12.50, 0.25, 50),
    'claude-opus-5-5': _rates(4, 5, 0.20, 20),
    'claude-sonnet-5-5': _rates(2, 2.50, 0.10, 10),
    'claude-haiku-5-5': _rates(0.10, 0.125, 0.01, 0.50),
}
LISTED_LONG = {'claude-haiku-5-5': _rates(0.50, 0.625, 0.05, 2.50)}
STATES_NO_COST = ('claude',)  # the native programs whose record holds no cost on a key and whose provider's list is carried
ANTHROPIC_HOST = 'api.anthropic.com'  # an endpoint program on this exact host is billed at the same list


def snapshot(harness_name: str, pool) -> dict | None:
    """The rates a launch on this key pool is priced at, kept with the launch; None where there are none."""
    listed = {'key_rates': LISTED, 'long_rates': LISTED_LONG, 'basis': f'Anthropic list of {LISTED_ON}'}
    if pool.endpoint:  # a declared price is the endpoint program's own launch price
        return listed if not pool.price and urlsplit(pool.endpoint).hostname == ANTHROPIC_HOST else None
    if pool.price:
        return {'key_rates': {'*': pool.price}, 'basis': 'declared pool price'}
    return listed if harness_name in STATES_NO_COST and pool.provider == 'anthropic' else None


def _cost(launch_price: dict, model, tokens) -> float | None:
    rates = launch_price['key_rates']
    chosen = rates.get(model) or rates.get('*')
    if tokens.read_cached + tokens.read_uncached + tokens.cache_written > LONG_PROMPT:
        chosen = launch_price.get('long_rates', {}).get(model) or chosen
    return Price.parse(chosen).cost(tokens) if chosen else None


def _priced_steps(turn, launch_price: dict):
    """Each request on its own: a positive stated cost stands, and a stated zero is priced (it proves nothing)."""
    values, unknown = [], 0
    for step in turn.cost_steps:
        value = amount(step.recorded) or _cost(launch_price, step.model, step.tokens)
        if value is None:
            unknown += 1
        else:
            values.append(value)
    if not values:
        return turn
    models = {step.model for step in turn.cost_steps}
    return replace(turn, cost=amount(sum(values)), cost_basis=launch_price.get('basis'), unpriced_steps=unknown,
                   price_model=next(iter(models)) if len(models) == 1 else None)


def priced(turn, launch_price: dict | None):
    """The turn with its cost, where it states none and its launch names rates for its model."""
    if not isinstance(launch_price, dict) or 'key_rates' not in launch_price:
        return turn
    try:
        if turn.cost_steps:
            return _priced_steps(turn, launch_price)
        if turn.cost is not None:
            return turn
        cost = _cost(launch_price, turn.model, turn.tokens)
    except (AttributeError, TypeError, ValueError):
        return turn
    return turn if cost is None else replace(turn, cost=cost, cost_basis=launch_price.get('basis'), cost_steps=())
