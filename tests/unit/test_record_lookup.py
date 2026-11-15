"""Read-only joined-record lookups and quiet service passes on synthetic seats only."""

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import builtins
import fcntl
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import record_owner
from quarterdeck.harness.synthetic_record import ClaudeShape
from quarterdeck.seat import Launched, Seat
from quarterdeck.service import Service
from quarterdeck.ship import Pool, SeatSpec, Session, Ship


AT = datetime(2026, 1, 1, tzinfo=timezone.utc)
ZERO = dict(write_opens=0, append_opens=0, append_writes=0, mkdirs=0, locks=0)


def make_seat(tmp_path, name='worker', *, home=True):
    pools = {name: Pool(name, 'fake') for name in ('pool-a', 'pool-b')}
    spec = SeatSpec(name, 'crew', 'worker', 'fake', tuple(pools), cwd=tmp_path)
    ship = Ship(tmp_path, 'synthetic', 'operator', tmp_path, pools,
                {'crew': Session('crew', 'crew')}, {name: spec})
    seat = Seat(ship, spec, SimpleNamespace(pane=lambda ident: None))
    if home:
        seat.home.mkdir(parents=True)
    return seat


def launch(seat):
    value = Launched('%1', 1001, AT.isoformat(), 'pool-a', None, ['fake'], 'fake', 'fake')
    value.write(seat.folder / 'launch.json')
    return value


def candidate(seat, name='first', seconds=1):
    shape = ClaudeShape(seat.home, seat.ship.workspace, name, 'synthetic-model')
    shape.write(dict(type='user', timestamp=(AT + timedelta(seconds=seconds)).isoformat(),
                     message=dict(role='user', content='synthetic input')))
    shape.write(dict(type='system', subtype='turn_duration',
                     timestamp=(AT + timedelta(seconds=seconds + 1)).isoformat(), durationMs=1000))
    return shape.path


@contextmanager
def mutations(monkeypatch, root):
    """Count actual write-capable opens, append writes, mkdir attempts and flock calls.

    io.open covers Path.open; builtins.open and os.open cover the other entry points.
    Only the synthetic project is counted, including unsuccessful mkdir attempts.
    """
    counts = dict(ZERO)
    def within(path):
        return isinstance(path, (str, bytes, os.PathLike)) and Path(os.fsdecode(path)).is_relative_to(root)

    class Appending:
        def __init__(self, stream):
            self.stream = stream
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def __iter__(self):
            return iter(self.stream)
        def __enter__(self):
            self.stream.__enter__()
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def write(self, text):
            counts['append_writes'] += 1
            return self.stream.write(text)

    def resolved(path, folder):
        """A name opened within a folder's handle (seat_files' no-follow writes), as its full path."""
        if folder is None or not isinstance(path, (str, bytes, os.PathLike)):
            return path
        return Path(os.readlink(f'/proc/self/fd/{folder}')) / os.fsdecode(path)

    def opening(real):
        def opened(path, mode='r', *args, **kwargs):
            if isinstance(path, int):
                # os.fdopen of a descriptor os.open already counted: only its writes are counted here.
                stream = real(path, mode, *args, **kwargs)
                try:
                    inside = within(os.readlink(f'/proc/self/fd/{path}'))
                except OSError:
                    inside = False
                return Appending(stream) if inside and 'a' in mode else stream
            tracked = within(path)
            if tracked and set(mode) & set('wax+'):
                counts['write_opens'] += 1
            if tracked and 'a' in mode:
                counts['append_opens'] += 1
            stream = real(path, mode, *args, **kwargs)
            return Appending(stream) if tracked and 'a' in mode else stream
        return opened

    real_open, real_mkdir, real_flock = os.open, os.mkdir, fcntl.flock
    def opened(path, flags, *args, **kwargs):
        if within(resolved(path, kwargs.get('dir_fd'))) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC
                                                                     | os.O_APPEND):
            counts['write_opens'] += 1
            if flags & os.O_APPEND:
                counts['append_opens'] += 1
        return real_open(path, flags, *args, **kwargs)
    def mkdir(path, *args, **kwargs):
        if within(resolved(path, kwargs.get('dir_fd'))):
            counts['mkdirs'] += 1
        return real_mkdir(path, *args, **kwargs)
    def flock(fd, operation):
        if within(os.readlink(f'/proc/self/fd/{fd}')):
            counts['locks'] += 1
        return real_flock(fd, operation)

    with monkeypatch.context() as patch:
        patch.setattr(io, 'open', opening(io.open))
        patch.setattr(builtins, 'open', opening(builtins.open))
        patch.setattr(os, 'open', opened)
        patch.setattr(os, 'mkdir', mkdir)
        patch.setattr(fcntl, 'flock', flock)
        yield counts


def test_lookup_never_discovers_or_creates_an_unjoined_home(tmp_path, monkeypatch):
    seat = make_seat(tmp_path, home=False)
    monkeypatch.setattr(seat.harness, 'join', lambda *args: pytest.fail('lookup must not discover records'))
    with mutations(monkeypatch, tmp_path) as counts:
        assert seat.lookup_record() is None
    assert counts == ZERO and not seat.folder.exists()


def test_lookup_writes_nothing_even_when_legacy_reader_binding_is_missing(tmp_path, monkeypatch):
    seat = make_seat(tmp_path)
    launch(seat)
    path = candidate(seat)
    (seat.folder / 'record').write_text(str(path))
    with mutations(monkeypatch, tmp_path) as counts:
        assert seat.lookup_record() == path
    assert counts == ZERO
    assert not record_owner.registry(seat.folder).exists()
    assert not (seat.folder / 'record-launches.jsonl').exists()
    assert not (seat.home / 'conversations.owned').exists()
    # Normal record() must still finish retaining the legacy join.
    assert seat.record() == path
    with mutations(monkeypatch, tmp_path) as counts:
        assert seat.record() == seat.lookup_record() == path
    assert counts == ZERO


def test_record_still_joins_the_first_new_record_and_keeps_its_pin(tmp_path, monkeypatch):
    seat = make_seat(tmp_path)
    launch(seat)
    candidate(seat, 'older', -10)
    assert seat.record() is None and seat.join().reason == 'no record yet'
    first = candidate(seat)
    candidate(seat, 'later', 5)
    assert seat.lookup_record() is None
    with mutations(monkeypatch, tmp_path) as counts:
        assert seat.record() == first  # law: one-record
    assert counts['append_writes'] == 2, 'reader binding and exact launch boundary are retained'
    assert counts['locks'] == 2
    with mutations(monkeypatch, tmp_path) as counts:
        assert seat.join().record == seat.lookup_record() == first
    assert counts == ZERO, 'readers needing Join.reason also avoid republication'
    # A restarted reader has no process-local cache to rely on.
    other = Seat(seat.ship, seat.spec, seat.tmux)
    with mutations(monkeypatch, tmp_path) as counts:
        assert other.record() == first
    assert counts == ZERO


def test_an_ambiguous_first_join_is_reported_and_never_pinned(tmp_path, monkeypatch):
    seat = make_seat(tmp_path)
    launch(seat)
    candidate(seat, 'first')
    candidate(seat, 'twin')
    with mutations(monkeypatch, tmp_path) as counts:
        assert seat.lookup_record() is None
        assert seat.record() is None
        found = seat.join()
    assert found.record is None and found.reason.startswith('ambiguous:')  # law: one-record
    assert not (seat.folder / 'record').exists()
    assert counts == ZERO


@pytest.mark.parametrize('change', ['launch', 'pool', 'price', 'missing-journal', 'missing-reader', 'missing-owned'])
def test_incomplete_join_retention_still_uses_the_joining_path(tmp_path, monkeypatch, change):
    seat = make_seat(tmp_path)
    original = launch(seat)
    path = candidate(seat)
    assert seat.record() == path
    if change == 'launch':
        replace(original, at=(AT + timedelta(seconds=10)).isoformat(), pid=1002).write(seat.folder / 'launch.json')
    elif change == 'pool':
        replace(original, pool='pool-b').write(seat.folder / 'launch.json')
    elif change == 'price':
        replace(original, price={'synthetic': 1}).write(seat.folder / 'launch.json')
    else:
        named = {'missing-journal': seat.folder / 'record-launches.jsonl',
                 'missing-reader': record_owner.registry(seat.folder),
                 'missing-owned': seat.home / 'conversations.owned'}[change]
        named.unlink()
    joined = []
    real_join = seat.join
    def joining():
        joined.append(True)
        return real_join()
    monkeypatch.setattr(seat, 'join', joining)
    assert seat.record() == path and joined == [True]
    with mutations(monkeypatch, tmp_path) as counts:
        assert seat.record() == path
    assert counts == ZERO and joined == [True]


@pytest.mark.parametrize('change', ['empty-pin', 'missing-record', 'conflicting-reader', 'changed-launch-reader'])
def test_lookup_and_join_refuse_the_same_invalid_pin(tmp_path, monkeypatch, change):
    seat = make_seat(tmp_path)
    original = launch(seat)
    path = candidate(seat)
    assert seat.record() == path
    if change == 'empty-pin':
        (seat.folder / 'record').write_text('')
    elif change == 'missing-record':
        path.unlink()
    elif change == 'conflicting-reader':
        record_owner.register(seat.folder, path, 'claude')
    else:
        replace(original, harness='claude').write(seat.folder / 'launch.json')
    with mutations(monkeypatch, tmp_path) as counts:
        assert seat.lookup_record() is None
        assert seat.record() is None
        assert seat.join().record is None
    assert counts == ZERO


def test_quiet_service_pass_writes_nothing_after_the_first_join(tmp_path, monkeypatch):
    seats = [make_seat(tmp_path, f'worker-{n}') for n in range(4)]
    for seat in seats:
        launch(seat)
        candidate(seat)
        assert seat.record() is not None
    # follow is the real periodic duty, with no transport or ledger mutation at idle.
    svc = Service(seats[0].ship, seats[0].tmux)
    monkeypatch.setattr(svc, 'resolved_seats', lambda: seats)
    try:
        with monkeypatch.context() as old:
            # Original record() body and unconditional pinned-join publication on this base.
            old.setattr(Seat, 'record', lambda self: self.join().record)
            old.setattr(Seat, '_join_retained', lambda *args: False)
            with mutations(monkeypatch, tmp_path) as before:
                svc.follow()
                old_states = dict(svc.working)
        with mutations(monkeypatch, tmp_path) as after:
            svc.follow()
            new_states = dict(svc.working)
        assert old_states == new_states == {seat.name: False for seat in seats}
        assert before == dict(write_opens=8, append_opens=4, append_writes=0, mkdirs=16, locks=4)
        assert after == ZERO
        print('quiet-pass write counts: ' + json.dumps(dict(seats=len(seats), before=before, after=after), sort_keys=True))
    finally:
        svc.ledger.close()
