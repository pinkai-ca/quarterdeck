"""A fresh start's index lists what is still open for its seat: a message carried as an index line alone leaves it
once the seat has printed its thread whole after that launch, as receipted by an unjailed qd.

Synthetic ship, fake harness; no program runs. The seat's claim is what its qd thread writes in its own folder, the one
place a jail lets it write; the receipt is what an unjailed qd takes from it into the seat's trusted folder.
"""

import json

import pytest
from datetime import datetime, timedelta

from quarterdeck import post, post_reads, post_threads, trusted_files
from .test_thread_read_receipts import command, indexed, rows, world  # noqa: F401 -- world is a fixture


def launch_again(world, message):
    """A later fresh start that carries the message as an index line alone, as _up closes its attempt."""
    attempt = world.attempts.open(message, 'builder', '%synthetic', 72, 'synthetic-later-launch')
    return world.attempts.close(attempt, 'launch', post_threads.INDEXED_LAUNCH_DETAIL)


def test_a_closed_order_is_not_listed_unless_its_thread_holds_something_open_for_the_seat(world):
    order, _ = indexed(world, 'order')
    world.store.new('report', 'builder', post.SHIP, 'Synthetic done.', re=order.id)
    assert order.id not in rows(world), 'reported on: closed, not listed'
    follow, _ = indexed(world, 'order', re=order.id)  # its thread now holds an open order for the seat
    row = rows(world)[order.id]
    assert follow.id in row.active and order.id not in row.active


def test_an_answer_whose_thread_the_seat_printed_is_listed_once_then_not(world, capsys):
    answer, _ = indexed(world, 'answer')
    assert answer.id in rows(world), 'listed at the launch that carried it as an index line'
    assert answer.id in rows(world), 'and at the next, while the seat has not read it'
    assert command(world, answer.id) == 0
    assert answer.body in capsys.readouterr().out
    assert answer.id not in rows(world), 'printed whole after its launch: it has reached the seat'
    assert command(world, answer.id) == 0 and answer.body in capsys.readouterr().out, 'nothing is lost'


def test_an_open_order_is_always_listed_whatever_the_seat_printed(world, capsys):
    order, _ = indexed(world, 'order')
    assert command(world, order.id) == 0
    capsys.readouterr()
    assert order.id in rows(world), 'printed whole, and still listed: only its report closes it'
    assert order.id not in post_reads.read(world.ship.runtime, 'builder'), 'qd thread never receipts an obligation'
    launch_again(world, order)
    assert order.id in rows(world)


def test_a_thread_printed_before_the_launch_does_not_count(world, capsys):
    answer, _ = indexed(world, 'answer')
    assert command(world, answer.id) == 0
    capsys.readouterr()
    assert answer.id not in rows(world), 'read after the launch that carried it'
    launch_again(world, answer)  # a fresh start carries it again: the reading belongs to the conversation before it
    assert answer.id in rows(world), 'a reading before this launch does not count for it'
    assert command(world, answer.id) == 0
    capsys.readouterr()
    assert answer.id not in rows(world), 'printed again after this launch'


def test_a_reading_claimed_before_the_launch_and_taken_after_it_does_not_count(world):
    """The claim waits in the seat's folder (a pass had not taken it) when the next launch begins: the launch takes
    it before its index, so it is dated before that launch."""
    answer, _ = indexed(world, 'answer')
    post_reads.record(world.ship.runtime, 'builder', [answer.id])  # claimed, not yet taken
    rows(world)  # this launch's index takes it first
    launch_again(world, answer)
    assert answer.id in rows(world)


def test_a_jailed_seats_reading_is_recorded_the_same_way(world, capsys):
    """qd thread writes only the seat's own claim, which its jail can write; the receipt is taken by an unjailed qd
    into the trusted folder, which the jail shows as an empty folder of its own."""
    answer, _ = indexed(world, 'answer')
    receipts = post_reads.receipts_path(world.ship.runtime, 'builder')
    assert command(world, answer.id) == 0
    capsys.readouterr()
    assert post_reads.path(world.ship.runtime, 'builder').parent == world.ship.runtime / 'seats' / 'builder'
    assert answer.id in post_reads.claimed(world.ship.runtime, 'builder') and not receipts.exists()
    # Inside the jail the trusted folder is not there: a take made there writes nothing and spends no claim.
    jail_runtime = world.ship.runtime.parent / 'jail-view'
    (jail_runtime / 'seats').mkdir(parents=True)
    (jail_runtime / 'seats' / 'builder').symlink_to(world.ship.runtime / 'seats' / 'builder')
    assert post_reads.take(jail_runtime, 'builder', lambda: [answer.id]) == 0
    assert answer.id in post_reads.claimed(world.ship.runtime, 'builder'), 'the claim waits for an unjailed take'
    # The service's pass, unjailed, takes it.
    assert post_reads.take(world.ship.runtime, 'builder', lambda: [answer.id]) == 1
    assert answer.id in post_reads.read(world.ship.runtime, 'builder')
    assert answer.id not in rows(world)


def test_a_forged_claim_can_only_hide_a_non_obligation_of_its_own_seat(world):
    """The worst a claim the seat writes by hand does: a message addressed to it, not an order or a question, leaves
    its own next index, as reading it would; it stays a file, in its inbox and in qd thread. A claim cannot date
    itself after its take, names no other seat's message, and closes nothing."""
    answer, _ = indexed(world, 'answer')
    order, _ = indexed(world, 'order')
    other = world.store.new('report', post.SHIP, 'peer', 'Synthetic body for another seat.')
    future = (datetime.now().astimezone() + timedelta(days=30)).isoformat()
    claim = post_reads.path(world.ship.runtime, 'builder')
    claim.parent.mkdir(parents=True, exist_ok=True)
    claim.write_text(json.dumps(dict(format=1, seat='builder', read={
        answer.id: future, order.id: future, other.id: future})))
    before = datetime.now().astimezone()
    current = rows(world)
    times = post_reads.read_times(world.ship.runtime, 'builder')
    assert other.id not in times, 'a message the seat was not party to is never receipted'
    assert times[answer.id] <= datetime.now().astimezone() and times[answer.id] >= before, 'dated by its take'
    assert answer.id not in current, 'hidden from its own index'
    assert order.id in current, 'an order is never hidden: only its report closes it'
    launch_again(world, answer)
    assert answer.id in rows(world), 'a forged future date does not outlive the next launch'
    assert world.store.get(answer.id) is not None


def test_the_service_pass_receipts_a_claim_into_the_trusted_folder(world):
    answer, _ = indexed(world, 'answer')
    post_reads.record(world.ship.runtime, 'builder', [answer.id])
    service = type('Pass', (), {})()
    service.ship, service.runtime, service.store = world.ship, world.ship.runtime, world.store
    service.log = lambda *args, **kwargs: None
    from quarterdeck.service import Service
    Service._take_thread_reads(service)
    assert post_reads.read(world.ship.runtime, 'builder') == {answer.id}
    assert post_reads.receipts_path(world.ship.runtime, 'builder').parent == trusted_files.folder(
        world.ship.runtime, 'builder')
    assert not post_reads.path(world.ship.runtime, 'builder').exists()


# -- a claim that cannot be taken never stops a launch or a pass, and is logged once while it stands ---------------

def logged(world):
    log = world.ship.runtime / 'log' / 'service.jsonl'
    lines = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return [line for line in lines if line.get('step') == 'thread-reads']


def a_pass(world):
    from quarterdeck.service import Service
    service = type('Pass', (), {})()
    service.ship, service.runtime, service.store = world.ship, world.ship.runtime, world.store
    service.log = lambda event, **data: logged_by_service.append((event, data))
    logged_by_service = []
    Service._take_thread_reads(service)
    Service._take_thread_reads(service)
    return logged_by_service


def plant(world, kind, monkeypatch):
    """What a jailed seat can put at its claim's name: a link, a FIFO, an oversize file."""
    import os
    claim = post_reads.path(world.ship.runtime, 'builder')
    claim.parent.mkdir(parents=True, exist_ok=True)
    if kind == 'link':
        elsewhere = world.ship.runtime.parent / 'elsewhere.json'
        elsewhere.write_text(json.dumps(dict(format=1, seat='builder', read={})))
        claim.symlink_to(elsewhere)
    elif kind == 'fifo':
        os.mkfifo(claim)
    else:
        monkeypatch.setattr(post_reads, 'LIMIT', 256)  # the bound, made small: the same refusal as a 17 MiB claim
        claim.write_text(json.dumps(dict(format=1, seat='builder', read={'x' * 300: 'synthetic'})))
    return claim, os.lstat(claim)


@pytest.mark.parametrize('kind', ['link', 'fifo', 'oversize'])
def test_a_claim_that_cannot_be_read_is_left_and_logged_once_and_never_stops_a_launch(world, kind, monkeypatch):
    import os
    answer, _ = indexed(world, 'answer')
    claim, before = plant(world, kind, monkeypatch)
    assert answer.id in rows(world), "the launch's index is built, without the claim"
    assert answer.id in rows(world)
    assert a_pass(world) == [], 'the take refused it itself: no error escaped to the pass'
    assert not post_reads.receipts_path(world.ship.runtime, 'builder').exists(), 'nothing receipted'
    after = os.lstat(claim)
    assert (after.st_mode, after.st_ino) == (before.st_mode, before.st_ino), 'the claim is left in place'
    [line] = logged(world)
    assert line['seat'] == 'builder' and 'left in place' in line['error'], 'logged once while it stands'


def test_a_claim_replaced_by_a_folder_before_its_removal_receipts_nothing(world, monkeypatch, capsys):
    answer, _ = indexed(world, 'answer')
    assert command(world, answer.id) == 0
    capsys.readouterr()
    claim = post_reads.path(world.ship.runtime, 'builder')
    real = post_reads._load

    def swapped(target, seat):
        data = real(target, seat)
        if target == claim:
            claim.unlink()
            claim.mkdir()  # the seat swaps its claim for a folder between the reading and the removal
        return data
    monkeypatch.setattr(post_reads, '_load', swapped)
    assert answer.id in rows(world), 'the launch is built; the reading is not receipted'
    monkeypatch.setattr(post_reads, '_load', real)
    assert a_pass(world) == []
    assert not post_reads.receipts_path(world.ship.runtime, 'builder').exists()
    assert claim.is_dir(), 'left where it is'
    [line] = logged(world)
    assert 'cannot be removed' in line['error']
    claim.rmdir()  # the seat mends its folder: the refusal is settled, and a new one would be logged again
    rows(world)
    assert not (world.ship.runtime / 'trusted' / 'builder' / post_reads.REFUSED).exists()


def test_a_take_that_fails_outright_never_stops_a_launch_and_a_pass_logs_it_once(world, monkeypatch):
    answer, _ = indexed(world, 'answer')

    def broken(*args, **kwargs):
        raise RuntimeError('synthetic take failure')
    monkeypatch.setattr(post_reads, 'take', broken)
    assert answer.id in rows(world) and answer.id in rows(world), 'the launch is built without it'
    assert len(logged(world)) == 1, "the launch's guard logs it once"
    assert [event for event, data in a_pass(world) if data['seat'] == 'builder'] == ['error'], \
        'two passes, one line while it stands'
