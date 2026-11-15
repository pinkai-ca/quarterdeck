"""One read of a seat's program record takes a bounded amount, and never another file.

Every reader of a record opens it through ``record_file``: no link at the name or on the way from the seat folder, no
fifo, socket or device (which could hold the read for good), only this user's own regular file with one name, the one
the path named; and it reads at most a bound. A record past the bound is not read: the reading says so, and the status
line names the seat from three quarters of the bound, by lstat alone.
"""
from __future__ import annotations

import os
from pathlib import Path
import socket
import sqlite3

import pytest

from quarterdeck import accounting_health, record_follow
from quarterdeck.harness import claude, json_record, record_file, sqlite_snapshot
from quarterdeck.harness.check_evidence import json_tools
from quarterdeck.harness.synthetic_record import ClaudeShape


@pytest.fixture
def seats(tmp_path):
    folder = tmp_path / 'runtime' / 'seats'
    folder.mkdir(parents=True)
    return folder


def record_of(seats, seat='worker-example'):
    shape = ClaudeShape(seats / seat / 'home', seats.parent.parent, 'synthetic', 'synthetic')
    shape.begin('First synthetic input', 'synthetic', 'low')
    return shape.path


def test_a_record_reads_whole_within_its_bound(seats):
    path = record_of(seats)
    data, opened = record_file.read_bytes(path, record_file.LINES_BOUND)
    assert data == path.read_bytes() and opened.st_ino == path.stat().st_ino
    assert claude.state(path).last_input == 'First synthetic input'


def test_a_link_at_the_records_name_is_refused(seats):
    real = record_of(seats)
    link = real.with_name('linked.jsonl')
    link.symlink_to(real)
    with pytest.raises(record_file.Refused, match='a link'):
        record_file.read_bytes(link, record_file.LINES_BOUND)
    with pytest.raises(json_record.RecordError):
        claude.state(link)


def test_a_link_on_the_way_to_another_seats_record_is_refused(seats):
    """The jail swaps a folder of its home for a link into another seat's: the inode and owner checks would pass,
    since the path names that file and every file is the operator's; the walk from the seat folder refuses it."""
    theirs = record_of(seats, 'other-example')
    mine = seats / 'worker-example' / 'home'
    mine.mkdir(parents=True)
    (mine / 'projects').symlink_to(theirs.parent.parent)
    path = mine / 'projects' / theirs.parent.name / theirs.name
    assert path.read_bytes() == theirs.read_bytes(), 'the path does reach the other seat'
    with pytest.raises(record_file.Refused, match='on the way'):
        record_file.read_bytes(path, record_file.LINES_BOUND)
    with pytest.raises(json_record.RecordError):
        claude.state(path)


def test_a_record_with_another_name_elsewhere_is_refused(seats, tmp_path):
    path = record_of(seats)
    os.link(path, tmp_path / 'elsewhere.jsonl')
    with pytest.raises(record_file.Refused, match='another name'):
        record_file.read_bytes(path, record_file.LINES_BOUND)


@pytest.mark.parametrize('kind', ['fifo', 'socket', 'device'])
def test_a_fifo_a_socket_or_a_device_reads_as_not_a_regular_file_and_the_pass_goes_on(seats, kind, monkeypatch):
    """None of them is opened: a fifo opened for reading would hold the service's read for good."""
    home = seats / 'worker-example' / 'home'
    home.mkdir(parents=True)
    path = home / 'record.jsonl'
    held = None
    if kind == 'fifo':
        os.mkfifo(path)
    elif kind == 'socket':
        held = socket.socket(socket.AF_UNIX)
        monkeypatch.chdir(home)  # a socket's path has a short limit
        held.bind(path.name)
    else:
        path = Path('/dev/null')
    try:
        with pytest.raises(record_file.Refused, match='not a regular file'):
            record_file.read_bytes(path, record_file.LINES_BOUND)
        with pytest.raises(json_record.RecordError):
            json_record.entries(path)
        assert claude.state(record_of(seats, 'next-example')).last_input == 'First synthetic input', 'the pass goes on'
    finally:
        if held is not None:
            held.close()


def test_a_fifo_swapped_in_after_the_check_never_holds_the_read(seats, monkeypatch):
    """Checked a regular file, then swapped for a fifo before the open: O_NONBLOCK opens it at once, fstat refuses."""
    path = record_of(seats)
    regular = os.stat(path)
    path.unlink()
    os.mkfifo(path)
    real = os.stat

    def checked(name, *args, **kwargs):
        return regular if name == path.name and not kwargs.get('follow_symlinks', True) else real(name, *args, **kwargs)
    monkeypatch.setattr(record_file.os, 'stat', checked)
    with pytest.raises(record_file.Refused, match='not a regular file'):
        record_file.open_record(path)


def test_a_file_replaced_between_the_check_and_the_open_is_refused(seats, monkeypatch, tmp_path):
    path = record_of(seats)
    (tmp_path / 'another').write_text('another file\n')
    other = os.stat(tmp_path / 'another')
    real = os.stat

    def checked(name, *args, **kwargs):
        return other if name == path.name and not kwargs.get('follow_symlinks', True) else real(name, *args, **kwargs)
    monkeypatch.setattr(record_file.os, 'stat', checked)
    with pytest.raises(record_file.Refused, match='replaced while being opened'):
        record_file.open_record(path)


def test_a_record_past_its_bound_is_not_read_and_the_reading_says_so(seats, monkeypatch):
    path = record_of(seats)
    bound = path.stat().st_size - 1
    monkeypatch.setattr(record_file, 'LINES_BOUND', bound)
    json_record.release(path)
    with pytest.raises(record_file.PastBound, match=r'record past the read bound .*; not read'):
        claude.state(path)
    with pytest.raises(record_file.PastBound):
        json_record.entries(path)


def test_a_record_that_grows_past_its_bound_during_the_read_is_not_read_in_part(seats):
    """fstat said it fitted; the read finds more: it stops at the bound plus one byte and refuses, never a part."""
    path = record_of(seats)
    descriptor, opened = record_file.open_record(path)
    try:
        small = os.stat_result((*tuple(opened)[:6], 0, *tuple(opened)[7:]))  # a size the read will outgrow
        with pytest.raises(record_file.PastBound):
            record_file.read_from(descriptor, small, path, 8)
    finally:
        os.close(descriptor)


def test_a_followed_record_past_its_bound_reads_unknown_as_the_whole_reading_does(seats, monkeypatch):
    from quarterdeck import harness
    path = record_of(seats)
    reader = record_follow.reader_class(harness.get('claude'))
    assert record_follow._follow('worker-example', reader, ('example',), path) is not None
    monkeypatch.setattr(record_file, 'LINES_BOUND', path.stat().st_size - 1)
    with pytest.raises(record_file.PastBound):
        record_follow._follow('worker-example', reader, ('example',), path)


def test_claudes_control_readers_take_no_link_and_no_more_than_the_bound(seats, monkeypatch):
    path = record_of(seats)
    origin = claude.control_origin(path, {'example': True})
    assert origin is not None and origin['offset'] == path.stat().st_size
    link = path.with_name('linked.jsonl')
    link.symlink_to(path)
    assert claude.control_origin(link, {'example': True}) is None
    monkeypatch.setattr(record_file, 'LINES_BOUND', path.stat().st_size - 1)
    assert claude.control_origin(path, {'example': True}) is None
    assert claude.control_suffix(path, origin) is None


def test_the_tool_evidence_reader_refuses_a_link(seats):
    path = record_of(seats)
    link = path.with_name('linked.jsonl')
    link.symlink_to(path)
    assert json_tools(link, lambda row: None)['status'] == 'unknown'


def database(path: Path, rows: int) -> None:
    connection = sqlite3.connect(path)
    connection.execute('create table t (x text)')
    connection.executemany('insert into t values (?)', [('synthetic ' * 20,)] * rows)
    connection.commit()
    connection.close()


def test_a_database_and_its_log_are_bounded_together_and_the_ledger_is_not(seats, monkeypatch):
    home = seats / 'worker-example' / 'home'
    home.mkdir(parents=True)
    path = home / 'opencode.db'
    database(path, 50)
    size = path.stat().st_size
    assert sqlite_snapshot.snapshot(path)[1][:16] == b'SQLite format 3\x00'
    (home / 'opencode.db-wal').write_bytes(b'')
    monkeypatch.setattr(record_file, 'DATABASE_BOUND', size - 1)
    with pytest.raises(record_file.PastBound, match='record past the read bound'):
        sqlite_snapshot.snapshot(path, record_file.DATABASE_BOUND)
    (home / 'opencode.db-wal').write_bytes(b'x' * 64)
    with pytest.raises(record_file.PastBound):
        sqlite_snapshot.snapshot(path, size + 32)
    (home / 'opencode.db-wal').unlink()
    ledger = seats.parent / 'ledger.sqlite'
    database(ledger, 50)
    assert sqlite_snapshot.snapshot(ledger, bound=None)[1][:16] == b'SQLite format 3\x00', 'the ship\'s own: no bound'


def test_the_status_line_names_a_record_near_and_past_its_bound_by_lstat_alone(seats, monkeypatch):
    from types import SimpleNamespace
    path = record_of(seats)
    size = path.stat().st_size

    def seat(human=False):
        return SimpleNamespace(name='worker-example', session=SimpleNamespace(human=human), record=lambda: path)
    monkeypatch.setattr(record_file, 'read_from', lambda *args, **kwargs: pytest.fail('the alarm read the record'))
    monkeypatch.setattr(record_file, 'LINES_BOUND', size * 2)
    assert record_file.alarms([seat()]) == [], 'half the bound: nothing to say'
    monkeypatch.setattr(record_file, 'LINES_BOUND', int(size / 0.8))
    [near] = record_file.alarms([seat()])
    assert near.startswith('worker-example: record ') and 'start it fresh at a natural break' in near
    monkeypatch.setattr(record_file, 'LINES_BOUND', size - 1)
    [past] = record_file.alarms([seat()])
    assert past.startswith('worker-example: record past the read bound') and past.endswith('not read; start it fresh')
    assert record_file.alarms([seat(human=True)]) == [], "a seat the operator owns: never its conversation"


def test_the_accounting_names_a_record_past_its_bound(seats):
    error = record_file.past_bound(Path('record.jsonl'), 200, 100)
    assert accounting_health.cause(error) == 'record past the read bound'
    assert accounting_health.cause(record_file.Refused('a link')) == 'record refused'
    assert {'record past the read bound', 'record refused'} <= accounting_health.CAUSES


def test_one_large_read_at_a_time(seats, monkeypatch):
    path = record_of(seats)
    entered = []

    class Spy:
        def __enter__(self):
            entered.append(True)

        def __exit__(self, *exc):
            return False
    monkeypatch.setattr(record_file, '_large', Spy())
    monkeypatch.setattr(record_file, 'LARGE', path.stat().st_size - 1)
    record_file.read_bytes(path, record_file.LINES_BOUND)
    assert entered == [True]
    monkeypatch.setattr(record_file, 'LARGE', path.stat().st_size)
    record_file.read_bytes(path, record_file.LINES_BOUND)
    assert entered == [True], 'a small read does not wait'
