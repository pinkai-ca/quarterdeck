"""Exact held-restart reports become labelled history from the queue's evidence."""
from dataclasses import replace
from datetime import timedelta
import time
from types import SimpleNamespace

import pytest

from quarterdeck import post, post_notices, request_queue, post_threads, ship as shipmod
from quarterdeck.service import Service
from tests.unit.test_rebrief_notices_settle import AT, store, launch, reader


STEM = '120000-relaunch-123'


def held(store, monkeypatch, *, resume=True, own=True, refused=False):
    runtime = store.dir.parent.parent
    path = request_queue.enqueue(runtime, 'relaunch', {'seat': 'reader', 'resume': resume}, name=STEM)
    meta = dict(caller='reader' if own else 'operator', waiting=dict(
        dimension='tools-unknown', reason='tool state unknown', since=AT.isoformat()))
    monkeypatch.setattr(post, 'now', lambda: AT + timedelta(seconds=120))
    service = SimpleNamespace(runtime=runtime, store=store, request_path=path,
                              request_wait=('tools-unknown', 'tool state unknown'), log=lambda *a, **kw: None)
    Service.tell_held(service, dict(kind='relaunch', seat='reader', resume=resume), meta,
                      'refused: synthetic ended turn held' if refused else None)
    request_queue.save_metadata(path, meta)
    return path, store.get(meta['told'])


def service_for(store, proof='reader: restarted in synthetic pane'):
    service = object.__new__(Service)
    service.runtime = store.dir.parent.parent
    service.ship = shipmod.load(service.runtime.parent)
    service.store = store
    service.answer = lambda data: proof
    service.log = lambda *args, **kwargs: None
    return service


def finish(store, path, proof='reader: restarted in synthetic pane'):
    service = service_for(store, proof)
    assert Service._requests(service, (path,)) is True
    assert not path.exists()
    return service


@pytest.mark.parametrize('resume', [True, False])
@pytest.mark.parametrize('own', [True, False])
def test_waiting_report_uses_the_exact_producer_form(store, monkeypatch, resume, own):
    path, message = held(store, monkeypatch, resume=resume, own=own)
    assert post_notices.held_request(message) == path.stem
    assert message.id not in store.settled_notices('reader')
    assert post.Attempts(store.dir.parent.parent).undelivered(store, 'reader') == [message]


@pytest.mark.parametrize('resume', [True, False])
def test_ended_own_turn_report_uses_the_exact_producer_form(store, monkeypatch, resume):
    path, message = held(store, monkeypatch, resume=resume, refused=True)
    assert post_notices.held_request(message) == path.stem
    assert message.id not in store.settled_notices('reader')


@pytest.mark.parametrize('ending', ['applied', 'withdrawn', 'retired'])
def test_completed_request_notice_is_history_and_not_carried(store, monkeypatch, ending):
    path, message = held(store, monkeypatch)
    before = (store.dir / (message.id + '.json')).read_bytes()
    if ending == 'applied':
        finish(store, path)
    elif ending == 'withdrawn':
        request_queue.cancel(store.dir.parent.parent, 'reader', path.stem)
    else:
        request_queue.terminal(path, request_queue.load(path), 'reader: request retired without applying')
    if ending != 'applied':
        assert message.id not in store.settled_notices('reader')
        finish(store, path)
    receipt = request_queue.notice_records(store.dir.parent.parent, 'reader')[message.id]
    assert receipt['state'] == {'applied': 'applied', 'withdrawn': 'withdrawn', 'retired': 'retired'}[ending]
    settled = store.settled_notices('reader')
    assert settled[message.id] == 'held restart: settled by completion of its request'
    assert post.Attempts(store.dir.parent.parent).undelivered(store, 'reader') == []
    carriage = reader(store).carriage()
    assert not carriage.carried and not carriage.rows
    assert 'held restart: settled by completion of its request' in carriage.history
    data = post.inbox_reading(store, 'reader')
    assert message in data['received'] and message.id in data['settled']
    assert 'held restart: settled by completion of its request' in post.inbox_text(store, 'reader')
    thread = post_threads.render(post_threads.whole(store, message.id, 'reader'))
    assert 'settled by completion of its request · received history' in thread
    assert message.body in thread
    assert (store.dir / (message.id + '.json')).read_bytes() == before
    assert store.read_ids('reader') == set()


def test_later_launch_settles_a_held_request_notice(store, monkeypatch):
    path, message = held(store, monkeypatch)
    launch(store, message.at + timedelta(seconds=1))
    assert store.settled_notices('reader')[message.id] == 'held restart: settled by a later launch'
    assert request_queue.load(path)['kind'] == 'relaunch'


def test_unreadable_request_settles_nothing(store, monkeypatch):
    path, message = held(store, monkeypatch)
    path.write_text('{incomplete')
    assert not store.settled_notices('reader')
    assert post.Attempts(store.dir.parent.parent).undelivered(store, 'reader') == [message]


def test_absence_without_record_is_not_completion(store, monkeypatch):
    path, message = held(store, monkeypatch)
    path.unlink()
    request_queue.clear_metadata(path)
    assert request_queue.notice_state(store.dir.parent.parent, path.stem, 'reader', message.id) is None
    assert message.id not in store.settled_notices('reader')


@pytest.mark.parametrize('change', [
    lambda m: replace(m, body=m.body + '\n'),
    lambda m: replace(m, body=m.body + ' extra'),
    lambda m: replace(m, body=m.body.replace('give your own word', 'give a word')),
    lambda m: replace(m, body=m.body.replace('qd restart reader', 'qd restart somebody')),
    lambda m: replace(m, sender='somebody'),
    lambda m: replace(m, re='synthetic-root'),
    lambda m: replace(m, kind='answer'),
])
def test_near_form_remains_deliverable(store, monkeypatch, change):
    path, original = held(store, monkeypatch)
    message = replace(change(original), id='rep-synthetic-nearform')
    store.write(message)
    finish(store, path, 'reader: request ended')
    assert post_notices.held_request(message) is None
    assert message in post.Attempts(store.dir.parent.parent).undelivered(store, 'reader')


def aged_clock(monkeypatch, *paths):
    stamps = [time.time_ns()]
    for path in paths:
        info = path.stat()
        stamps.extend((info.st_mtime_ns, info.st_ctime_ns))
    now = max(stamps) + post.POST_TIMESTAMP_TICK_NS + 1_000_000_000
    monkeypatch.setattr(post_notices.time, 'time_ns', lambda: now)


def test_quiet_pass_reads_no_request_record_and_next_change_is_seen(store, monkeypatch):
    path, message = held(store, monkeypatch)
    aged_clock(monkeypatch)
    original = request_queue.notice_records
    calls = []

    def read(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(request_queue, 'notice_records', read)
    assert message.id not in store.settled_notices('reader')
    assert len(calls) == 1
    assert message.id not in store.settled_notices('reader')
    assert len(calls) == 1
    finish(store, path, 'reader: request ended')
    assert len(calls) == 2  # one service-writer registry read, separate from delivery
    # The newly written receipt must age before an unchanged reading can certify it.
    aged_clock(monkeypatch, request_queue.notice_path(store.dir.parent.parent, 'reader'))
    calls.clear()
    assert message.id in store.settled_notices('reader')
    assert len(calls) == 1
    assert message.id in store.settled_notices('reader')
    assert len(calls) == 1  # the quiet next reading uses the stamped receipt


def test_young_or_unreadable_receipt_is_not_cached(store, monkeypatch):
    path, message = held(store, monkeypatch)
    finish(store, path)
    original = request_queue.notice_records
    calls = []
    monkeypatch.setattr(request_queue, 'notice_records', lambda *args: calls.append(args) or original(*args))
    assert message.id in store.settled_notices('reader')
    assert message.id in store.settled_notices('reader')
    assert len(calls) == 2
    request_queue.notice_path(store.dir.parent.parent, 'reader').write_text('{incomplete')
    aged_clock(monkeypatch)
    assert message.id not in store.settled_notices('reader')
    assert message.id not in store.settled_notices('reader')
    assert len(calls) == 4


def test_service_down_intent_stays_unsettled_and_reader_grows_nothing(store, monkeypatch):
    path, message = held(store, monkeypatch)
    request_queue.cancel(store.dir.parent.parent, 'reader', path.stem)
    assert path.exists() and request_queue.metadata(path)['notice_terminal_state'] == 'withdrawn'
    before = {str(p.relative_to(path.parent.parent)): p.read_bytes()
              for p in path.parent.parent.rglob('*') if p.is_file()}
    for _ in range(3):
        assert message.id not in store.settled_notices('reader')
    after = {str(p.relative_to(path.parent.parent)): p.read_bytes()
             for p in path.parent.parent.rglob('*') if p.is_file()}
    assert before == after
    assert not request_queue.notice_path(store.dir.parent.parent, 'reader').exists()


def test_acknowledgment_preserves_receipt_then_later_launch_prunes_it(store, monkeypatch):
    path, message = held(store, monkeypatch)
    service = finish(store, path)
    receipt = request_queue.notice_records(service.runtime, 'reader')[message.id]
    assert receipt['request'] == path.stem and receipt['seat'] == 'reader' and receipt['notice'] == message.id
    assert receipt['state'] == 'applied' and len(receipt['stamp']) == 6
    path.with_suffix('.done').unlink()
    request_queue.acknowledged(path)
    assert message.id in store.settled_notices('reader')
    launch(store, message.at + timedelta(seconds=1))
    Service.prune_request_notices(service)
    assert not request_queue.notice_path(service.runtime, 'reader').exists()
    assert store.settled_notices('reader')[message.id] == 'held restart: settled by a later launch'


def test_request_without_told_does_not_acquire_a_receipt(store, monkeypatch):
    runtime = store.dir.parent.parent
    path = request_queue.enqueue(runtime, 'relaunch', {'seat': 'reader', 'resume': True}, name=STEM)
    finish(store, path)
    assert not request_queue.notice_path(runtime, 'reader').exists()
