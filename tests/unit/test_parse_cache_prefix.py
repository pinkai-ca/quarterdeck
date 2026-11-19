"""The record cache keeps a proved prefix and the adapter-specific retained data.

An appended record decodes only its new lines; a prefix rewritten to other bytes of the same length parses whole; the
cache charge evicts; the readers' state equals a whole parse's; the accounting child keeps rows as
before. Synthetic records only.
"""
import json
import os

import pytest

from quarterdeck.harness import claude, codex, json_record
from quarterdeck.harness.synthetic_record import ClaudeShape, CodexShape


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.setattr(json_record, '_KEEP_ROWS', False, raising=False)
    json_record._snapshots.clear()
    yield
    json_record._snapshots.clear()


def record_with(tmp_path, turns=5, name='home', *, shape=ClaudeShape):
    record = shape(tmp_path / name, tmp_path, 'synthetic', 'synthetic')
    for index in range(turns):
        record.begin(f'Synthetic input {index}', 'synthetic', 'low')
        record.end('Finished')
    return record


def counting(monkeypatch):
    decoded = []
    original = json.loads

    def load(raw, *args, **kwargs):
        decoded.append(raw)
        return original(raw, *args, **kwargs)
    monkeypatch.setattr(json_record.json, 'loads', load)
    return decoded


def lines_of(path):
    return sum(1 for line in path.read_text().splitlines() if line.strip())


def test_the_generic_cache_keeps_no_text_and_no_lines(tmp_path):
    record = record_with(tmp_path, shape=CodexShape)
    codex.read(record.path)
    codex.inputs(record.path)
    entry = json_record._snapshots[record.path]
    assert entry.raw == '' and entry.rows == [] and entry.data is None
    assert entry.prefix_bytes == record.path.stat().st_size and entry.row_count == lines_of(record.path)
    assert entry.readers and entry.inputs, 'reader state and input results are what it keeps'


def test_an_appended_record_decodes_only_its_new_lines(tmp_path, monkeypatch):
    record = record_with(tmp_path)
    claude.read(record.path)
    claude.inputs(record.path)
    before = lines_of(record.path)
    decoded = counting(monkeypatch)
    record.begin('Synthetic appended input', 'synthetic', 'low')
    record.assistant([{'type': 'text', 'text': 'Synthetic growing step'}], None)
    added = lines_of(record.path) - before
    state, turns = claude.read(record.path)
    assert len(decoded) == added, (len(decoded), added)
    assert state.turn_open is True and len(turns) == 5


def test_a_prefix_rewritten_to_other_bytes_of_the_same_length_parses_whole(tmp_path, monkeypatch):
    record = record_with(tmp_path)
    claude.read(record.path)
    text = record.path.read_text()
    changed = text.replace('Synthetic input 0', 'Synthetic input X', 1)
    assert len(changed) == len(text) and changed != text
    stamp = record.path.stat()
    record.path.write_text(changed)
    os.utime(record.path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1))
    decoded = counting(monkeypatch)
    claude.read(record.path)
    assert len(decoded) >= lines_of(record.path), 'the proof failed: every line decoded again'
    assert claude.inputs(record.path)[0].text == 'Synthetic input X'


def test_the_bound_in_bytes_evicts(tmp_path, monkeypatch):
    one, two = record_with(tmp_path, name='one'), record_with(tmp_path, name='two')
    claude.read(one.path)
    claude.read(two.path)
    one_charge = json_record._snapshots[one.path].retained_cost
    two_charge = json_record._snapshots[two.path].retained_cost
    assert one_charge > 0 and two_charge > 0
    json_record._snapshots.clear()
    monkeypatch.setattr(json_record, '_CLAUDE_CACHE_BYTES', one_charge + two_charge - 1)
    claude.read(one.path)
    claude.read(two.path)
    assert list(json_record._snapshots) == [two.path], 'the oldest went to keep the charge under the bound'
    monkeypatch.setattr(json_record, '_CLAUDE_CACHE_BYTES', one_charge - 1)
    json_record._snapshots.clear()
    claude.read(one.path)
    assert not json_record._snapshots, 'a record past the bound is not kept at all'
    assert json_record.cache_size() == (0, 0)


def test_the_readers_state_after_appends_equals_a_whole_parse(tmp_path):
    record = record_with(tmp_path)
    claude.read(record.path)
    claude.inputs(record.path)
    for index in range(4):
        record.begin(f'Synthetic later input {index}', 'synthetic', 'low')
        claude.inputs(record.path)  # an input read between appends, as a delivery's confirmation
        record.assistant([{'type': 'text', 'text': f'Synthetic step {index}'}], None)
        if index % 2:
            record.end('Finished')
        claude.read(record.path)
    incremental = claude.read(record.path), claude.inputs(record.path)
    json_record._snapshots.clear()
    whole = claude.read(record.path), claude.inputs(record.path)
    assert incremental[0][0] == whole[0][0]
    assert incremental[0][1] == whole[0][1]
    assert [item.text for item in incremental[1]] == [item.text for item in whole[1]]


@pytest.mark.parametrize('shape,adapter', [(CodexShape, codex), (ClaudeShape, claude)])
def test_the_accounting_child_keeps_rows_for_each_reader(tmp_path, shape, adapter):
    record = record_with(tmp_path, shape=shape)
    original = record.path.read_text()
    json_record.keep_rows()
    adapter.read(record.path)
    entry = json_record._snapshots[record.path]
    if adapter is claude:
        assert entry.raw == '' and entry.data is None
        assert entry.rows == json_record._lines(original, 0)[0]
    else:
        assert entry.raw == original
    assert len(entry.rows) == lines_of(record.path)


def test_the_cache_size_is_read_each_pass_and_written_with_the_minutes_timing_line(tmp_path):
    from quarterdeck import service_timing
    from quarterdeck.service import Service
    record = record_with(tmp_path)
    claude.read(record.path)
    timings = service_timing.Timings()
    svc = object.__new__(Service)
    svc.timings = lambda: timings
    Service.log_parse_cache(svc)  # each pass: read, not logged
    json_record._snapshots.clear()
    Service.log_parse_cache(svc)
    logged = []
    timings.since -= service_timing.INTERVAL
    timings.publish(lambda event, **data: logged.append((event, data)))
    (event, data), = logged
    size = record.path.stat().st_size
    assert event == 'service-timing'
    assert data['parse_cache'] == {'records': 0, 'record_bytes': 0, 'max_record_bytes': size, 'max_records': 1}
