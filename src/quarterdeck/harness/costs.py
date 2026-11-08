"""Endpoint step costs: recorded positives, proven rates, and unresolved siblings."""

from dataclasses import replace

from ..pricing import Price, amount
from .types import CostStep


def model_name(value) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def costed(turn, snapshot=None):
    """Resolve each step independently; a positive sibling never certifies a zero."""
    if turn.cost_steps is None:
        return turn  # no step evidence authorizes pricing an aggregate
    try:
        price = Price.parse(snapshot['rates'])
        model = model_name(snapshot['model'])
    except (KeyError, TypeError, ValueError):
        price, model = None, None
    values, raw, bases = [], [], set()
    unknown = 0
    models = set()
    for step in turn.cost_steps:
        models.add(step.model)
        stated = amount(step.recorded)
        if stated is not None:
            raw.append(stated)
        value, basis = (stated, 'record') if stated else (None, None)
        if value is None and price is not None and model is not None and step.model == model:
            value, basis = price.cost(step.tokens), 'stated launch rates'
        if value is None:
            unknown += 1
        else:
            values.append(value)
            bases.add(basis)
    total = amount(sum(values)) if values else None
    if values and total is None:  # a sum outside the numeric representation is not a complete measurement
        unknown = len(turn.cost_steps)
    return replace(turn, cost=total, recorded_cost=amount(sum(raw)) if raw else None,
                   cost_basis=' and '.join(sorted(bases)) if total is not None else None,
                   unpriced_steps=unknown or (1 if not turn.cost_steps else 0),
                   price_model=next(iter(models)) if len(models) == 1 else None)
