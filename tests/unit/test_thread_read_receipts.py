"""Ordinary synthetic thread-read receipts and fresh index selection; no native program or transport."""

from concurrent.futures import ThreadPoolExecutor
from io import StringIO
import json
import sys
from types import SimpleNamespace

import pytest

from quarterdeck import cli, post, post_reads, post_threads, ship as shipmod, trusted_files
from quarterdeck.seat import Seat
from quarterdeck.ship import ShipError


@pytest.fixture
def world(tmp_path, monkeypatch):
    (tmp_path/'ship.toml').write_text('''[ship]
code="checkout"
name="synthetic"
[pools.fake]
provider="fake"
[sessions.crew]
[seats.builder]
session="crew"
harness="fake"
pool="fake"
model="fake-1"
[seats.peer]
session="crew"
harness="fake"
pool="fake"
model="fake-1"
''')
    for key in ('QD_SHIP', 'QD_SEAT', 'QD_TMUX_SOCKET', 'TMUX', 'TMUX_PANE'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('QD_SEAT', 'builder')
    ship = shipmod.load(tmp_path)
    trusted_files.make(ship.runtime, 'builder')  # a launched seat's trusted folder, where its receipts are kept
    return SimpleNamespace(ship=ship, seat=Seat(ship, ship.seat('builder'), SimpleNamespace()),
                           store=post.Store(ship.runtime), attempts=post.Attempts(ship.runtime))


def indexed(world, kind='report', **fields):
    message = world.store.new(kind, post.SHIP, 'builder', 'Synthetic complete body.', **fields)
    attempt = world.attempts.open(message, 'builder', '%synthetic', 71, 'synthetic-launch')
    attempt = world.attempts.close(attempt, 'launch', post_threads.INDEXED_LAUNCH_DETAIL)
    return message, attempt


def rows(world):
    return {row.id: row for row in world.seat.carriage().rows}


def command(world, reference, *, json_output=False):
    return cli.main(['--ship', str(world.ship.folder), *(['--json'] if json_output else []), 'thread', reference])


@pytest.mark.parametrize('json_output', [False, True])
def test_indexed_notice_read_by_its_seat_leaves_the_next_fresh_index(world, capsys, json_output):
    notice, attempt = indexed(world)
    before = attempt.path.read_bytes()
    assert notice.id in rows(world)
    assert command(world, notice.id, json_output=json_output) == 0
    printed = capsys.readouterr().out
    assert notice.body in printed
    if json_output:
        assert json.loads(printed)['root'] == notice.id
    assert notice.id in post_reads.claimed(world.ship.runtime, 'builder'), 'qd thread claims it'
    assert notice.id not in post_reads.read(world.ship.runtime, 'builder'), 'a claim alone is no receipt'
    assert notice.id not in rows(world), 'the index takes the claim first'
    assert notice.id in post_reads.read(world.ship.runtime, 'builder')
    assert not post_reads.path(world.ship.runtime, 'builder').exists(), 'the claim is spent'
    assert world.seat.first_message() == (None, [])
    assert attempt.path.read_bytes() == before
    assert world.attempts.latest()[notice.id] == 'launch'


def test_report_alone_settles_its_recipients_non_obligation(world):
    notice, attempt = indexed(world)
    before = attempt.path.read_bytes()
    world.store.new('report', 'builder', post.SHIP, 'Synthetic completed reading.', re=notice.id)
    assert notice.id not in rows(world)
    assert not post_reads.path(world.ship.runtime, 'builder').exists()
    assert attempt.path.read_bytes() == before


def test_other_reporters_and_other_references_do_not_settle_a_notice(world):
    notice, _ = indexed(world)
    other, _ = indexed(world)
    world.store.new('report', 'peer', post.SHIP, 'Synthetic peer report.', re=notice.id)
    world.store.new('report', 'builder', post.SHIP, 'Synthetic other report.', re=other.id)
    assert notice.id in rows(world) and other.id not in rows(world)


@pytest.mark.parametrize('kind,reply', [('order', 'report'), ('question', 'answer')])
def test_thread_read_keeps_indexed_obligation_open_until_its_normal_reply(world, capsys, kind, reply):
    message, _ = indexed(world, kind)
    assert command(world, message.id) == 0
    assert message.body in capsys.readouterr().out
    assert world.store.is_open(message) and message.id in rows(world)
    assert message.id not in world.store.read_ids('builder')
    world.store.new(reply, 'builder', post.SHIP, 'Synthetic completion.', re=message.id)
    assert not world.store.is_open(message) and message.id not in rows(world)


def test_reporting_on_a_question_does_not_replace_its_answer(world):
    question, _ = indexed(world, 'question')
    world.store.new('report', 'builder', post.SHIP, 'Synthetic report only.', re=question.id)
    assert world.store.is_open(question) and question.id in rows(world)


def test_read_notice_under_open_order_is_history_while_the_order_stays_active(world, capsys):
    order, _ = indexed(world, 'order')
    notice, _ = indexed(world, re=order.id)
    assert notice.id in rows(world)[order.id].active
    assert command(world, notice.id) == 0
    assert notice.body in capsys.readouterr().out
    row = rows(world)[order.id]
    assert order.id in row.active and notice.id not in row.active
    assert notice.id in row.amendments and world.store.is_open(order)


def test_pure_thread_snapshot_has_no_receipt_side_effect(world):
    notice, _ = indexed(world)
    assert notice.body in post_threads.render(post_threads.whole(world.store, notice.id, 'builder'))
    assert notice.id in rows(world)
    assert not post_reads.path(world.ship.runtime, 'builder').exists()


def test_thread_handler_still_collects_a_reading_without_receipting(world):
    notice, _ = indexed(world)
    args = cli.build_parser().parse_args(['--ship', str(world.ship.folder), 'thread', notice.id])
    reading = args.run(args)
    assert isinstance(reading, cli.Reading) and reading.data.root == notice.id
    assert notice.body in reading.render(reading.data)
    assert notice.id in rows(world)
    assert not post_reads.path(world.ship.runtime, 'builder').exists()


def test_reading_from_another_seat_does_not_receipt_the_addressees_notice(world, monkeypatch, capsys):
    notice, _ = indexed(world)
    monkeypatch.setenv('QD_SEAT', 'peer')
    assert command(world, notice.id) == 0
    assert notice.body not in capsys.readouterr().out
    assert notice.id in rows(world)
    assert not post_reads.path(world.ship.runtime, 'builder').exists()


@pytest.mark.parametrize('failure', ['render', 'write', 'flush'])
def test_incomplete_output_leaves_the_notice_in_the_fresh_index(world, monkeypatch, failure):
    notice, _ = indexed(world)
    class Unfinished(StringIO):
        def write(self, text):
            return len(text)-1 if failure == 'write' else super().write(text)
        def flush(self):
            if failure == 'flush':
                raise OSError('synthetic flush failure')
            super().flush()
    def unrenderable(thread):
        raise ValueError('synthetic render failure')
    with monkeypatch.context() as reading:
        if failure == 'render':
            reading.setattr(post_threads, 'render', unrenderable)
        else:
            reading.setattr(sys, 'stdout', Unfinished())
        with pytest.raises((OSError, ValueError)):
            command(world, notice.id)
    assert notice.id in rows(world)
    assert not post_reads.path(world.ship.runtime, 'builder').exists()


def test_repeat_read_is_idempotent_and_writes_only_its_own_seat_receipt(world, capsys, monkeypatch):
    notice, _ = indexed(world)
    assert command(world, notice.id) == 0
    capsys.readouterr()
    target = post_reads.path(world.ship.runtime, 'builder')
    before = target.read_bytes(), target.stat().st_mtime_ns
    def unexpected_write(*args, **kwargs):
        pytest.fail('repeat reading tried to rewrite its receipt')
    monkeypatch.setattr(post_reads.durable, 'replace_file', unexpected_write)
    assert command(world, notice.id) == 0
    capsys.readouterr()
    assert (target.read_bytes(), target.stat().st_mtime_ns) == before


def test_concurrent_read_receipts_merge_without_losing_either(world):
    with ThreadPoolExecutor(max_workers=2) as writers:
        jobs = [writers.submit(post_reads.record, world.ship.runtime, 'builder', [identifier])
                for identifier in ('synthetic-first', 'synthetic-second')]
        for job in jobs:
            job.result()
    assert post_reads.claimed(world.ship.runtime, 'builder') == {'synthetic-first', 'synthetic-second'}


def test_unreadable_receipt_never_suppresses_work_or_gets_overwritten(world):
    notice, _ = indexed(world)
    target = post_reads.path(world.ship.runtime, 'builder')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{ synthetic incomplete metadata')
    before = target.read_bytes()
    assert notice.id in rows(world)
    with pytest.raises(ShipError, match='thread read receipts unreadable'):
        post_reads.record(world.ship.runtime, 'builder', [notice.id])
    assert target.read_bytes() == before


def test_busy_receipt_writer_does_not_wait_indefinitely(world, monkeypatch):
    def busy(*args):
        raise BlockingIOError('synthetic concurrent writer')
    monkeypatch.setattr(post_reads.fcntl, 'flock', busy)
    monkeypatch.setattr(post_reads, 'WAIT', 0)
    with pytest.raises(ShipError, match='receipt writer busy'):
        post_reads.record(world.ship.runtime, 'builder', ['synthetic-notice'])
    assert post_reads.read(world.ship.runtime, 'builder') == frozenset()


@pytest.mark.parametrize('json_output', [False, True])
@pytest.mark.parametrize('large', [False, True])
def test_closed_stdout_thread_read_exits_quietly_without_a_read_receipt(world, json_output, large):
    """Close the actual pipe after collecting the thread, before its write/flush."""
    import os
    from pathlib import Path
    import select
    import subprocess

    notice = world.store.new('report', post.SHIP, 'builder',
                             'Synthetic notice. ' * (60000 if large else 1))
    attempt = world.attempts.open(notice, 'builder', '%synthetic', 71, 'synthetic-launch')
    attempt = world.attempts.close(attempt, 'launch', post_threads.INDEXED_LAUNCH_DETAIL)
    original_attempt = attempt.path.read_bytes()
    driver = r'''
import os, sys
sys.path.insert(0, sys.argv[1])
import quarterdeck
from quarterdeck import cli
folder, reference, as_json = sys.argv[2], sys.argv[3], bool(int(sys.argv[4]))
ready, proceed = int(sys.argv[5]), int(sys.argv[6])
original = cli.emit
def emit(args, result, **kwargs):
    os.write(ready, b'1')
    assert os.read(proceed, 1) == b'1'
    return original(args, result, **kwargs)
cli.emit = emit
sys.argv = ['qd', '--ship', folder, *(['--json'] if as_json else []), 'thread', reference]
sys.exit(quarterdeck.main())
'''
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    process = None
    try:
        process = subprocess.Popen(
            [sys.executable, '-I', '-c', driver, str(Path(cli.__file__).resolve().parents[1]),
             str(world.ship.folder), notice.id, str(int(json_output)), str(ready_write), str(release_read)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(ready_write, release_read))
        os.close(ready_write)
        ready_write = None
        os.close(release_read)
        release_read = None
        assert select.select([ready_read], [], [], 15)[0], 'thread did not reach its final output'
        assert os.read(ready_read, 1) == b'1'
        process.stdout.close()
        process.stdout = None
        os.write(release_write, b'1')
        _, stderr = process.communicate(timeout=15)
        assert process.returncode == 0
        assert stderr == b'', 'no traceback or interpreter shutdown-flush warning'
    finally:
        for descriptor in (ready_read, ready_write, release_read, release_write):
            if descriptor is not None:
                os.close(descriptor)
        if process is not None:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if process.stderr is not None:
                process.stderr.close()
            if process.stdout is not None:
                process.stdout.close()
    assert not post_reads.path(world.ship.runtime, 'builder').exists()
    assert notice.id not in world.store.read_ids('builder')
    assert notice.id in rows(world), 'incomplete output cannot settle the next fresh index'
    assert attempt.path.read_bytes() == original_attempt
