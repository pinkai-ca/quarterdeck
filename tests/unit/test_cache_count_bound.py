"""The record cache is bounded by its count of entries, with record bytes only a safety ceiling, and a sweep of many
records (the seat-token fallback) passes through it without pushing the working set out. Synthetic records only."""
import pytest

from quarterdeck.harness import claude, json_record
from quarterdeck.harness.synthetic_record import ClaudeShape


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.setattr(json_record, '_KEEP_ROWS', False)
    json_record._snapshots.clear()
    yield
    json_record._snapshots.clear()


def records(tmp_path, count, prefix='seat'):
    made = []
    for index in range(count):
        record = ClaudeShape(tmp_path / f'{prefix}-{index}', tmp_path, 'synthetic', 'synthetic')
        record.begin(f'Synthetic input {index}', 'synthetic', 'low')
        record.end('Finished')
        made.append(record.path)
    return made


def held():
    return list(json_record._snapshots)


def test_a_sweep_of_many_records_leaves_the_working_set_held(tmp_path, monkeypatch):
    monkeypatch.setattr(json_record, '_CACHE_COUNT', 4)
    working = records(tmp_path, 3, 'working')
    for path in working:
        claude.read(path)
    swept = records(tmp_path, 12, 'old')
    with json_record.passing():
        for path in swept + working[:1]:
            claude.read(path)  # each read whole, none admitted; a member read in passing stays a member
    assert set(held()) == set(working), 'the sweep admitted nothing and evicted nothing'
    claude.read(swept[0])
    assert swept[0] in held(), 'outside the sweep the cache admits as before'


def test_the_count_bound_evicts_the_least_recent(tmp_path, monkeypatch):
    monkeypatch.setattr(json_record, '_CACHE_COUNT', 3)
    one, two, three, four = records(tmp_path, 4)
    for path in (one, two, three):
        claude.read(path)
    claude.inputs(one)  # used again: now the most recent
    claude.read(four)
    assert held() == [three, one, four] or set(held()) == {three, one, four}
    assert two not in held(), 'the least recently used went'


def test_the_byte_ceiling_still_holds(tmp_path, monkeypatch):
    one, two = records(tmp_path, 2)
    monkeypatch.setattr(json_record, '_CACHE_BYTES', one.stat().st_size + two.stat().st_size - 1)
    claude.read(one)
    claude.read(two)
    assert held() == [two]


def test_the_bounds_are_as_stated():
    assert json_record._CACHE_COUNT == 96
    assert json_record._CACHE_BYTES_CEILING == 768 * 1024 * 1024


def test_the_timing_line_carries_the_count_and_the_bytes():
    from quarterdeck import service_timing
    timings = service_timing.Timings()
    timings.cache_reading(9, 100)
    timings.cache_reading(63, 90)
    logged = []
    timings.since -= service_timing.INTERVAL
    timings.publish(lambda event, **data: logged.append(data))
    assert logged[0]['parse_cache'] == {'records': 63, 'record_bytes': 90, 'max_record_bytes': 100, 'max_records': 63}


def test_the_seat_token_fallback_reads_in_passing(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from quarterdeck import ledger, record_owner, watch
    seen = []

    class Item:
        def read(self):
            seen.append(json_record._PASSING.get())
            return SimpleNamespace(turns=0, turn_open=False), []
    monkeypatch.setattr(ledger.Ledger, 'reading', staticmethod(lambda path: None))
    monkeypatch.setattr(record_owner, 'records', lambda seat: [Item(), Item()])
    seat = SimpleNamespace(ship=SimpleNamespace(runtime=tmp_path))
    monkeypatch.setattr(watch, 'UnresolvedSeat', type('Never', (), {}))
    watch.tokens_today(seat)
    assert seen == [True, True]
