"""Journal reuse must keep the next status collection current."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import pending, post, post_dir, presentation, watch
from quarterdeck.service import Service


def journal(runtime, filename='sample.json', *, phase='delivered', payload='sample'):
    stamp = datetime.now(timezone.utc).isoformat()
    receipt = runtime / 'post/attempts' / filename
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text('{}')
    path = runtime / 'post/pending/reader' / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    data = dict(seat='reader', phase=phase, payload=payload, shown=None, record=None,
                identity=dict(pane='%1', pid=123, launch=stamp, harness='claude'),
                attempts=[filename], at=stamp, since=stamp)
    path.write_text(json.dumps(data))
    return path, data


def collect(reader, runtime):
    seat = SimpleNamespace(name='reader', ship=SimpleNamespace(runtime=runtime))
    with pending.journal_reading(reader), post.reading_pass():
        return pending.journals(seat)


def count_reads(monkeypatch, folder):
    reads = []
    original = post_dir._read_nofollow

    def read(descriptor, name, path, *args, **kwargs):
        if Path(path).parent == folder:
            reads.append(name)
        return original(descriptor, name, path, *args, **kwargs)

    monkeypatch.setattr(post_dir, '_read_nofollow', read)
    return reads


def test_unchanged_journals_are_parsed_once_and_return_independent_proofs(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    path, data = journal(runtime)
    started = max(path.stat().st_mtime_ns, path.stat().st_ctime_ns) + post.POST_TIMESTAMP_TICK_NS + 1
    monkeypatch.setattr(pending, 'time', SimpleNamespace(time_ns=lambda: started))
    reader = pending.JournalReader()
    reads = count_reads(monkeypatch, path.parent)
    first, errors = collect(reader, runtime)
    assert not errors
    first[0].data['attempts'].append('caller-edit')
    first.clear()
    for _ in range(3):
        found, errors = collect(reader, runtime)
        assert not errors and found[0].data == data
    assert reads == [path.name]


@pytest.mark.parametrize('age_ns', [0, post.POST_TIMESTAMP_TICK_NS])
def test_same_stamp_replacement_within_timestamp_tick_is_read_again(tmp_path, monkeypatch, age_ns):
    runtime = tmp_path / 'runtime'
    path, data = journal(runtime, payload='before')
    reader = pending.JournalReader()
    stamp = reader._stamp(path.stat())
    now = [max(stamp[-2:]) + age_ns]
    monkeypatch.setattr(pending, 'time', SimpleNamespace(time_ns=lambda: now[0]))
    # Model coarse timestamps and inode reuse: both complete files have one stamp.
    monkeypatch.setattr(reader, '_stamp', lambda info: stamp)
    reads = count_reads(monkeypatch, path.parent)
    assert collect(reader, runtime)[0][0].data['payload'] == 'before'
    data['payload'] = 'after!'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data))
    os.replace(temporary, path)
    # Merely waiting cannot certify the observation made within the tick.
    now[0] = max(stamp[-2:]) + post.POST_TIMESTAMP_TICK_NS + 1
    assert collect(reader, runtime)[0][0].data['payload'] == 'after!'
    assert collect(reader, runtime)[0][0].data['payload'] == 'after!'
    assert reads == [path.name, path.name]


def test_journals_outside_status_scope_reads_disk_after_a_cached_collection(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    path, data = journal(runtime, payload='before')
    started = max(path.stat().st_mtime_ns, path.stat().st_ctime_ns) + post.POST_TIMESTAMP_TICK_NS + 1
    monkeypatch.setattr(pending, 'time', SimpleNamespace(time_ns=lambda: started))
    reader = pending.JournalReader()
    reads = count_reads(monkeypatch, path.parent)
    assert collect(reader, runtime)[0][0].data['payload'] == 'before'
    assert collect(reader, runtime)[0][0].data['payload'] == 'before'
    assert reads == [path.name]
    data['payload'] = 'after!'
    path.write_text(json.dumps(data))
    seat = SimpleNamespace(name='reader', ship=SimpleNamespace(runtime=runtime))
    found, errors = pending.journals(seat)
    assert not errors and found[0].data['payload'] == 'after!'
    assert reads == [path.name, path.name]


@pytest.mark.parametrize('change', ['replace', 'in-place', 'same-size-restored-mtime'])
def test_changed_journal_is_read_on_the_next_collection(tmp_path, monkeypatch, change):
    runtime = tmp_path / 'runtime'
    path, data = journal(runtime, payload='before')
    reader = pending.JournalReader()
    reads = count_reads(monkeypatch, path.parent)
    assert collect(reader, runtime)[0][0].data['payload'] == 'before'
    before = path.stat()
    data['payload'] = 'after!'
    if change == 'replace':
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(data))
        os.replace(temporary, path)
    else:
        path.write_text(json.dumps(data))
        if change == 'same-size-restored-mtime':
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            assert path.stat().st_size == before.st_size
            assert path.stat().st_ctime_ns != before.st_ctime_ns
    assert collect(reader, runtime)[0][0].data['payload'] == 'after!'
    assert reads == [path.name, path.name]


def test_new_removed_and_recreated_journal_names_are_current(tmp_path):
    runtime = tmp_path / 'runtime'
    path, _ = journal(runtime)
    reader = pending.JournalReader()
    assert len(collect(reader, runtime)[0]) == 1
    another, _ = journal(runtime, 'another.json')
    assert {p.path for p in collect(reader, runtime)[0]} == {path, another}
    path.unlink()
    assert [p.path for p in collect(reader, runtime)[0]] == [another]
    another.unlink()
    path.parent.rmdir()
    assert collect(reader, runtime) == ([], [])
    journal(runtime, payload='recreated')
    assert collect(reader, runtime)[0][0].data['payload'] == 'recreated'


def test_present_bad_journal_and_missing_receipt_stay_visible(tmp_path):
    runtime = tmp_path / 'runtime'
    path, data = journal(runtime)
    reader = pending.JournalReader()
    assert not collect(reader, runtime)[1]
    receipt = runtime / 'post/attempts' / path.name
    receipt.unlink()
    assert collect(reader, runtime) == ([], [path.name])
    receipt.write_text('{}')
    assert not collect(reader, runtime)[1]
    path.write_text('{ malformed')
    assert collect(reader, runtime) == ([], [path.name])
    path.write_text(json.dumps(data))
    assert not collect(reader, runtime)[1]


def test_next_service_status_shows_and_clears_a_changed_journal_alarm(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    path, data = journal(runtime)
    identity = data['identity']
    seat = SimpleNamespace(name='reader', ship=SimpleNamespace(runtime=runtime),
                           session=SimpleNamespace(human=True),
                           launched=lambda: SimpleNamespace(at=identity['launch']),
                           pane=lambda: SimpleNamespace(id=identity['pane'], pid=identity['pid']),
                           harness=SimpleNamespace(name='claude'))
    svc = object.__new__(Service)
    svc.ship, svc.tmux, svc.status_framework = seat.ship, None, None
    svc.store, svc.attempts = post.Store(runtime), post.Attempts(runtime)
    lines, pending_details = [], []

    def publish(*args):
        with post.reading_pass():
            held = pending.held(seat, None)
            facts = watch.Facts('ok', 0, 0, 0, pending=[held[0]] if held else [])
            lines.append(facts.line())
            pending_details.append(facts.pending)

    monkeypatch.setattr(presentation, 'publish', publish)
    svc.publish_status()
    assert not pending_details[-1] and '1 pending' not in lines[-1]
    data['phase'] = 'prepared'
    path.write_text(json.dumps(data))
    svc.publish_status()
    assert 'paste/submit outcome uncertain' in pending_details[-1][0]
    assert pending_details[-1][0] in lines[-1]
    data['phase'] = 'delivered'
    path.write_text(json.dumps(data))
    svc.publish_status()
    assert not pending_details[-1] and '1 pending' not in lines[-1]
    path.write_text('{ malformed')
    svc.publish_status()
    assert 'pending delivery journal unreadable' in pending_details[-1][0]
    assert 'pending' in lines[-1]
    assert pending._JOURNAL_READER.get() is None


def test_readers_keep_separate_runtime_inventories(tmp_path):
    a, b = tmp_path / 'a/runtime', tmp_path / 'b/runtime'
    journal(a, payload='a')
    journal(b, payload='b')
    reader = pending.JournalReader()
    assert collect(reader, a)[0][0].data['payload'] == 'a'
    assert collect(reader, b)[0][0].data['payload'] == 'b'
    assert collect(reader, a)[0][0].data['payload'] == 'a'
