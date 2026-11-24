"""The service's side of a jailed watch: bounded on every lock a jail can see, and closing only what it offered."""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import inbox_watch, post, post_retirement, ship_locks, watch_broker


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


@pytest.mark.parametrize('fault', [None, 'missing-receipt-print', 'wrong-message', 'wrong-batch', 'wrong-generation'])
def test_the_broker_preserves_a_flushed_print_proof_only_for_its_exact_offer(tmp_path: Path, fault):
    runtime = tmp_path / 'runtime'
    svc = service(runtime)
    generation, lease = armed(runtime, 'desk')
    seat = SimpleNamespace(name='desk', ship=SimpleNamespace(runtime=runtime))
    try:
        for body in ('first synthetic post', 'second synthetic post'):
            svc.store.new('order', 'captain', seat.name, body)
        watch_broker.run(svc, seat)
        opened = svc.attempts.all()
        assert len(opened) == 2 and all(a.result is None for a in opened)
        proof = inbox_watch.print_proof(runtime, opened, 'synthetic printed batch\n', io.StringIO())
        watch_broker.receipt(runtime, opened, proof=proof)
        receipt = post.watch_file(runtime, seat.name).parent / watch_broker.RECEIPTS / opened[0].path.name
        data = json.loads(receipt.read_text())
        if fault == 'missing-receipt-print':
            data.pop('print')
        elif fault == 'wrong-message':
            data['message'] = opened[1].message
        elif fault == 'wrong-batch':
            data['print']['batch'] = inbox_watch.batch_digest([opened[0].path.name])
        elif fault == 'wrong-generation':
            data['print']['generation'] = 'f' * 32
        receipt.write_text(json.dumps(data))
        watch_broker.run(svc, seat)
        accepted = {a.path.name: a for a in svc.attempts.all()}
        assert [a.result for a in accepted.values()] == ['watch'] * 2
        assert inbox_watch.accepted_batch(runtime, generation, accepted) == (
            ([], None) if fault else (list(accepted.values()), proof))
    finally:
        os.close(lease)


ROLLBACK_BASE = '5b999ce8236f01ae9c04f854204c46c4719564b3'
OLDER_WATCH_READERS = '''
import sys
from pathlib import Path
from quarterdeck import post, watch_broker

runtime, phase = Path(sys.argv[1]), sys.argv[2]
assert Path(post.__file__).is_relative_to(Path(sys.argv[3]))
service = watch_broker.CheckHost(runtime)
brokered, direct = (service.attempts.for_message(identifier)[0] for identifier in sys.argv[4:6])
assert direct.result == 'watch'
if phase == 'offered':
    assert brokered.result is None
    offer = watch_broker.read_offer(runtime, brokered.seat)
    assert watch_broker._attempt(runtime, brokered.path.name) == brokered
    watch_broker._settle(service, brokered.seat, offer)
    brokered, = service.attempts.for_message(brokered.message)
    assert brokered.result == 'watch'
    assert not (watch_broker.seat_folder(runtime, brokered.seat) / watch_broker.RECEIPTS / brokered.path.name).exists()
else:
    for attempt in (brokered, direct):
        assert attempt.path.parent == runtime / 'post' / 'retired' / 'attempts'
        assert watch_broker._attempt(runtime, attempt.path.name) == attempt
assert service.attempts.latest() == {direct.message: 'watch', brokered.message: 'watch'}
'''


def test_new_watch_attempts_and_receipts_can_be_read_and_settled_after_rollback(tmp_path):
    # Archive the actual preceding package, rather than restating its schema in a test double.
    older = tmp_path / 'older'
    source = subprocess.check_output(['git', 'archive', ROLLBACK_BASE, 'src/quarterdeck'],
                                     cwd=Path(__file__).parents[2])
    with tarfile.open(fileobj=io.BytesIO(source)) as archive:
        archive.extractall(older, filter='data')
    older_source = older / 'src'
    runtime = tmp_path / 'runtime'
    svc = service(runtime)
    brokered = svc.store.new('order', 'captain', 'reader', 'synthetic brokered rollback post')
    direct = svc.store.new('order', 'captain', 'direct', 'synthetic direct rollback post')
    generation, lease = armed(runtime, brokered.to)
    try:
        watch_broker.run(svc, SimpleNamespace(name=brokered.to, ship=SimpleNamespace(runtime=runtime)))
        opened = svc.attempts.for_message(brokered.id)
        proof = inbox_watch.print_proof(runtime, opened, 'synthetic broker output\n', io.StringIO())
        assert proof['generation'] == generation
        watch_broker.receipt(runtime, opened, proof=proof)
    finally:
        os.close(lease)
    messages, opened = post.wait_batch(runtime, direct.to, stop_after=0)
    proof = inbox_watch.print_proof(runtime, opened, post.typed(messages), io.StringIO())
    post.printed(runtime, opened, proof=proof)

    env = {**os.environ, 'PYTHONPATH': str(older_source), 'PYTHONDONTWRITEBYTECODE': '1'}

    def old(phase):
        result = subprocess.run([sys.executable, '-c', OLDER_WATCH_READERS, str(runtime), phase,
                                 str(older_source), brokered.id, direct.id], cwd=older, env=env,
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr + result.stdout

    old('offered')
    for attempt in svc.attempts.all():
        assert attempt.result == 'watch'
        post_retirement._batch(svc, attempt.seat, {attempt.path.name: attempt}, [], lambda: None,
                               dict(attempts=0, journals=0), {})
        assert not attempt.path.exists()
    old('retired')


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


def test_a_receipted_offer_is_delivered_even_when_the_next_pass_starts_past_the_confirm_window(tmp_path: Path, monkeypatch):
    """The attempt opens in one pass and is receipted before the next, which begins more than five seconds later:
    reconcile, which runs first in a pass, must not judge it blind; the broker then closes it as a watch delivery."""
    import shutil

    from quarterdeck import door, service as servicemod, ship as shipmod
    from quarterdeck.tmux import Tmux

    folder = tmp_path / 'ships' / 'drydock'
    shutil.copytree(Path(__file__).resolve().parents[2] / 'ships' / 'drydock', folder)
    ship = shipmod.load(folder)
    svc = servicemod.Service(ship, Tmux(socket=str(tmp_path / 'private.sock')))
    armed(ship.runtime, 'desk')
    message = svc.store.new('order', 'captain', 'desk', 'for the jailed desk')
    svc.jailed_watches()  # pass N: the offer and its open attempt
    offer = watch_broker.read_offer(ship.runtime, 'desk')
    name, = offer['attempts']
    receipts = post.watch_file(ship.runtime, 'desk').parent / watch_broker.RECEIPTS
    receipts.mkdir()
    (receipts / name).write_text(json.dumps(dict(format=1, attempt=name, message=message.id)))  # the wait printed
    later = time.time() + door.CONFIRM_SECONDS + 5
    monkeypatch.setattr(servicemod.time, 'time', lambda: later)  # pass N+1 starts past the confirm window
    svc.reconcile()
    svc.jailed_watches()
    attempt, = svc.attempts.for_message(message.id)
    assert (attempt.result, attempt.detail) == ('watch', 'printed by qd inbox --wait'), attempt


def test_an_offered_attempt_never_receipted_is_still_judged_blind_after_its_bound(tmp_path: Path, monkeypatch):
    """Silence still shows: past the offer's bound an open offered attempt is judged blind as any watch's is."""
    import shutil

    from quarterdeck import service as servicemod, ship as shipmod
    from quarterdeck.tmux import Tmux

    folder = tmp_path / 'ships' / 'drydock'
    shutil.copytree(Path(__file__).resolve().parents[2] / 'ships' / 'drydock', folder)
    ship = shipmod.load(folder)
    svc = servicemod.Service(ship, Tmux(socket=str(tmp_path / 'private.sock')))
    armed(ship.runtime, 'desk')
    message = svc.store.new('order', 'captain', 'desk', 'never printed')
    svc.jailed_watches()
    later = time.time() + watch_broker.OFFER_SECONDS + 5
    monkeypatch.setattr(servicemod.time, 'time', lambda: later)
    svc.reconcile()
    attempt, = svc.attempts.for_message(message.id)
    assert attempt.result == 'blind'
