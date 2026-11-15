"""Ordinary claim ownership and stop/start outcomes, with no seat programs."""

import json
from contextlib import contextmanager
import os
from pathlib import Path
import selectors
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from quarterdeck import post, request_queue, servicectl
from quarterdeck.service import Service
from quarterdeck.ship import ShipError


def enqueue(runtime, name='old'):
    return request_queue.enqueue(runtime, 'relaunch', dict(seat='orchid', resume=True), name=name)


def service(runtime, answer):
    runner = Service.__new__(Service)
    runner.runtime = runtime
    runner.log_path = runtime / 'log' / 'service.jsonl'
    runner.quick_request_scan = runner.quick_request_retry = None
    runner.stale_claims_pending = True
    runner.copy_identity = runner.copy_start_token = None
    runner.heartbeat_lock = threading.Lock()
    runner.watch_credentials = lambda stop: None
    runner.ship = SimpleNamespace(tick=0.01)
    runner.answer = answer
    return runner


class EndPass(Exception):
    pass


def start_once(runner, tick):
    def one_pass():
        tick()
        raise EndPass
    runner.tick = one_pass
    try:
        with pytest.raises(EndPass):
            runner.run()
    finally:
        if hasattr(runner, 'lock'):
            runner.lock.close()


def make_claim(path):
    raw = path.read_text()
    request_queue.claim(path, raw, request_queue.metadata(path))
    assert path.with_suffix('.claimed').read_text() == raw


def mark_previous_start(path):
    """A real current PID, but an earlier process start: the PID was reused."""
    make_claim(path)
    meta = request_queue.metadata(path)
    meta['claim_owner']['start'] = str(int(servicectl.process_start(os.getpid())) + 1)
    request_queue.save_metadata(path, meta)


def snapshot(path):
    return {p: p.read_bytes() for p in (path, path.with_suffix('.claimed'), request_queue.state_path(path))}


@pytest.mark.parametrize('legacy', [False, True])
def test_sync_stop_between_claim_and_apply_settles_at_next_start(tmp_path, legacy):
    runtime = tmp_path / 'runtime'
    path = enqueue(runtime)
    # Use the real service loop and the same stop primitive as sync. The only
    # stand-in is its dispatcher, paused immediately after the durable claim.
    child = subprocess.Popen([sys.executable, '-c', '''
import signal, sys, threading
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
from quarterdeck.service import Service
from quarterdeck import durable, request_queue, servicectl
if sys.argv[3] == 'legacy':
    # The old service copied the payload without claimer metadata.
    request_queue.claim = lambda path, raw, meta: durable.replace_file(path.with_suffix('.claimed'), raw)
runner = Service.__new__(Service)
runner.runtime = Path(sys.argv[2])
runner.log_path = runner.runtime / 'log' / 'service.jsonl'
runner.quick_request_scan = runner.quick_request_retry = None
runner.stale_claims_pending = True
runner.copy_identity = runner.copy_start_token = None
runner.heartbeat_lock = threading.Lock()
runner.watch_credentials = lambda stop: None
runner.ship = SimpleNamespace(tick=0.01)
def before_apply(data):
    print('claimed-before-apply', flush=True)
    signal.pause()
    raise AssertionError('the stopped request must never apply')
runner.answer = before_apply
runner.tick = runner.requests
servicectl.write_heartbeat(runner.runtime)
runner.run()
''', str(Path(request_queue.__file__).parents[1]), str(runtime), 'legacy' if legacy else 'current'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        with selectors.DefaultSelector() as ready:
            ready.register(child.stdout, selectors.EVENT_READ)
            assert ready.select(10), 'service did not reach its claim boundary'
        assert child.stdout.readline().strip() == 'claimed-before-apply'
        owner = request_queue.metadata(path).get('claim_owner')
        if legacy:
            assert owner is None
            # Cross the framework's conservative supported filesystem tick.
            time.sleep(post.POST_TIMESTAMP_TICK_NS / 1_000_000_000 + 0.05)
        else:
            assert owner['pid'] == child.pid and owner['start'] == servicectl.process_start(child.pid)
        assert not path.with_suffix('.done').exists()
        assert 'service stopped' in servicectl.stop(runtime)
        child.communicate(timeout=5)
        assert child.returncode == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)

    applied = []
    runner = service(runtime, lambda data: applied.append(data) or 'new request applied')

    def first_tick():
        # Startup, before even the first tick, settles the old request.
        assert path.with_suffix('.done').read_text().strip() == request_queue.RESTART_SETTLEMENT
        assert not path.exists() and not path.with_suffix('.claimed').exists()
        assert not request_queue.state_path(path).exists()
        assert applied == []
        new = enqueue(runtime, 'new')
        runner.requests()
        runner.requests()
        assert new.with_suffix('.done').read_text().strip() == 'new request applied'
        assert len(applied) == 1

    start_once(runner, first_tick)
    events = [json.loads(line) for line in runner.log_path.read_text().splitlines()]
    [settled] = [event for event in events if event['event'] == 'request-settled']
    assert settled['request'] == path.stem and settled['claimer'] == owner
    assert settled['answer'] == request_queue.RESTART_SETTLEMENT
    if legacy:
        assert settled['before_service_start']['margin_ns'] == post.POST_TIMESTAMP_TICK_NS


def test_running_claimer_is_untouched_at_startup(tmp_path):
    path = enqueue(tmp_path)
    make_claim(path)
    before = snapshot(path)
    runner = service(tmp_path, lambda data: pytest.fail('a live claim was replayed'))
    start_once(runner, runner.requests)
    assert snapshot(path) == before
    assert not path.with_suffix('.done').exists()


def test_reused_pid_is_not_the_claiming_process(tmp_path):
    path = enqueue(tmp_path)
    mark_previous_start(path)
    assert request_queue.stale_claim(path)
    runner = service(tmp_path, lambda data: pytest.fail('an old claim was replayed'))
    start_once(runner, runner.requests)
    assert path.with_suffix('.done').read_text().strip() == request_queue.RESTART_SETTLEMENT
    assert not path.exists()


@pytest.mark.parametrize('unknown', ['legacy', 'missing-start', 'unreadable-process'])
def test_unproved_claimer_stays_held(tmp_path, monkeypatch, unknown):
    path = enqueue(tmp_path)
    make_claim(path)
    meta = request_queue.metadata(path)
    if unknown == 'legacy':
        del meta['claim_owner']
    elif unknown == 'missing-start':
        meta['claim_owner']['start'] = None
    else:
        read = Path.read_text
        def unreadable(p, *args, **kwargs):
            if p == Path(f'/proc/{os.getpid()}/stat'):
                raise PermissionError('process identity temporarily unreadable')
            return read(p, *args, **kwargs)
        monkeypatch.setattr(Path, 'read_text', unreadable)
    request_queue.save_metadata(path, meta)
    before = snapshot(path)
    runner = service(tmp_path, lambda data: pytest.fail('an unproved claim was replayed'))
    runner.settle_stale_claims()
    runner.requests()
    assert snapshot(path) == before
    assert not path.with_suffix('.done').exists()


@pytest.mark.parametrize('stale', [False, True])
def test_cancel_requires_the_claiming_process_to_be_proved_gone(tmp_path, stale):
    path = enqueue(tmp_path)
    if stale:
        mark_previous_start(path)
        assert 'cancelled' in request_queue.cancel(tmp_path, 'orchid', path.stem)
        assert 'cancelled; never applied by this request' in path.with_suffix('.done').read_text()
        assert not path.exists() and not path.with_suffix('.claimed').exists()
    else:
        make_claim(path)
        before = snapshot(path)
        with pytest.raises(ShipError, match='already claimed; outcome unknown'):
            request_queue.cancel(tmp_path, 'orchid', path.stem)
        assert snapshot(path) == before


def test_interrupted_settlement_intent_is_completed_without_dispatch(tmp_path, monkeypatch):
    path = enqueue(tmp_path)
    mark_previous_start(path)
    runner = service(tmp_path, lambda data: pytest.fail('a terminal claim was replayed'))
    settle = request_queue.settle
    def unavailable(*args, **kwargs):
        raise OSError('answer storage temporarily unavailable')
    monkeypatch.setattr(request_queue, 'settle', unavailable)
    runner.settle_stale_claims()
    assert request_queue.metadata(path)['terminal'] == request_queue.RESTART_SETTLEMENT
    monkeypatch.setattr(request_queue, 'settle', settle)
    runner.requests()
    assert path.with_suffix('.done').read_text().strip() == request_queue.RESTART_SETTLEMENT
    assert not path.exists()


def test_busy_startup_settlement_defers_dispatch_until_coordination_is_available(tmp_path, monkeypatch):
    old = enqueue(tmp_path)
    mark_previous_start(old)
    applied = []
    runner = service(tmp_path, lambda data: applied.append(data) or 'applied once')
    coordination = request_queue.coordination
    @contextmanager
    def busy(*args, **kwargs):
        assert kwargs == dict(wait=False)
        yield False
    monkeypatch.setattr(request_queue, 'coordination', busy)
    runner.settle_stale_claims()
    assert runner.stale_claims_pending
    new = enqueue(tmp_path, 'new')
    assert runner._requests([new]) is False
    assert not applied and old.exists() and new.exists()
    monkeypatch.setattr(request_queue, 'coordination', coordination)
    runner.requests()
    assert old.with_suffix('.done').read_text().strip() == request_queue.RESTART_SETTLEMENT
    assert new.with_suffix('.done').read_text().strip() == 'applied once'
    assert len(applied) == 1 and not runner.stale_claims_pending


def test_one_settlement_storage_failure_does_not_block_a_healthy_request(tmp_path, monkeypatch):
    old = enqueue(tmp_path)
    mark_previous_start(old)
    new = request_queue.enqueue(tmp_path, 'stop', dict(seat='iris'), name='healthy')
    settle = request_queue.settle
    def unavailable(path, *args, **kwargs):
        if path == old:
            raise OSError('old answer storage temporarily unavailable')
        return settle(path, *args, **kwargs)
    monkeypatch.setattr(request_queue, 'settle', unavailable)
    applied = []
    runner = service(tmp_path, lambda data: applied.append(data['seat']) or 'healthy request applied')
    # A full scan succeeded. One failed settlement is held for retry, rather
    # than turning it into a globally busy request owner.
    runner._requests([new])
    assert applied == ['iris']
    assert new.with_suffix('.done').read_text().strip() == 'healthy request applied'
    assert old.exists() and old.with_suffix('.claimed').exists()
    assert runner.stale_claims_pending


@pytest.mark.parametrize('offset', [-post.POST_TIMESTAMP_TICK_NS - 1, -post.POST_TIMESTAMP_TICK_NS,
                                  -post.POST_TIMESTAMP_TICK_NS + 1, -1, 0, 1])
def test_legacy_ctime_must_strictly_precede_service_start_by_more_than_a_tick(tmp_path, offset):
    path = enqueue(tmp_path)
    path.with_suffix('.claimed').write_bytes(path.read_bytes())
    info = path.with_suffix('.claimed').stat()
    runner = service(tmp_path, lambda data: pytest.fail('a legacy claim was replayed'))
    runner.claim_service_start = dict(ctime_ns=info.st_ctime_ns - offset, device=info.st_dev)
    before = {p: p.read_bytes() for p in (path, path.with_suffix('.claimed'))}
    runner.settle_stale_claims()
    runner.requests()
    if offset < -post.POST_TIMESTAMP_TICK_NS:
        assert not path.exists() and not path.with_suffix('.claimed').exists()
        assert path.with_suffix('.done').read_text().strip() == request_queue.RESTART_SETTLEMENT
    else:
        assert {p: p.read_bytes() for p in before} == before
        assert not path.with_suffix('.done').exists()


def test_legacy_time_proof_requires_the_same_filesystem(tmp_path):
    path = enqueue(tmp_path)
    path.with_suffix('.claimed').write_bytes(path.read_bytes())
    info = path.with_suffix('.claimed').stat()
    start = dict(ctime_ns=info.st_ctime_ns + 10 * post.POST_TIMESTAMP_TICK_NS,
                 device=info.st_dev + 1)
    assert not request_queue.stale_claim(path, service_start=start)


@pytest.mark.parametrize('invalid', [False, True])
def test_recorded_owner_takes_precedence_over_old_ctime(tmp_path, invalid):
    path = enqueue(tmp_path)
    make_claim(path)
    if invalid:
        meta = request_queue.metadata(path)
        meta['claim_owner']['start'] = None
        request_queue.save_metadata(path, meta)
    info = path.with_suffix('.claimed').stat()
    start = dict(ctime_ns=info.st_ctime_ns + 10 * post.POST_TIMESTAMP_TICK_NS,
                 device=info.st_dev)
    assert not request_queue.stale_claim(path, service_start=start)


def test_startup_settlement_of_an_absent_queue_is_read_only(tmp_path):
    runtime = tmp_path / 'not-created'
    runner = service(runtime, lambda data: pytest.fail('an absent request was dispatched'))
    assert runner.settle_stale_claims()
    assert not runner.stale_claims_pending
    assert not runtime.exists()
