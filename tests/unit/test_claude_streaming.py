"""Synthetic native records for retained evidence and raw observation semantics."""

from hashlib import sha256
import json
import os

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
def test_exhausted_required_evidence_is_unknown(tmp_path, monkeypatch, budget):
    record = native(tmp_path / 'synthetic.jsonl')
    if budget == 'row':
        with record.path.open('ab') as stream:
            stream.write(json.dumps({'type': 'user', 'message': {'content': 'x' * claude_record.ROW_BYTES}}).encode() + b'\n')
    elif budget == 'total':
        monkeypatch.setattr(claude_record, 'PROJECTED_BYTES', 1)
    else:
        monkeypatch.setattr(claude_record, 'ROWS', 1)
    for read in (claude.state, claude.inputs):
        with pytest.raises(record_file.RecordError, match='budget'):
            read(record.path)
    assert claude.compactions(record.path).reason is not None


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
        record.truncate(128 * 1024 * 1024 + 1)

    def unexpected(*_):
        pytest.fail('a past-bound record read body bytes')

    monkeypatch.setattr(claude_record, 'read_piece', unexpected)
    with pytest.raises(record_file.PastBound) as refusal:
        claude.state(path)
    assert refusal.value.bound == 128 * 1024 * 1024
    assert refusal.value.size == refusal.value.bound + 1


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
