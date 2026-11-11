"""Bounded error readings and exact input disposition; native facts stay in adapters."""
from dataclasses import replace
import re

from .types import TurnFailure

ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')


def words(value, fallback='error details unavailable'):
    if not isinstance(value,str) or not value.strip():
        return fallback
    text=' '.join(''.join(c for c in ANSI.sub('',value) if c.isprintable() or c.isspace()).split()[:16])
    return text[:160] or fallback


def dispositions(inputs, turns):
    """Keep native acceptance, annotating only exact error-bound input identities."""
    failures={}
    for turn in turns:
        if turn.failure:
            for item in turn.failure.inputs:
                failures.setdefault((item.text.strip(),item.at),[]).append(turn.failure)
    for index,item in enumerate(inputs):
        found=failures.get((item.text.strip(),item.at),())
        if not found or item.kind=='queued':
            continue
        value=('uncertain' if len(found)!=1 or found[0].model_output is None else
               'refused' if found[0].model_output is False else None)
        if value:
            inputs[index]=replace(item,disposition=value)
    return inputs
