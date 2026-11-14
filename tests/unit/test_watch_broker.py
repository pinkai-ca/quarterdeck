"""The service's side of a jailed watch: bounded on every lock a jail can see, and closing only what it offered."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

from quarterdeck import post, ship_locks, watch_broker


def service(runtime: Path):
    logged = []
    return SimpleNamespace(runtime=runtime, store=post.Store(runtime), attempts=post.Attempts(runtime),
                           log=lambda event, **data: logged.append((event, data)), logged=logged)


def armed(runtime: Path, seat: str) -> tuple[str, int]:
    """A brokered watch as the jailed wait leaves it: registration, generation, request, and the lease held."""
    folder = post.watch_file(runtime, seat).parent
    folder.mkdir(parents=True, exist_ok=True)
    generation = 'a' * 32
    (folder / 'watch-generation').write_text(generation + '\n')
    (folder / 'watch').write_text(f'7 {post.now().isoformat()}\n')  # the jail's own pid: meaningless outside
    lease = os.open(folder / watch_broker.LEASE, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    info = os.fstat(lease)
    (folder / watch_broker.REQUEST).write_text(json.dumps(dict(format=1, generation=generation,
                                                               lease=[info.st_dev, info.st_ino])))
    return generation, lease


def test_the_lease_not_the_jails_pid_says_the_watch_is_armed(tmp_path: Path):
    runtime = tmp_path / 'runtime'
    generation, lease = armed(runtime, 'desk')
    assert post.watch_reading(runtime, 'desk').armed is True
    os.close(lease)
    assert post.watch_reading(runtime, 'desk').armed is False, 'the lease went with the wait'


def test_a_held_admission_lock_never_stalls_the_service(tmp_path: Path):
    runtime = tmp_path / 'runtime'
    svc = service(runtime)
    armed(runtime, 'desk')
    svc.store.new('order', 'captain', 'desk', 'waiting')
    path = ship_locks.path(runtime, 'admission', hashlib.sha256(b'desk').hexdigest() + '.lock')
    ship_locks.prepare(path)
    path.write_text('')
    held = os.open(path, os.O_RDONLY)  # a read-only descriptor takes the exclusive lock
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        started = time.monotonic()
        watch_broker.run(svc, SimpleNamespace(name='desk', ship=SimpleNamespace(runtime=runtime)))
        assert time.monotonic() - started < watch_broker.WAIT + 1, 'bounded'
        assert [event for event, _ in svc.logged] == ['watch-broker-skipped']
        assert not watch_broker.offer_path(runtime, 'desk').exists(), 'nothing offered while skipped'
    finally:
        os.close(held)
    watch_broker.run(svc, SimpleNamespace(name='desk', ship=SimpleNamespace(runtime=runtime)))
    assert watch_broker.read_offer(runtime, 'desk') is not None, 'the next pass offers'


def test_a_held_attempts_lock_skips_the_seat_without_an_attempt(tmp_path: Path):
    runtime = tmp_path / 'runtime'
    svc = service(runtime)
    armed(runtime, 'desk')
    message = svc.store.new('order', 'captain', 'desk', 'waiting')
    lock = ship_locks.path(runtime, 'attempts.lock')
    ship_locks.prepare(lock)
    lock.write_text('')
    held = os.open(lock, os.O_RDONLY)
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        started = time.monotonic()
        watch_broker.run(svc, SimpleNamespace(name='desk', ship=SimpleNamespace(runtime=runtime)))
        assert time.monotonic() - started < watch_broker.WAIT + 1
        assert svc.logged[-1][0] == 'watch-broker-skipped'
    finally:
        os.close(held)
    assert svc.attempts.for_message(message.id) == [], 'no attempt was opened'


def test_only_an_offered_open_attempt_of_this_seat_is_closed_by_a_receipt(tmp_path: Path):
    runtime = tmp_path / 'runtime'
    svc = service(runtime)
    armed(runtime, 'desk')
    message = svc.store.new('order', 'captain', 'desk', 'waiting')
    watch_broker.run(svc, SimpleNamespace(name='desk', ship=SimpleNamespace(runtime=runtime)))
    offer = watch_broker.read_offer(runtime, 'desk')
    assert offer['messages'] == [message.id] and len(offer['attempts']) == 1
    receipts = post.watch_file(runtime, 'desk').parent / watch_broker.RECEIPTS
    receipts.mkdir()
    (receipts / 'not-offered.json').write_text(json.dumps(dict(format=1, attempt='not-offered.json')))
    (receipts / 'linked.json').symlink_to(tmp_path / 'elsewhere.json')
    name = offer['attempts'][0]
    (receipts / name).write_text(json.dumps(dict(format=1, attempt=name, message=message.id)))
    watch_broker.run(svc, SimpleNamespace(name='desk', ship=SimpleNamespace(runtime=runtime)))
    attempt, = svc.attempts.for_message(message.id)
    assert (attempt.result, attempt.detail) == ('watch', 'printed by qd inbox --wait')
    assert ('watch-receipt-ignored', dict(seat='desk', receipt='not-offered.json')) in svc.logged
    assert (receipts / 'linked.json').is_symlink(), 'a link is never read and is left where it is'
    watch_broker.run(svc, SimpleNamespace(name='desk', ship=SimpleNamespace(runtime=runtime)))
    assert watch_broker.read_offer(runtime, 'desk') is None, 'a settled offer is retired'


def test_a_held_lease_is_only_tested_and_never_delays_a_pass(tmp_path: Path):
    """The wait holds its lease for its whole life; the service only tests it, without blocking, for every seat."""
    runtime = tmp_path / 'runtime'
    svc = service(runtime)
    _, lease = armed(runtime, 'desk')  # held, exclusively, as a live wait holds it
    _, other = armed(runtime, 'office')
    try:
        svc.store.new('order', 'captain', 'office', 'for the other seat')
        started = time.monotonic()
        for _ in range(20):
            assert watch_broker.lease_held(runtime, 'desk', 'a' * 32) is True
            watch_broker.run(svc, SimpleNamespace(name='desk', ship=SimpleNamespace(runtime=runtime)))
            watch_broker.run(svc, SimpleNamespace(name='office', ship=SimpleNamespace(runtime=runtime)))
        assert time.monotonic() - started < 2, 'forty passes over held leases, none waiting on them'
        assert watch_broker.read_offer(runtime, 'office') is not None
    finally:
        os.close(lease)
        os.close(other)


def test_a_receipts_folder_that_is_a_link_is_never_entered(tmp_path: Path):
    runtime = tmp_path / 'runtime'
    svc = service(runtime)
    armed(runtime, 'desk')
    svc.store.new('order', 'captain', 'desk', 'waiting')
    watch_broker.run(svc, SimpleNamespace(name='desk', ship=SimpleNamespace(runtime=runtime)))
    name = watch_broker.read_offer(runtime, 'desk')['attempts'][0]
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / name).write_text(json.dumps(dict(format=1, attempt=name)))
    (outside / 'precious.json').write_text('keep me')
    (post.watch_file(runtime, 'desk').parent / watch_broker.RECEIPTS).symlink_to(outside)
    watch_broker.run(svc, SimpleNamespace(name='desk', ship=SimpleNamespace(runtime=runtime)))
    assert sorted(p.name for p in outside.iterdir()) == sorted([name, 'precious.json']), 'nothing outside is removed'
    attempt, = svc.attempts.for_message(watch_broker.read_offer(runtime, 'desk')['messages'][0])
    assert attempt.result is None, 'a claim reached through a link closes nothing'


def test_an_oversized_or_linked_request_is_no_claim(tmp_path: Path):
    runtime = tmp_path / 'runtime'
    armed(runtime, 'desk')
    request = post.watch_file(runtime, 'desk').parent / watch_broker.REQUEST
    request.write_text(' ' * (watch_broker.LIMIT + 1))
    assert watch_broker.request(runtime, 'desk') is None
    request.unlink()
    elsewhere = tmp_path / 'elsewhere.json'
    elsewhere.write_text(json.dumps(dict(format=1, generation='a' * 32, lease=[0, 0])))
    request.symlink_to(elsewhere)
    assert watch_broker.request(runtime, 'desk') is None


def test_a_human_seat_without_a_brokered_watch_is_not_touched(tmp_path: Path):
    """No request, no offer, no receipt: the step returns before any lock or log, so open seats are as before."""
    runtime = tmp_path / 'runtime'
    svc = service(runtime)
    svc.store.new('order', 'captain', 'desk', 'waiting')
    watch_broker.run(svc, SimpleNamespace(name='desk', ship=SimpleNamespace(runtime=runtime)))
    assert svc.logged == [] and not watch_broker.offer_path(runtime, 'desk').exists()
    assert svc.attempts.for_message(svc.store.all()[0].id) == []
