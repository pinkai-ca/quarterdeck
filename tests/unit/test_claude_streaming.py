"""Synthetic native records for retained evidence and raw observation semantics."""

from hashlib import sha256
import json
import os
from types import SimpleNamespace
from weakref import ref

import pytest

from quarterdeck import record_location
from quarterdeck.harness import claude, claude_record, json_record, record_file
from tests.compaction_shapes import Native


def native(path):
    record = Native(path, 'claude')
    record.finish()
    return record


@pytest.mark.parametrize('type_first', [False, True])
def test_large_history_keeps_raw_identity_and_other_fields(tmp_path, type_first):
    record = native(tmp_path / 'synthetic.jsonl')
    padding = 'x' * (claude_record.ROW_BYTES + claude_record.CHUNK)
    large = dict(snapshot={'notes': ['½\"', {'value': padding}]}, version='made-release')
    row = {'type': 'file-history-snapshot', **large} if type_first else {**large, 'type': 'file-history-snapshot'}
    source = record.path.read_bytes() + json.dumps(row, ensure_ascii=False).encode() + b'\n'
    record.path.write_bytes(source)
    with json_record._read_lock:
        snapshot = claude_record.parse(record.path)
        assert snapshot.source == record.path
        assert snapshot.rows[-1][1] == {'type': 'file-history-snapshot', 'version': 'made-release'}
        assert snapshot.raw_digest == sha256(source).hexdigest()
    assert claude.compactions(record.path).extent == len(source)
    assert claude.state(record.path).turn_open is False


@pytest.mark.parametrize('budget', ['row', 'total', 'lines'])
@pytest.mark.parametrize('keep_rows', [False, True])
def test_valid_under_bound_required_evidence_has_a_complete_fallback(tmp_path, monkeypatch, budget, keep_rows):
    record = native(tmp_path / 'synthetic.jsonl')
    monkeypatch.setattr(json_record, '_KEEP_ROWS', False)
    json_record.release(record.path)
    if keep_rows:
        json_record.keep_rows()
    if budget == 'row':
        with record.path.open('ab') as stream:
            stream.write(json.dumps({'type': 'file-history-snapshot',
                                     'message': {'content': 'x' * claude_record.ROW_BYTES}}).encode() + b'\n')
    elif budget == 'total':
        monkeypatch.setattr(claude_record, 'PROJECTED_BYTES', 1)
    else:
        monkeypatch.setattr(claude_record, 'ROWS', 1)
    expected = json_record.object_lines(record.path)
    snapshot = claude_record.parse(record.path)
    assert json_record._entry(snapshot).claude_full and list(snapshot.rows) == expected
    assert claude.state(record.path).turn_open is False
    assert claude.compactions(record.path).reason is None


def test_fallback_read_releases_the_abandoned_projection(tmp_path, monkeypatch):
    record = native(tmp_path / 'synthetic.jsonl')
    projections = []

    class Projection:
        pass

    def scan(*args):
        projection = Projection()
        projections.append(ref(projection))
        raise claude_record.ProjectionLimit('synthetic projection limit')

    read_from = record_file.read_from

    def read_without_projection(*args):
        assert len(projections) == 1 and projections[0]() is None
        return read_from(*args)

    monkeypatch.setattr(claude_record, '_scan_layout', scan)
    monkeypatch.setattr(record_file, 'read_from', read_without_projection)
    assert json_record._entry(claude_record.parse(record.path)).claude_full


@pytest.mark.parametrize('tail', [b'!\n', b'{"type":\n', b'[]\n'])
def test_fallback_preserves_ordinary_diagnostics_and_partial_reading(tmp_path, monkeypatch, tail):
    record = native(tmp_path / 'synthetic.jsonl')
    with record.path.open('ab') as stream:
        stream.write(tail)
    baseline = json_record._parsed(record.path)
    assert len(baseline.skipped) == 1
    monkeypatch.setattr(claude_record, 'PROJECTED_BYTES', 1)
    parsed = claude_record.parse(record.path)
    assert parsed.skipped == baseline.skipped
    assert list(parsed.rows) == list(baseline.rows)
    assert claude.state(record.path).turn_open is False
    assert claude.state(record.path).pending_tools is None
    assert claude.inputs(record.path).diagnostics()['total'] == 1
    assert claude.compactions(record.path).reason is not None


def test_fallback_keeps_the_ordinary_whole_file_refusal(tmp_path, monkeypatch):
    record = native(tmp_path / 'synthetic.jsonl')
    with record.path.open('ab') as stream:
        stream.write(b' ' * (claude_record.CHUNK + 1) + b'\xff\n')
    with pytest.raises(record_file.RecordError) as baseline:
        json_record._parsed(record.path)
    monkeypatch.setattr(claude_record, 'PROJECTED_BYTES', 1)
    for read in (claude.state, claude.inputs):
        with pytest.raises(record_file.RecordError) as refused:
            read(record.path)
        assert str(refused.value) == str(baseline.value)
    assert record.path not in json_record._snapshots
    assert claude.compactions(record.path).reason is not None


def test_larger_synthetic_stream_cannot_enter_the_full_compatibility_reader(tmp_path, monkeypatch):
    record = native(tmp_path / 'synthetic.jsonl')
    monkeypatch.setattr(record_file, 'CLAUDE_STREAM_BYTES', 1)
    monkeypatch.setattr(claude_record, 'COMPATIBILITY_BOUND', 1)

    def unexpected(*_):
        pytest.fail('compatibility reader exceeded its own raw bound')

    monkeypatch.setattr(record_file, 'read_from', unexpected)
    with pytest.raises(claude_record.ProjectionLimit):
        claude.state(record.path)


@pytest.mark.parametrize('continuous', [False, True])
def test_full_compatibility_observation_retries_only_a_stable_source(tmp_path, monkeypatch, continuous):
    record = native(tmp_path / 'synthetic.jsonl')
    monkeypatch.setattr(claude_record, 'PROJECTED_BYTES', 1)
    original = record_file.read_from
    calls = 0

    def after_read(descriptor, info, source, bound):
        nonlocal calls
        data = original(descriptor, info, source, bound)
        calls += 1
        if calls == 1 or continuous:
            with source.open('ab') as stream:
                stream.write(b'\n')
        return data

    monkeypatch.setattr(record_file, 'read_from', after_read)
    if continuous:
        with pytest.raises(record_file.RecordError, match='changed while being read'):
            claude.state(record.path)
        assert calls == 3
    else:
        assert claude.state(record.path).turn_open is False
        assert calls == 2


@pytest.mark.parametrize('use_full', [False, True])
def test_identical_replacement_refreshes_the_guarded_compaction_proof(tmp_path, monkeypatch, use_full):
    record = native(tmp_path / 'synthetic.jsonl')
    if use_full:
        monkeypatch.setattr(claude_record, 'PROJECTED_BYTES', 1)
    first = claude.compactions(record.path)
    replacement = record.path.with_suffix('.replacement')
    replacement.write_bytes(record.path.read_bytes())
    os.replace(replacement, record.path)
    later = claude.compactions(record.path)
    assert later.digest == first.digest and later.extent == first.extent
    assert later.prefix.stamp != first.prefix.stamp
    assert later.matches_prefix(later.extent, later.digest)


def test_legacy_physical_lines_preserve_their_number_and_encoding(tmp_path, monkeypatch):
    record = native(tmp_path / 'synthetic.jsonl')
    text = record.path.read_text()
    core = text.replace('\n', '\r\n', 1) + '"not an object"\u2028  \r\n\u00a0\n'
    record.path.write_bytes(core.encode())
    expected = json_record._lines(core, 0)
    monkeypatch.setattr(claude_record, 'CHUNK', 1)
    actual = claude_record.parse(record.path)
    assert (actual.rows, actual.skipped) == expected
    assert actual.line_count == len(core.splitlines())
    assert actual.raw_digest == sha256(core.encode()).hexdigest()


def test_interrupted_scan_closes_its_descriptor_without_admitting_evidence(tmp_path, monkeypatch):
    record = native(tmp_path / 'synthetic.jsonl')
    original = claude_record.read_piece
    descriptors = []

    def stop(descriptor, remaining):
        descriptors.append(descriptor)
        original(descriptor, remaining)
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(claude_record, 'read_piece', stop)
        with pytest.raises(KeyboardInterrupt):
            claude_record.parse(record.path)
    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
    assert record.path not in json_record._snapshots
    assert claude.state(record.path).turn_open is False


def test_retired_location_keeps_the_logical_record_but_reestablishes_the_proof(tmp_path):
    path = tmp_path / 'runtime' / 'seats' / 'synthetic' / 'home' / 'projects' / 'repo' / 'synthetic.jsonl'
    path.parent.mkdir(parents=True)
    record = native(path)
    before = claude.compactions(record.path)
    assert before.reason is None
    moved = record_location.destination(path)
    assert moved is not None
    moved.parent.mkdir(parents=True)
    os.rename(path, moved)
    with pytest.raises(OSError):
        before.matches_prefix(before.extent, before.digest)
    after = claude.compactions(path)
    assert after.digest == before.digest and after.prefix.record == path and after.prefix.source == moved
    assert after.matches_prefix(after.extent, after.digest)


def test_production_bound_refuses_before_scanning_any_body(tmp_path, monkeypatch):
    path = tmp_path / 'synthetic.jsonl'
    with path.open('wb') as record:
        record.truncate(record_file.CLAUDE_STREAM_BOUND + 1)

    def unexpected(*_):
        pytest.fail('a past-bound record read body bytes')

    monkeypatch.setattr(claude_record, 'read_piece', unexpected)
    with pytest.raises(record_file.PastBound) as refusal:
        claude.state(path)
    assert refusal.value.bound == 1024 * 1024 * 1024
    assert refusal.value.size == refusal.value.bound + 1


def test_large_projection_is_kept_for_certified_reads_with_accounting_rows(tmp_path, monkeypatch):
    record = native(tmp_path / 'synthetic.jsonl')
    monkeypatch.setattr(claude_record, 'COMPATIBILITY_BOUND', 1)
    monkeypatch.setattr(json_record, '_KEEP_ROWS', True)
    monkeypatch.setattr(json_record, '_KEPT_BYTES', 1)
    info = record.path.stat()
    aged = max(info.st_mtime_ns, info.st_ctime_ns) + json_record._TIMESTAMP_TICK_NS + 1
    monkeypatch.setattr(claude_record, 'time', SimpleNamespace(time_ns=lambda: aged))
    first = claude_record.parse(record.path)
    assert first.retained_cost and first.certified_at is not None
    json_record.release(record.path, keep_projection=True)
    monkeypatch.setattr(claude_record, 'read_piece', lambda *_: pytest.fail('a certified observation read raw bytes'))
    assert claude_record.parse(record.path) is first
    assert claude.state(record.path).projection_usage.lines == first.line_count
    assert claude.compactions(record.path).extent == first.total_bytes
    json_record.release(record.path)


def test_projected_charge_bounds_the_cache_across_large_records(tmp_path, monkeypatch):
    paths = [native(tmp_path / f'synthetic-{i}.jsonl').path for i in range(2)]
    monkeypatch.setattr(claude_record, 'COMPATIBILITY_BOUND', 1)
    first = claude_record.parse(paths[0])
    monkeypatch.setattr(json_record, '_CLAUDE_CACHE_BYTES', first.retained_cost)
    second = claude_record.parse(paths[1])
    assert second.retained_cost == first.retained_cost
    assert paths[0] not in json_record._snapshots and paths[1] in json_record._snapshots
    json_record.release(paths[1])


@pytest.mark.parametrize(('budget', 'resource'), [
    ('CLAUDE_STREAM_ROWS', 'physical lines'), ('CLAUDE_STREAM_BYTES', 'retained bytes'),
    ('CLAUDE_STREAM_MEMORY', 'retained memory'),
])
def test_a_large_stream_with_an_exhausted_projection_reports_its_first_resource(tmp_path, monkeypatch, budget, resource):
    record = native(tmp_path / 'synthetic.jsonl')
    monkeypatch.setattr(claude_record, 'COMPATIBILITY_BOUND', 1)
    monkeypatch.setattr(record_file, budget, 1)
    with pytest.raises(claude_record.ProjectionLimit) as refused:
        claude.state(record.path)
    assert refused.value.projection_usage.exhausted == resource
    assert refused.value.projection_usage.stamp[2] == record.path.stat().st_size
    assert record.path not in json_record._snapshots


def test_large_blank_physical_line_is_ignored(tmp_path):
    record = native(tmp_path / 'synthetic.jsonl')
    with record.path.open('ab') as stream:
        stream.write(b' ' * (claude_record.ROW_BYTES + 1) + b'\n')
    assert not claude_record.parse(record.path).skipped
    assert claude.compactions(record.path).reason is None


def test_callers_with_a_generic_view_never_receive_omitted_claude_fields(tmp_path):
    record = native(tmp_path / 'synthetic.jsonl')
    metadata = {'type': 'file-history-snapshot', 'snapshot': {'value': 'synthetic'}}
    with record.path.open('ab') as stream:
        stream.write(json.dumps(metadata).encode() + b'\n')
    assert claude_record.entries(record.path)[-1] == {'type': 'file-history-snapshot'}
    assert json_record.entries(record.path)[-1] == metadata
    assert claude_record.entries(record.path)[-1] == {'type': 'file-history-snapshot'}
