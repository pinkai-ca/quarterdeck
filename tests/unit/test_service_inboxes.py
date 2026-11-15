"""A completed inbox reading is reused only while every input and its tail stand."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace
import time

import pytest

from quarterdeck import compaction, durable, post, service, service_inbox
from quarterdeck.ship import ShipError


def moved(path):
    """What a change to a folder moves: its identity, size and modification times. Never its access time,
    which a read moves, and which crosses a second on a slow runner (GitHub, 3.12, 314605e7)."""
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@pytest.fixture
def inboxes(tmp_path, monkeypatch):
    folder = tmp_path / 'ship'
    runtime = folder / 'runtime'
    runtime.mkdir(parents=True)
    (folder / 'ship.toml').write_text('[ship]\n')
    store = post.Store(runtime)
    svc = SimpleNamespace(runtime=runtime, store=store,
                          ship=SimpleNamespace(seats={'worker': None, 'issuer': None}),
                          log=lambda *a, **kw: None)
    order = store.new('order', 'issuer', 'worker', 'synthetic', due='literal due time')
    for name in svc.ship.seats:
        post.render_inbox(runtime, store, name)
    clock = SimpleNamespace(ns=time.time_ns() + 10_000_000_000, at=post.now())
    monkeypatch.setattr(service_inbox.time, 'time_ns', lambda: clock.ns)
    monkeypatch.setattr(post, 'now', lambda: clock.at)
    service.Service.inboxes(svc)
    assert svc.inbox_certificate is not None
    return svc, order, clock


def test_quiet_warm_pass_does_no_rendering_reading_or_writing(inboxes, monkeypatch):
    svc, _, _ = inboxes
    def unexpected(*a, **kw):
        pytest.fail('quiet inbox pass did work')
    monkeypatch.setattr(post, 'inbox_reading', unexpected)
    monkeypatch.setattr(post.post_dir, 'read_text', unexpected)
    monkeypatch.setattr(durable, 'replace_file', unexpected)
    service.Service.inboxes(svc)
    assert svc.inbox_certificate is not None


@pytest.mark.parametrize('target', [
    'post/store', 'post/imports', 'post/addressed', 'post/forwarded',
    'seats/worker/refusal.json', 'seats/worker/compaction.json',
    'ship.toml', 'membership', 'inbox-content', 'inbox-removed', 'inbox-folder',
])
def test_each_input_alone_rebuilds(inboxes, monkeypatch, target):
    svc, order, _ = inboxes
    previous_store = svc.store
    runtime = svc.runtime
    inbox = runtime / 'post/inbox/worker.md'
    if target == 'membership':
        svc.ship.seats['new-seat'] = None
    elif target == 'ship.toml':
        (runtime.parent / target).write_text('[ship]\n# changed\n')
    elif target == 'inbox-content':
        inbox.write_text('out of date\n')
    elif target == 'inbox-removed':
        inbox.unlink()
    elif target == 'inbox-folder':
        parent = inbox.parent
        parent.rename(parent.with_name('previous-inbox'))
        parent.mkdir()
        for name in svc.ship.seats:
            (parent / f'{name}.md').write_text((parent.with_name('previous-inbox') / f'{name}.md').read_text())
    elif target == 'post/store':
        # The closing report goes to ANOTHER seat; worker's order still changes.
        post.Store(runtime).new('report', 'worker', 'issuer', 'done', re=order.id)
    elif target.endswith('/refusal.json'):
        path = runtime / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"format": 1, "seat": "worker", "events": []}')
    elif target.endswith('/compaction.json'):
        compaction.save(runtime, 'worker', compaction.new())
    else:
        path = runtime / target
        path.mkdir(parents=True)
        # Immutable ancillary records are published into their own directories.
        (path / 'unused.json').write_text('{}')
    calls, original = [], post.inbox_reading
    def read(*args, **kwargs):
        calls.append(args[1])
        return original(*args, **kwargs)
    monkeypatch.setattr(post, 'inbox_reading', read)
    service.Service.inboxes(svc)
    assert 'worker' in calls
    if target == 'post/store':
        assert svc.store is previous_store, 'ordinary arrivals retain the incremental reader'
    elif target in ('ship.toml', 'membership', 'post/imports', 'post/addressed', 'post/forwarded',
                    'seats/worker/refusal.json', 'seats/worker/compaction.json'):
        assert svc.store is not previous_store, 'projection changes require a fresh reader'
    assert inbox.read_text() == post.inbox_text(svc.store, 'worker')
    if target == 'post/store':
        assert '## Open (0)' in inbox.read_text() and 'closed by rep-' in inbox.read_text()


def test_a_question_answer_to_another_seat_rebuilds(inboxes):
    svc, _, _ = inboxes
    question = svc.store.new('question', 'issuer', 'worker', 'which?')
    service.Service.inboxes(svc)
    service.Service.inboxes(svc)
    svc.store.new('answer', 'worker', 'issuer', 'this one', re=question.id)
    service.Service.inboxes(svc)
    assert '## Open (1)' in (svc.runtime / 'post/inbox/worker.md').read_text()


@pytest.mark.parametrize('file', ['refusal.json', 'compaction.json'])
def test_existing_mutable_journal_edit_without_directory_change_rebuilds(inboxes, monkeypatch, file):
    svc, _, _ = inboxes
    path = svc.runtime / 'seats/worker' / file
    path.parent.mkdir(parents=True, exist_ok=True)
    value = ({'format': 1, 'seat': 'worker', 'events': []} if file == 'refusal.json'
             else compaction.new())
    path.write_text(json.dumps(value))
    service.Service.inboxes(svc)
    service.Service.inboxes(svc)
    assert svc.inbox_certificate is not None
    previous_store = svc.store
    directory = moved(path.parent)
    with path.open('a') as stream:
        stream.write(' ')
    assert moved(path.parent) == directory
    calls, original = [], post.inbox_reading
    def read(*args, **kwargs):
        calls.append(args[1])
        return original(*args, **kwargs)
    monkeypatch.setattr(post, 'inbox_reading', read)
    service.Service.inboxes(svc)
    assert calls == ['worker', 'issuer']
    assert svc.store is not previous_store


def test_import_and_addressed_publication_change_inbox_without_store_change(inboxes):
    svc, order, _ = inboxes
    runtime = svc.runtime
    original = moved(svc.store.dir)
    digest = hashlib.sha256((svc.store.dir / f'{order.id}.json').read_bytes()).hexdigest()
    imported = runtime / 'post/imports' / f'{order.id}.json'
    imported.parent.mkdir()
    imported.write_text(json.dumps(dict(origin='ship', context=True, sha256=digest)))
    service.Service.inboxes(svc)
    inbox = runtime / 'post/inbox/worker.md'
    assert '## Open (0)' in inbox.read_text()
    service.Service.inboxes(svc)
    assert svc.inbox_certificate is not None
    addressed = runtime / 'post/addressed' / f'{order.id}.json'
    addressed.parent.mkdir()
    addressed.write_text(json.dumps(dict(format=1, id=order.id, sha256=digest)))
    service.Service.inboxes(svc)
    assert '## Open (1)' in inbox.read_text()
    assert moved(svc.store.dir) == original


@pytest.mark.parametrize('offset', [-1, 0, 1])
def test_recent_timestamps_never_become_a_certificate_by_aging(inboxes, offset):
    svc, _, clock = inboxes
    newest = max(service_inbox.inputs(svc)[0][1][1], service_inbox.inputs(svc)[1][1])
    clock.ns = newest + post.POST_TIMESTAMP_TICK_NS + offset
    service.Service.inboxes(svc)
    assert (svc.inbox_certificate is not None) == (offset > 0)
    if offset <= 0:
        clock.ns = newest + post.POST_TIMESTAMP_TICK_NS + 1
        service.Service.inboxes(svc)
        assert svc.inbox_certificate is not None


def test_failed_pass_and_restart_have_no_certificate(inboxes, monkeypatch):
    svc, _, _ = inboxes
    real = post.inbox_reading
    svc.inbox_certificate = None
    calls = []
    def fail(*args, **kwargs):
        calls.append(args[1])
        raise ShipError('synthetic failed read')
    monkeypatch.setattr(post, 'inbox_reading', fail)
    service.Service.inboxes(svc)
    assert svc.inbox_certificate is None and calls == ['worker', 'issuer']
    monkeypatch.setattr(post, 'inbox_reading', real)
    service.Service.inboxes(svc)
    assert svc.inbox_certificate is not None
    restarted = SimpleNamespace(**{k: v for k, v in vars(svc).items() if k != 'inbox_certificate'})
    monkeypatch.setattr(post, 'inbox_reading', fail)
    service.Service.inboxes(restarted)
    assert restarted.inbox_certificate is None and len(calls) == 4


def test_change_during_render_remains_owed(inboxes, monkeypatch):
    svc, _, _ = inboxes
    svc.inbox_certificate = None
    original = post.inbox_reading
    def change(*args, **kwargs):
        data = original(*args, **kwargs)
        if args[1] == 'worker':
            svc.store.new('order', 'issuer', 'worker', 'arrived during rendering')
        return data
    monkeypatch.setattr(post, 'inbox_reading', change)
    service.Service.inboxes(svc)
    assert svc.inbox_certificate is None
    monkeypatch.setattr(post, 'inbox_reading', original)
    service.Service.inboxes(svc)
    assert 'arrived during rendering' in (svc.runtime / 'post/inbox/worker.md').read_text()


def test_received_tail_expires_at_its_exact_boundary_and_clock_reversal_rebuilds(inboxes, monkeypatch):
    svc, _, clock = inboxes
    at = datetime(2032, 1, 1, tzinfo=timezone.utc)
    note = post.Message('rep-clock', 'report', 'issuer', 'worker', at, 'tail entry')
    svc.store.write(note)
    clock.at = at + post.INBOX_TAIL_AGE - timedelta(seconds=1)
    service.Service.inboxes(svc)
    service.Service.inboxes(svc)
    path = svc.runtime / 'post/inbox/worker.md'
    assert 'tail entry' in path.read_text() and svc.inbox_certificate is not None
    clock.at = at + post.INBOX_TAIL_AGE
    service.Service.inboxes(svc)
    assert 'tail entry' in path.read_text()
    clock.at += timedelta(microseconds=1)
    service.Service.inboxes(svc)
    assert 'tail entry' not in path.read_text()
    service.Service.inboxes(svc)
    clock.at = at + post.INBOX_TAIL_AGE - timedelta(seconds=1)
    service.Service.inboxes(svc)
    assert 'tail entry' in path.read_text()


def test_unavailable_fingerprint_keeps_reading_every_pass(inboxes, monkeypatch):
    svc, _, _ = inboxes
    monkeypatch.setattr(service_inbox, 'inputs', lambda _: (_ for _ in ()).throw(OSError('synthetic')))
    for _ in range(2):
        service.Service.inboxes(svc)
        assert svc.inbox_certificate is None
