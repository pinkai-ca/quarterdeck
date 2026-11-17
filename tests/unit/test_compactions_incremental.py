"""A compaction reading is kept incremental: an appended record feeds only its new lines to the kept interpretation.

The reading after appends equals a whole reading's; an append decodes only its new lines, through the compaction read
as through the readers; a history the interpretation refuses stays refused across appends, and a rewritten record is
read whole again. Synthetic histories only (tests/compaction_shapes.py).
"""
import json
from dataclasses import astuple

import pytest

from quarterdeck.harness import claude, codex, json_record
from tests.compaction_shapes import Native

PROGRAMS = [('claude', claude), ('codex', codex)]


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.setattr(json_record, '_KEEP_ROWS', False)
    json_record._snapshots.clear()
    yield
    json_record._snapshots.clear()


def comparable(reading):
    values = astuple(reading)
    return values[:-2] + (reading.raw, )  # every field but the reader state, which is compared through its own tests


def history(tmp_path, program):
    native = Native(tmp_path / 'record.jsonl', program)
    native.call()
    native.result()
    native.finish()
    return native


@pytest.mark.parametrize('program,module', PROGRAMS)
def test_the_reading_after_appends_equals_a_whole_reading(tmp_path, program, module):
    native = history(tmp_path, program)
    assert module.compactions(native.path).reason is None
    for step in range(3):
        native.begin(f'Synthetic task {step}')
        module.read(native.path)  # a status reading between ticks: the entry moves on
        native.compact()
        native.call()
        native.result()
        native.finish()
        incremental = module.compactions(native.path)
    json_record._snapshots.clear()
    whole = module.compactions(native.path)
    assert incremental.reason is None and incremental.count == 3
    assert comparable(incremental) == comparable(whole)


@pytest.mark.parametrize('program,module', PROGRAMS)
def test_an_append_decodes_only_its_new_lines(tmp_path, monkeypatch, program, module):
    native = history(tmp_path, program)
    module.read(native.path)
    module.compactions(native.path)
    before = len(native.rows)
    decoded = []
    original = json.loads
    monkeypatch.setattr(json_record.json, 'loads', lambda raw, *a, **k: decoded.append(raw) or original(raw, *a, **k))
    native.compact()
    native.begin('Synthetic later task')
    reading = module.compactions(native.path)
    assert reading.reason is None and reading.count == 1
    assert len(decoded) == len(native.rows) - before, (len(decoded), len(native.rows) - before)


def test_a_refused_history_stays_refused_across_appends_and_a_rewrite_reads_whole(tmp_path):
    native = history(tmp_path, 'claude')
    assert claude.compactions(native.path).reason is None
    native.add('system', subtype='compact_boundary', compactMetadata=dict(trigger='unknown'))  # refused
    assert claude.compactions(native.path).reason is not None
    native.finish()
    assert claude.compactions(native.path).reason is not None, 'refused rows stay in the history'
    refused = next(index for index, row in enumerate(native.rows) if row.get('compactMetadata', {}).get('trigger') == 'unknown')
    native.rows = native.rows[:refused]  # rewritten from before the refused row: the prefix no longer proves
    native.previous = native.rows[-1]['uuid']
    native.finish()
    assert claude.compactions(native.path).reason is None
