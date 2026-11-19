"""The failure scans read a seat's JSON-lines record from where they last stopped, and start over whenever they
cannot prove it only grew; every state given equals the whole reading's."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import record_follow
from quarterdeck.harness import get
from tests.compaction_shapes import Native


class Seat:
    def __init__(self, name, record, program='codex', pid=100):
        self.name, self.harness, self.pid, self.path = name, get(program), pid, record

    def launched(self):
        return SimpleNamespace(pane='%1', pid=self.pid, at='2030-01-01T00:00:00+00:00')

    def record(self):
        return self.path


@pytest.fixture(autouse=True)
def fresh():
    record_follow._followed.clear()
    yield
    record_follow._followed.clear()


def conversation(tmp_path, program='codex', turns=2):
    native = Native(tmp_path / f'{program}.jsonl', program)
    for _ in range(turns):
        native.call()
        native.result()
        native.finish()
        native.begin()
    return native


def whole(seat):
    return seat.harness.state(seat.path)


def entry(seat):
    return record_follow._followed.get(seat.name)


def test_a_grown_record_feeds_only_its_new_lines_to_the_same_reader(tmp_path):
    native = conversation(tmp_path)
    seat = Seat('worker', native.path)
    assert record_follow.state(seat, seat.path) == whole(seat)
    reader, offset = entry(seat).reader, entry(seat).offset
    native.call()
    native.result()
    native.finish()
    assert record_follow.state(seat, seat.path) == whole(seat)
    assert entry(seat).reader is reader, 'the same reader, fed from where it stopped'
    assert entry(seat).offset == native.path.stat().st_size > offset


def test_a_replaced_record_starts_over(tmp_path):
    native = conversation(tmp_path)
    seat = Seat('worker', native.path)
    record_follow.state(seat, seat.path)
    reader = entry(seat).reader
    replacement = native.path.with_suffix('.new')
    replacement.write_bytes(native.path.read_bytes())
    replacement.replace(native.path)  # the same bytes under a new inode
    assert record_follow.state(seat, seat.path) == whole(seat)
    assert entry(seat).reader is not reader, 'a new identity: read whole again'


def test_a_record_shorter_than_where_it_stopped_starts_over(tmp_path):
    native = conversation(tmp_path)
    seat = Seat('worker', native.path)
    record_follow.state(seat, seat.path)
    reader = entry(seat).reader
    lines = native.path.read_bytes().splitlines(keepends=True)
    with open(native.path, 'r+b') as stream:  # the same inode, cut back to fewer lines
        stream.truncate(sum(len(line) for line in lines[:-3]))
    assert record_follow.state(seat, seat.path) == whole(seat)
    assert entry(seat).reader is not reader


def test_a_record_rewritten_before_where_it_stopped_starts_over(tmp_path):
    native = conversation(tmp_path)
    seat = Seat('worker', native.path)
    record_follow.state(seat, seat.path)
    reader, offset = entry(seat).reader, entry(seat).offset
    native.finish()  # it grows (the helper rewrites the file whole, in place) ...
    data = bytearray(native.path.read_bytes())
    at = offset - 10
    data[at] = ord('X') if data[at] != ord('X') else ord('Y')  # ... and a byte before the offset changed
    with open(native.path, 'r+b') as stream:
        stream.write(bytes(data))
    assert record_follow.state(seat, seat.path) == whole(seat)
    assert entry(seat).reader is not reader, 'its tail no longer matches: read whole again'


@pytest.mark.parametrize('change', ['launch', 'pointer'])
def test_a_new_launch_or_record_pointer_starts_over(tmp_path, change):
    native = conversation(tmp_path)
    seat = Seat('worker', native.path)
    record_follow.state(seat, seat.path)
    reader = entry(seat).reader
    if change == 'launch':
        seat.pid = 200  # a fresh start: the same file must never feed an old reader a new conversation
    else:
        other = conversation(tmp_path / 'other')
        seat.path = other.path
    assert record_follow.state(seat, seat.path) == whole(seat)
    assert entry(seat).reader is not reader


def test_a_final_line_still_being_written_gives_the_whole_readings_state(tmp_path):
    native = conversation(tmp_path)
    seat = Seat('worker', native.path)
    record_follow.state(seat, seat.path)
    complete = native.path.stat().st_size
    with open(native.path, 'ab') as stream:
        stream.write(b'{"type": "user", "message": {"role": "user", "con')  # a writer mid-line
    assert record_follow.state(seat, seat.path) == whole(seat)
    assert entry(seat).offset == complete, 'only complete lines are kept'


def test_a_record_another_reader_reads_takes_its_own_path(tmp_path, monkeypatch):
    seat = Seat('hand', tmp_path / 'opencode.db', 'opencode')
    monkeypatch.setattr(type(seat.harness), 'state', classmethod(lambda cls, record: 'its own reading'))
    assert record_follow.state(seat, seat.path) == 'its own reading' and entry(seat) is None


def test_claude_status_uses_the_stream_on_a_larger_owned_record(tmp_path, monkeypatch):
    from quarterdeck.harness import record_file

    native = conversation(tmp_path, 'claude')
    seat = Seat('worker', native.path, 'claude')
    at_bound = native.path.stat().st_size
    monkeypatch.setattr(record_file, 'LINES_BOUND', at_bound - 1)
    monkeypatch.setattr(record_file, 'CLAUDE_STREAM_BOUND', at_bound)
    assert record_follow.state(seat, seat.path) == whole(seat)
    assert entry(seat) is None
    with seat.path.open('ab') as stream:
        stream.write(b'\n')
    with pytest.raises(record_file.PastBound) as refused:
        record_follow.state(seat, seat.path)
    assert refused.value.bound == at_bound
