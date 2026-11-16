"""A seat's launch is read once per change of launch.json and launch-price.json. An injected clock, never wall time."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import pricing, seat as seatmod, trusted_cache
from quarterdeck.post import POST_TIMESTAMP_TICK_NS
from quarterdeck.seat import LaunchUnreadable, Seat

LAUNCH = dict(pane='%1', pid=10, at='2030-01-01T00:00:00+00:00', pool='synthetic', resume=None,
              argv=['program', '--flag'], version='1.0', harness='fake', records_before=['a'])


@pytest.fixture
def reads(monkeypatch):
    """Every open of a launch.json, counted; the kept launches start empty and end empty."""
    seatmod._LAUNCHES.clear()
    opened = []
    real = open

    def counting(file, *args, **kwargs):
        if Path(file).name == 'launch.json':
            opened.append(Path(file))
        return real(file, *args, **kwargs)

    monkeypatch.setattr(seatmod, 'open', counting, raising=False)
    yield opened
    seatmod._LAUNCHES.clear()


def holder(tmp_path):
    return SimpleNamespace(folder=tmp_path, trusted=tmp_path)


def write(tmp_path, data=LAUNCH, *, price=None):
    path = tmp_path / 'launch.json'
    path.write_text(json.dumps(data))
    if price is not None:
        pricing.save_price(path, price)
    return path


def newest(*paths):
    return max((max(os.stat(p).st_mtime_ns, os.stat(p).st_ctime_ns) for p in paths if p.exists()), default=0)


def past_tick(monkeypatch, *paths):
    monkeypatch.setattr(trusted_cache, '_now', lambda: newest(*paths) + POST_TIMESTAMP_TICK_NS + 1)


def test_an_unchanged_launch_is_read_once_and_each_caller_gets_its_own_copy(tmp_path, monkeypatch, reads):
    path = write(tmp_path, price={'input': 1.5})
    price = tmp_path / 'launch-price.json'
    past_tick(monkeypatch, path, price)
    first = Seat.launched(holder(tmp_path))
    second = Seat.launched(holder(tmp_path))
    assert reads == [path]
    assert first == second and first.price == {'input': 1.5} == pricing.saved_price(path)
    first.argv.append('changed by one caller')
    first.price['input'] = 99
    third = Seat.launched(holder(tmp_path))
    assert third.argv == ['program', '--flag'] and third.price == {'input': 1.5}
    assert reads == [path]


def test_the_binding_is_pricings_own_taken_from_the_one_read(tmp_path, monkeypatch, reads):
    path = write(tmp_path, price={'input': 2.0})
    info, raw = os.stat(path), path.read_bytes()
    assert seatmod._saved_price(tmp_path / 'launch-price.json', info, raw) == pricing.saved_price(path) == {'input': 2.0}
    path.write_text(json.dumps({**LAUNCH, 'pane': '%2'}))  # the price no longer binds this launch
    assert Seat.launched(holder(tmp_path)).price is None is pricing.saved_price(path)


SHAPES = Path(__file__).resolve().parents[1] / 'fixtures' / 'launch-shapes'


@pytest.mark.parametrize('program', ['pi', 'claude', 'opencode'])
@pytest.mark.parametrize('price', [None, {'input': 0.25, 'output': 1.25, 'currency': 'USD', 'unit': 'per-million'}])
def test_seats_binding_is_pricings_over_real_launch_shapes(tmp_path, program, price):
    """Real crew launch.json shapes (every string but pane, at, start and version replaced by x's of its length), with
    their price file written by pricing.save_price as qd writes it. If either form changes, this fails first."""
    path = tmp_path / 'launch.json'
    path.write_bytes((SHAPES / f'{program}-launch.json').read_bytes())
    pricing.save_price(path, price)
    info, raw = os.stat(path), path.read_bytes()
    assert seatmod._binding(info, raw) == pricing.binding(path)
    assert json.loads((tmp_path / 'launch-price.json').read_text())['binding'] == pricing.binding(path)
    assert seatmod._saved_price(tmp_path / 'launch-price.json', info, raw) == pricing.saved_price(path) == price
    os.utime(path)  # the launch rewritten in place: the price no longer binds it, by either form
    info, raw = os.stat(path), path.read_bytes()
    assert seatmod._binding(info, raw) == pricing.binding(path)
    assert seatmod._saved_price(tmp_path / 'launch-price.json', info, raw) is pricing.saved_price(path) is None


@pytest.mark.parametrize('change', ['rewrite', 'rename', 'price', 'price-appears', 'price-goes'])
def test_a_changed_launch_or_price_file_is_read_afresh(tmp_path, monkeypatch, reads, change):
    path = write(tmp_path, price=None if change == 'price-appears' else {'input': 1.0})
    price = tmp_path / 'launch-price.json'
    clock = [newest(path, price) + POST_TIMESTAMP_TICK_NS + 1]
    monkeypatch.setattr(trusted_cache, '_now', lambda: clock[0])
    before = Seat.launched(holder(tmp_path))
    if change == 'rewrite':
        path.write_text(json.dumps({**LAUNCH, 'pane': '%9'}))
    elif change == 'rename':  # a replace by rename: a new inode, the same size
        other = tmp_path / 'launch.tmp'
        other.write_text(json.dumps({**LAUNCH, 'pane': '%8'}))
        os.replace(other, path)
    elif change == 'price':
        pricing.save_price(path, {'input': 3.0})
    elif change == 'price-appears':
        pricing.save_price(path, {'input': 4.0})
    else:
        price.unlink()
    clock[0] = newest(path, price) + POST_TIMESTAMP_TICK_NS + 1
    after = Seat.launched(holder(tmp_path))
    assert reads == [path, path], 'a changed stamp is a fresh read'
    assert after == Seat.launched(holder(tmp_path)) and reads == [path, path], 'and that one is kept in its turn'
    expected = {'rewrite': ('%9', None), 'rename': ('%8', None), 'price': ('%1', {'input': 3.0}),
                'price-appears': ('%1', {'input': 4.0}), 'price-goes': ('%1', None)}[change]
    assert (after.pane, after.price) == expected != (before.pane, before.price)


@pytest.mark.parametrize('age', [0, POST_TIMESTAMP_TICK_NS - 1, POST_TIMESTAMP_TICK_NS])
def test_a_stamp_inside_the_tick_is_never_kept(tmp_path, monkeypatch, reads, age):
    path = write(tmp_path, price={'input': 1.0})
    price = tmp_path / 'launch-price.json'
    monkeypatch.setattr(trusted_cache, '_now', lambda: newest(path, price) + age)
    Seat.launched(holder(tmp_path))
    Seat.launched(holder(tmp_path))
    assert reads == [path, path] and str(path) not in seatmod._LAUNCHES
    past_tick(monkeypatch, path, price)
    Seat.launched(holder(tmp_path))
    Seat.launched(holder(tmp_path))
    assert len(reads) == 3


def test_a_removed_launch_drops_its_entry_and_reads_as_none(tmp_path, monkeypatch, reads):
    path = write(tmp_path)
    past_tick(monkeypatch, path)
    Seat.launched(holder(tmp_path))
    assert str(path) in seatmod._LAUNCHES
    path.unlink()
    assert Seat.launched(holder(tmp_path)) is None
    assert str(path) not in seatmod._LAUNCHES


@pytest.mark.parametrize('content', ['{', '[]', b'\xff\xfe'])
def test_an_unreadable_launch_is_refused_as_before_and_never_kept(tmp_path, monkeypatch, reads, content):
    path = tmp_path / 'launch.json'
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    past_tick(monkeypatch, path)
    for _ in range(2):
        with pytest.raises(LaunchUnreadable):
            Seat.launched(holder(tmp_path))
    assert len(reads) == 2 and str(path) not in seatmod._LAUNCHES


# -- the read-only record reader answer ------------------------------------------------------------------------------

@pytest.fixture
def lookups(monkeypatch):
    """Every call that reaches record_owner.lookup_reader, counted; the kept answers start and end empty."""
    from quarterdeck import record_owner
    seatmod._LOOKUPS.clear()
    seatmod._LAUNCHES.clear()
    calls = []
    real = record_owner.lookup_reader

    def counting(seat, record):
        calls.append(Path(record))
        return real(seat, record)

    monkeypatch.setattr(record_owner, 'lookup_reader', counting)
    yield calls
    seatmod._LOOKUPS.clear()
    seatmod._LAUNCHES.clear()


def owning_seat(tmp_path):
    """A seat whose trusted folder registers its record as the fake program's, with a pin and an owned list."""
    from quarterdeck import record_owner, trusted_files
    trusted, home = tmp_path / 'trusted', tmp_path / 'home'
    trusted.mkdir()
    home.mkdir()
    record = home / 'record.jsonl'
    record.write_text('')
    (trusted / 'launch.json').write_text(json.dumps(LAUNCH))
    (trusted / trusted_files.RECORD).write_text(str(record) + '\n')
    (trusted / trusted_files.LAUNCHES).write_text(json.dumps({'record': str(record), 'harness': 'fake'}) + '\n')
    record_owner.register(trusted, record, 'fake')
    (home / 'conversations.owned').write_text(str(record) + '\n')
    seat = SimpleNamespace(trusted=trusted, home=home, folder=tmp_path, harness=SimpleNamespace(home_variable='HOME'))
    seat.launched = lambda: Seat.launched(seat)
    return seat, record


def every_file(seat):
    from quarterdeck import record_owner, trusted_files
    return [record_owner.registry(seat.trusted), seat.trusted / trusted_files.LAUNCHES,
            seat.trusted / trusted_files.RECORD, seat.trusted / 'launch.json', seat.home / 'conversations.owned']


def test_an_unchanged_reader_answer_is_read_once(tmp_path, monkeypatch, lookups):
    seat, record = owning_seat(tmp_path)
    past_tick(monkeypatch, *every_file(seat))
    assert Seat._lookup_reader(seat, record) == 'fake'
    assert Seat._lookup_reader(seat, record) == 'fake'
    assert lookups == [record]
    other = record.with_name('other.jsonl')
    other.write_text('')
    assert Seat._lookup_reader(seat, other) is None  # another record is another question
    assert lookups == [record, other]


@pytest.mark.parametrize('change', ['registry', 'launches', 'pin', 'launch', 'unconfirmed', 'owned', 'owned-link'])
def test_a_change_to_any_file_it_reads_is_read_afresh(tmp_path, monkeypatch, lookups, change):
    from quarterdeck import record_owner, seat_migration, trusted_files
    seat, record = owning_seat(tmp_path)
    clock = [0]
    monkeypatch.setattr(trusted_cache, '_now', lambda: clock[0])
    paths = [*every_file(seat), seat.trusted / seat_migration.UNCONFIRMED]
    clock[0] = newest(*paths) + POST_TIMESTAMP_TICK_NS + 1
    Seat._lookup_reader(seat, record)
    if change == 'registry':
        record_owner.register(seat.trusted, record.with_name('more.jsonl'), 'fake')
    elif change == 'launches':
        with open(seat.trusted / trusted_files.LAUNCHES, 'a') as stream:
            stream.write(json.dumps({'record': str(record), 'harness': 'fake'}) + '\n')
    elif change == 'pin':
        (seat.trusted / trusted_files.RECORD).write_text(str(record) + '\n\n')
    elif change == 'launch':
        (seat.trusted / 'launch.json').write_text(json.dumps({**LAUNCH, 'pane': '%5'}))
    elif change == 'unconfirmed':
        (seat.trusted / seat_migration.UNCONFIRMED).write_text('{}')
    elif change == 'owned':
        (seat.home / 'conversations.owned').write_text(str(record) + '\nmore\n')
    else:  # the same content behind a link: the name changed, though what it points at reads the same
        target = tmp_path / 'elsewhere-owned'
        target.write_text(str(record) + '\n')
        (seat.home / 'conversations.owned').unlink()
        (seat.home / 'conversations.owned').symlink_to(target)
    clock[0] = newest(*paths) + POST_TIMESTAMP_TICK_NS + 1
    try:
        Seat._lookup_reader(seat, record)
    except LaunchUnreadable:
        assert change == 'unconfirmed'  # the hold refuses as launched() refuses, and is never kept
    assert lookups == [record, record]


@pytest.mark.parametrize('age', [0, POST_TIMESTAMP_TICK_NS - 1, POST_TIMESTAMP_TICK_NS])
def test_a_reader_answer_inside_the_tick_is_never_kept(tmp_path, monkeypatch, lookups, age):
    seat, record = owning_seat(tmp_path)
    monkeypatch.setattr(trusted_cache, '_now', lambda: newest(*every_file(seat)) + age)
    Seat._lookup_reader(seat, record)
    Seat._lookup_reader(seat, record)
    assert lookups == [record, record] and str(seat.trusted) not in seatmod._LOOKUPS


def test_a_removed_file_is_a_change_and_a_refusal_is_never_kept(tmp_path, monkeypatch, lookups):
    from quarterdeck import record_owner, trusted_files
    seat, record = owning_seat(tmp_path)
    past_tick(monkeypatch, *every_file(seat))
    Seat._lookup_reader(seat, record)
    (seat.trusted / trusted_files.LAUNCHES).unlink()
    assert Seat._lookup_reader(seat, record) == 'fake'  # the registry still names it
    assert lookups == [record, record]
    record_owner.registry(seat.trusted).write_text('not json\n')
    past_tick(monkeypatch, *every_file(seat))
    for _ in range(2):
        with pytest.raises(Exception):
            Seat._lookup_reader(seat, record)
    assert len(lookups) == 4
