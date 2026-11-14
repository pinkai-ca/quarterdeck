"""Independent gates for the reader registry's pass-scoped membership (record_owner, Service.account).

Synthetic seats, records and registries only. Registry parses, write-mode opens and locks are counted
at the boundaries the change names: ``record_owner.events``, ``Path.open`` and the ship's registry lock
(``ship_locks.held``, under runtime/locks). A competing writer is a second open file description appending
under its own real ``flock``, which is what another process does; no second interpreter is started.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import attribution_cache, record_owner, service, ship_locks, turn_failure
from quarterdeck.harness.base import RecordError, State, Tokens, Turn
from quarterdeck.harness.fake import FakeHarness
from quarterdeck.service import Service
from quarterdeck.ship import Ship

SEAT = 'synthetic-seat-q'
LETTERS = 'ghjkmnpqrstuvwxyz'  # record names stay clear of hexadecimal
WRITE_MODES = set('wax+')
AT = datetime(2026, 1, 1, tzinfo=timezone.utc)


class Spy:
    """Counts registry parses by the first framework caller, and registry write-mode opens and locks."""

    def __init__(self, monkeypatch):
        self.parses: list[tuple[str | None, Path]] = []
        self.writes: list[tuple[Path, str]] = []
        self.locks: list[tuple[Path, int]] = []
        self.real_events, self.real_open, self.real_flock = record_owner.events, Path.open, fcntl.flock
        self.real_held = ship_locks.held
        self.before_lock: list = []  # run as the registry's lock is taken: another writer's turn

        def counted_events(path):
            if Path(path).name == record_owner.REGISTRY:
                self.parses.append((caller(), Path(path)))
            return self.real_events(path)

        def counted_open(path, mode='r', *args, **kwargs):
            if path.name == record_owner.REGISTRY and set(mode) & WRITE_MODES:
                self.writes.append((path, mode))
            return self.real_open(path, mode, *args, **kwargs)

        def counted_held(lock, what, **options):
            if Path(lock).name.endswith(f'--{record_owner.REGISTRY}.lock'):
                self.locks.append((Path(lock), fcntl.LOCK_SH if options.get('shared') else fcntl.LOCK_EX))
                for hook in self.before_lock:
                    hook()
            return self.real_held(lock, what, **options)

        monkeypatch.setattr(record_owner, 'events', counted_events)
        monkeypatch.setattr(Path, 'open', counted_open)
        monkeypatch.setattr(ship_locks, 'held', counted_held)

    def ownership(self, path: Path) -> int:
        """Parses of one registry made by the ownership path itself (bindings/register)."""
        return sum(1 for module, parsed in self.parses if module == 'quarterdeck.record_owner' and parsed == path)

    def total(self, path: Path) -> int:
        """Every parse of one registry, whichever framework path asked for it."""
        return sum(1 for _, parsed in self.parses if parsed == path)


def caller() -> str | None:
    frame = sys._getframe(2)
    while frame is not None and not frame.f_globals.get('__name__', '').startswith('quarterdeck'):
        frame = frame.f_back
    return frame.f_globals['__name__'] if frame is not None else None


def make_seat(seats: Path, name: str = SEAT) -> SimpleNamespace:
    folder = seats / name
    home = folder / 'home'
    home.mkdir(parents=True)
    return SimpleNamespace(name=name, home=home, folder=folder, harness=FakeHarness(),
                           spec=SimpleNamespace(harness='fake'), launched=lambda: None)


def touch(seat, letter: str) -> Path:
    path = seat.home / f'record-{letter}'
    path.write_text(f'synthetic record {letter}\n')
    return path


def write_rows(folder: Path, rows) -> Path:
    registry = record_owner.registry(folder)
    registry.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    return registry


def rows_of(registry: Path) -> list[dict]:
    return [json.loads(line) for line in registry.read_text().splitlines() if line.strip()]


def append_as_another_writer(registry: Path, row: dict, real_flock) -> None:
    """Another writer: its own open file description, under its own exclusive lock."""
    with open(registry, 'a') as other:
        real_flock(other, fcntl.LOCK_EX)
        other.write(json.dumps(row) + '\n')


def summary(owned, seat):
    return [(item.path, item.name, item.reader is seat.harness, item.reason) for item in owned]


def typed_seat(tmp_path, count=5):
    seat = make_seat(tmp_path / 'runtime' / 'seats')
    typed = [touch(seat, letter) for letter in LETTERS[:count]]
    unknown = touch(seat, LETTERS[count])
    registry = write_rows(seat.folder, [*({'record': str(path), 'harness': 'fake'} for path in typed),
                                        {'record': str(unknown), 'harness': None}])
    return seat, typed, unknown, registry


# -- one pass, nothing new -------------------------------------------------------------------------------

def test_an_unchanged_registry_is_parsed_once_per_pass_and_never_opened_for_writing(tmp_path, monkeypatch):
    seat, typed, unknown, registry = typed_seat(tmp_path)
    (seat.folder / 'record').write_text(f'{typed[0]}\n')  # an ordinary joined pin
    before = registry.read_bytes()
    expected = summary(record_owner.records(seat), seat)
    assert {(path, name) for path, name, _, _ in expected} == {*((path, 'fake') for path in typed), (unknown, None)}
    spy = Spy(monkeypatch)

    with record_owner.reader_pass():
        first = summary(record_owner.records(seat), seat)
        pinned = record_owner.pinned_reader(seat, typed[1])
        untyped = record_owner.pinned_reader(seat, unknown)
        again = summary(record_owner.records(seat), seat)

    assert first == again == expected
    assert pinned == 'fake' and untyped is None, 'an untyped row stays unknown; nothing is guessed'
    assert spy.ownership(registry) == 1, 'records, pinned_reader and every register share one parse'
    assert spy.writes == [] and spy.locks == []
    assert registry.read_bytes() == before

    with record_owner.reader_pass():
        record_owner.records(seat)
    assert spy.ownership(registry) == 2, 'a later pass parses anew'
    assert spy.writes == [] and spy.locks == []


def test_outside_an_account_pass_every_lookup_reads_the_registry_fresh(tmp_path, monkeypatch):
    seat, typed, unknown, registry = typed_seat(tmp_path)
    spy = Spy(monkeypatch)
    record_owner.bindings(seat)
    record_owner.bindings(seat)
    assert spy.ownership(registry) == 2
    owned = record_owner.records(seat)
    assert spy.ownership(registry) >= 2 + 1 + len(owned), 'bindings and every register read fresh'
    assert spy.writes == [] and spy.locks == [], 'an existing row needs only read access, here too'


# -- lifetime ------------------------------------------------------------------------------------------------

def test_the_pass_ends_on_failure_and_a_nested_pass_restores_its_enclosing_one(tmp_path, monkeypatch):
    seat, typed, unknown, registry = typed_seat(tmp_path)
    spy = Spy(monkeypatch)
    with pytest.raises(RuntimeError, match='synthetic failure q'):
        with record_owner.reader_pass():
            record_owner.bindings(seat)
            raise RuntimeError('synthetic failure q')
    assert spy.ownership(registry) == 1
    record_owner.bindings(seat)
    assert spy.ownership(registry) == 2, 'nothing of a failed pass is reused afterwards'

    with record_owner.reader_pass():
        record_owner.bindings(seat)                   # the outer pass parses
        with record_owner.reader_pass():
            record_owner.bindings(seat)               # the inner pass has its own membership
            record_owner.bindings(seat)
        record_owner.bindings(seat)                   # the outer membership is back
    assert spy.ownership(registry) == 4


# -- new rows ------------------------------------------------------------------------------------------------

def test_a_new_row_is_appended_once_under_the_lock_and_seen_by_the_same_pass(tmp_path, monkeypatch):
    seat, typed, unknown, registry = typed_seat(tmp_path)
    fresh = touch(seat, 'z')
    spy = Spy(monkeypatch)
    with record_owner.reader_pass():
        assert fresh not in record_owner.bindings(seat)
        record_owner.register(seat.folder, fresh, 'fake')
        assert len(spy.writes) == 1 and [op for _, op in spy.locks] == [fcntl.LOCK_EX]
        record_owner.register(seat.folder, fresh, 'fake')
        assert record_owner.bindings(seat)[fresh] == {'fake'}
    assert len(spy.writes) == 1 and len(spy.locks) == 1, 'the same row a second time is a read-only no-op'
    assert rows_of(registry).count({'record': str(fresh), 'harness': 'fake'}) == 1


def test_the_same_row_appended_by_another_writer_after_the_check_is_not_appended_again(tmp_path, monkeypatch):
    seat, typed, unknown, registry = typed_seat(tmp_path)
    fresh = touch(seat, 'z')
    spy = Spy(monkeypatch)
    raced = []

    def another_writer_first():
        if not raced:
            raced.append(True)  # same row, other key order: equality is by whole row, not by bytes
            append_as_another_writer(registry, {'harness': 'fake', 'record': str(fresh)}, spy.real_flock)

    spy.before_lock.append(another_writer_first)
    before = len(rows_of(registry))
    with record_owner.reader_pass():
        record_owner.bindings(seat)
        record_owner.register(seat.folder, fresh, 'fake')
        after = record_owner.bindings(seat)
    assert raced, 'the other writer ran between the read-only check and the lock'
    assert len(spy.writes) == 1 and len(spy.locks) == 1
    rows = rows_of(registry)
    assert len(rows) == before + 1 and rows.count({'record': str(fresh), 'harness': 'fake'}) == 1
    assert after[fresh] == {'fake'}


def test_a_conflicting_row_from_another_writer_is_kept_and_seen_and_nothing_is_guessed(tmp_path, monkeypatch):
    seat, typed, unknown, registry = typed_seat(tmp_path)
    fresh = touch(seat, 'z')
    conflict = {'record': str(typed[0]), 'harness': 'codex'}
    spy = Spy(monkeypatch)
    raced = []

    def another_writer_first():
        if not raced:
            raced.append(True)
            append_as_another_writer(registry, conflict, spy.real_flock)

    spy.before_lock.append(another_writer_first)
    with record_owner.reader_pass():
        assert record_owner.pinned_reader(seat, typed[0]) == 'fake'
        record_owner.register(seat.folder, fresh, 'fake')
        assert record_owner.bindings(seat)[typed[0]] == {'fake', 'codex'}
        assert record_owner.pinned_reader(seat, typed[0]) is None
        owned = {item.path: item for item in record_owner.records(seat)}
    assert raced
    assert owned[typed[0]].name is None and owned[typed[0]].reader is None and owned[typed[0]].reason
    rows = rows_of(registry)
    assert rows.count(conflict) == 1 and rows.count({'record': str(fresh), 'harness': 'fake'}) == 1


# -- changed registries and failures ---------------------------------------------------------------------

def test_a_registry_changed_during_a_pass_is_reread_before_its_next_use(tmp_path, monkeypatch):
    seat, typed, unknown, registry = typed_seat(tmp_path)
    spy = Spy(monkeypatch)
    with record_owner.reader_pass():
        assert record_owner.pinned_reader(seat, typed[0]) == 'fake'
        append_as_another_writer(registry, {'record': str(typed[0]), 'harness': 'codex'}, spy.real_flock)
        assert record_owner.pinned_reader(seat, typed[0]) is None
        assert record_owner.bindings(seat)[typed[0]] == {'fake', 'codex'}
    assert spy.ownership(registry) == 2, 'reread once after the change, then reused'
    assert spy.writes == [] and spy.locks == []


def test_an_unreadable_registry_is_never_answered_from_an_earlier_reading(tmp_path, monkeypatch):
    seat, typed, unknown, registry = typed_seat(tmp_path)
    good = registry.read_text()
    Spy(monkeypatch)
    with record_owner.reader_pass():
        assert record_owner.bindings(seat)[typed[0]] == {'fake'}
        registry.write_text('{\n')
        with pytest.raises(RecordError):
            record_owner.bindings(seat)
        owned = record_owner.records(seat)
        assert [(item.path, item.name, item.reader) for item in owned] == [(registry, None, None)]
        assert 'unreadable record ownership' in owned[0].reason
        registry.write_text(good + json.dumps({'record': str(typed[1]), 'harness': 'codex'}) + '\n')
        found = record_owner.bindings(seat)
    assert found[typed[0]] == {'fake'} and found[typed[1]] == {'fake', 'codex'}


def test_a_registry_that_changes_while_it_is_parsed_is_not_reused(tmp_path, monkeypatch):
    seat, typed, unknown, registry = typed_seat(tmp_path)
    spy = Spy(monkeypatch)
    counted, raced = record_owner.events, []

    def appended_during_the_read(path):
        rows = counted(path)
        if Path(path) == registry and not raced:
            raced.append(True)
            append_as_another_writer(registry, {'record': str(typed[0]), 'harness': 'codex'}, spy.real_flock)
        return rows

    monkeypatch.setattr(record_owner, 'events', appended_during_the_read)
    with record_owner.reader_pass():
        assert record_owner.bindings(seat)[typed[0]] == {'fake'}, 'the read itself keeps its ordinary result'
        assert record_owner.bindings(seat)[typed[0]] == {'fake', 'codex'}
        assert record_owner.bindings(seat)[typed[0]] == {'fake', 'codex'}
    assert spy.ownership(registry) == 2, 'reread once after the unstable read, then reused'


def test_a_failed_metadata_check_grants_no_reuse(tmp_path, monkeypatch):
    # Coupled to the change's private stamp helper: the metadata-failure path has no public seam.
    seat, typed, unknown, registry = typed_seat(tmp_path)
    spy = Spy(monkeypatch)

    def refused(path):
        raise PermissionError('synthetic metadata refusal q')

    monkeypatch.setattr(record_owner, '_registry_stamp', refused)
    with record_owner.reader_pass():
        first = record_owner.bindings(seat)
        second = record_owner.bindings(seat)
    assert first == second and first[typed[0]] == {'fake'}
    assert spy.ownership(registry) == 2


def test_a_failed_append_leaves_no_reading_behind(tmp_path, monkeypatch):
    seat, typed, unknown, registry = typed_seat(tmp_path)
    fresh = touch(seat, 'z')
    spy = Spy(monkeypatch)
    spied = Path.open

    def refuse_writes(path, mode='r', *args, **kwargs):
        if path.name == record_owner.REGISTRY and set(mode) & WRITE_MODES:
            raise PermissionError('synthetic write refusal q')
        return spied(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', refuse_writes)
    with record_owner.reader_pass():
        record_owner.bindings(seat)
        with pytest.raises(PermissionError):
            record_owner.register(seat.folder, fresh, 'fake')
        parsed = spy.ownership(registry)
        assert fresh not in record_owner.bindings(seat)
        assert spy.ownership(registry) == parsed + 1, 'a lookup after a write attempt reads again'
    assert {'record': str(fresh), 'harness': 'fake'} not in rows_of(registry)


# -- the same rows as before, in a pass or not ---------------------------------------------------------

ROW_SHAPES = [
    ('absent registry and folder', None, [('k', 'fake')]),
    ('empty registry', [], [('k', 'fake')]),
    ('new known reader', [('g', 'fake')], [('k', 'fake')]),
    ('new untyped row beside a typed one', [('g', 'fake')], [('g', None)]),
    ('exact existing rows', [('g', 'fake'), ('m', None)], [('g', 'fake'), ('m', None)]),
    ('extra fields only', [('g', 'fake', {'note': 'synthetic-q'})], [('g', 'fake')]),
    ('conflicting evidence', [('g', 'codex')], [('g', 'fake')]),
    ('the same new row twice', [], [('k', 'fake'), ('k', 'fake')]),
]


def shaped_row(home: Path, shape) -> dict:
    letter, harness, *extra = shape
    return {'record': str(home / f'record-{letter}'), 'harness': harness, **(extra[0] if extra else {})}


@pytest.mark.parametrize('initial, calls', [case[1:] for case in ROW_SHAPES], ids=[case[0] for case in ROW_SHAPES])
def test_register_writes_exactly_the_rows_it_wrote_before_inside_and_outside_a_pass(tmp_path, monkeypatch, initial, calls):
    spy = Spy(monkeypatch)
    outcomes = []
    for in_pass in (False, True):
        root = tmp_path / ('in-pass' if in_pass else 'outside')
        folder = root / 'seats' / SEAT
        home = folder / 'home'
        start_rows = None if initial is None else [shaped_row(home, shape) for shape in initial]
        if start_rows is not None:
            folder.mkdir(parents=True)
            write_rows(folder, start_rows)
        expected = list(start_rows or [])
        for letter, harness in calls:
            row = {'record': str(home / f'record-{letter}'), 'harness': harness}
            if row not in expected:
                expected.append(row)
        writes = len(spy.writes)
        with (record_owner.reader_pass() if in_pass else contextlib.nullcontext()):
            for letter, harness in calls:
                record_owner.register(folder, home / f'record-{letter}', harness)
        registry = record_owner.registry(folder)
        assert rows_of(registry) == expected
        appended = len(expected) - len(start_rows or [])
        assert len(spy.writes) - writes == appended, 'a write-mode open only for a genuinely new row'
        outcomes.append((registry.read_text().replace(str(root), '<root>'), len(spy.writes) - writes))
    assert outcomes[0] == outcomes[1]


# -- the service's account pass ------------------------------------------------------------------------

def turn(index):
    return Turn(index, AT + timedelta(seconds=10 * index), AT + timedelta(seconds=10 * index + 5),
                'synthetic input q', 'synthetic reply q', 'synthetic-model-q', Tokens(1, 2, 3, 4), cost=0.25)


def accounting(tmp_path, monkeypatch, names=('synthetic-seat-q', 'synthetic-seat-w')):
    ship = Ship(tmp_path, 'synthetic', 'operator', tmp_path, {}, {}, {})
    svc = Service(ship, SimpleNamespace())
    seats, typed = [], {}
    for name in names:
        seat = make_seat(ship.runtime / 'seats', name)
        paths = [touch(seat, letter) for letter in LETTERS[:5]]
        unknown = touch(seat, LETTERS[5])
        write_rows(seat.folder, [*({'record': str(path), 'harness': 'fake'} for path in paths),
                                 {'record': str(unknown), 'harness': None}])
        (seat.folder / 'record').write_text(f'{paths[0]}\n')
        (seat.folder / 'record-launches.jsonl').write_text(json.dumps(dict(
            record=str(paths[0]), at=(AT - timedelta(minutes=1)).isoformat(), pool='synthetic-pool-q', harness='fake')) + '\n')
        monkeypatch.setattr(seat.harness, 'read', lambda path: (State(turns=1, turn_open=False), [turn(0)]))
        seat.session, seat.pane, seat.record = SimpleNamespace(human=False), (lambda: None), (lambda first=paths[0]: first)
        seats.append(seat)
        typed[seat.name] = paths
    logs = []
    monkeypatch.setattr(service.machine, 'processes', lambda: {})
    monkeypatch.setattr(svc, 'resolved_seats', lambda: list(seats))
    monkeypatch.setattr(svc, 'log', lambda event, **data: logs.append((event, data)))
    monkeypatch.setattr(FakeHarness, 'attribution_inputs', lambda self, path: [])
    monkeypatch.setattr(turn_failure, 'publish', lambda *args: None)
    return SimpleNamespace(svc=svc, seats=seats, typed=typed, logs=logs)


def test_each_account_pass_parses_every_seats_registry_once_and_writes_nothing_unchanged(tmp_path, monkeypatch):
    w = accounting(tmp_path, monkeypatch)
    spy = Spy(monkeypatch)
    try:
        w.svc.account()
        for seat in w.seats:
            assert spy.ownership(record_owner.registry(seat.folder)) == 1
            for path in w.typed[seat.name]:
                assert w.svc.ledger.ingested(path) == 1
        assert spy.writes == [] and spy.locks == []
        w.svc.account()
        for seat in w.seats:
            assert spy.ownership(record_owner.registry(seat.folder)) == 2, 'no membership outlives its pass'
        assert spy.writes == [] and spy.locks == []
    finally:
        w.svc.ledger.close()


def test_an_account_pass_appends_a_genuinely_new_owned_record_once_and_keeps_it_unknown(tmp_path, monkeypatch):
    w = accounting(tmp_path, monkeypatch, names=('synthetic-seat-q',))
    seat = w.seats[0]
    registry = record_owner.registry(seat.folder)
    fresh = touch(seat, 'z')
    (seat.home / 'conversations.owned').write_text(f'{fresh}\n')
    spy = Spy(monkeypatch)
    try:
        w.svc.account()
        assert rows_of(registry).count({'record': str(fresh), 'harness': None}) == 1
        assert len(spy.writes) == 1 and len(spy.locks) == 1
        assert str(fresh) in [row[0] for row in w.svc.ledger.incomplete_records()], 'an unknown reader stays visible'
        w.svc.account()
        assert len(spy.writes) == 1 and len(spy.locks) == 1
        assert rows_of(registry).count({'record': str(fresh), 'harness': None}) == 1
    finally:
        w.svc.ledger.close()


def test_an_account_pass_that_fails_leaves_no_membership_behind(tmp_path, monkeypatch):
    w = accounting(tmp_path, monkeypatch, names=('synthetic-seat-q',))
    seat = w.seats[0]
    registry = record_owner.registry(seat.folder)

    def broken(path):
        raise RuntimeError('synthetic reader failure q')

    monkeypatch.setattr(seat.harness, 'read', broken)
    spy = Spy(monkeypatch)
    try:
        w.svc.account()  # the pass fails; the service counts it rather than raising
        assert (w.svc.accounting_health.failures, w.svc.accounting_health.current) == (1, 'accounting exception')
        parsed = spy.ownership(registry)
        assert parsed == 1
        record_owner.bindings(seat)
        # Outside a pass both bindings and the register of the fixture's pin read fresh: nothing is reused.
        assert spy.ownership(registry) == parsed + 2
    finally:
        w.svc.ledger.close()


# -- the whole account pass: ownership and attribution replay share one reading ------------------------

def replays(monkeypatch, before=None):
    """Counts attribution replays; ``before`` runs between the ownership reads and the replay."""
    calls, real = [], attribution_cache.Attribution.replay

    def counted(self, store, runtime, bindings=None, **kwargs):
        calls.append(bindings)
        if before is not None:
            before()
        return real(self, store, runtime, bindings, **kwargs)

    monkeypatch.setattr(attribution_cache.Attribution, 'replay', counted)
    return calls


def replay_readers(w) -> dict[str, str | None]:
    return {record: source[0][0] for record, source in w.svc.ledger.attributions.sources.items()}


def untyped(seat) -> Path:
    return seat.home / f'record-{LETTERS[5]}'


def totals(spy, w) -> dict[str, int]:
    return {seat.name: spy.total(record_owner.registry(seat.folder)) for seat in w.seats}


def test_a_whole_unchanged_account_pass_parses_each_registry_once_for_ownership_and_replay(tmp_path, monkeypatch):
    w = accounting(tmp_path, monkeypatch)
    spy = Spy(monkeypatch)
    calls = replays(monkeypatch)
    try:
        w.svc.account()
        assert calls == [None], 'the replay ran once and enumerated the seat folders itself'
        assert totals(spy, w) == {seat.name: 1 for seat in w.seats}, 'ownership and replay share one parse; two fail'
        assert spy.writes == [] and spy.locks == []
        readers = replay_readers(w)
        for seat in w.seats:
            # Only the registry names record-h.. record-m; the launch journal names record-g alone.
            assert all(readers[str(path)] == 'fake' for path in w.typed[seat.name]), 'the replay used the registry reading'
            assert readers.get(str(untyped(seat))) is None, 'an untyped row gives the replay no reader'
        w.svc.account()
        assert len(calls) == 2
        assert totals(spy, w) == {seat.name: 2 for seat in w.seats}, 'a later pass parses anew, once'
        assert spy.writes == [] and spy.locks == []
        assert replay_readers(w) == readers, 'the second replay read the registry too'
    finally:
        w.svc.ledger.close()


def test_a_retired_seats_registry_is_still_read_by_the_replay_once_and_its_evidence_kept(tmp_path, monkeypatch):
    w = accounting(tmp_path, monkeypatch)
    kept, retired = w.seats
    spy = Spy(monkeypatch)
    replays(monkeypatch)
    try:
        w.svc.account()
        assert totals(spy, w) == {kept.name: 1, retired.name: 1}
        monkeypatch.setattr(w.svc, 'resolved_seats', lambda: [kept])
        w.svc.account()
        assert totals(spy, w) == {kept.name: 2, retired.name: 2}, 'the retired registry: one replay read, no more'
        assert spy.ownership(record_owner.registry(retired.folder)) == 2, 'parsed through the shared observation'
        assert spy.writes == [] and spy.locks == []
        readers = replay_readers(w)
        assert all(readers[str(path)] == 'fake' for path in w.typed[retired.name]), 'retired evidence is not skipped'
    finally:
        w.svc.ledger.close()


def test_a_registry_changed_between_ownership_and_replay_is_reread_by_the_replay(tmp_path, monkeypatch):
    w = accounting(tmp_path, monkeypatch)
    changed, other = w.seats
    registry = record_owner.registry(changed.folder)
    conflicted = w.typed[changed.name][1]
    spy = Spy(monkeypatch)
    calls = replays(monkeypatch, before=lambda: append_as_another_writer(
        registry, {'record': str(conflicted), 'harness': 'codex'}, spy.real_flock))
    try:
        w.svc.account()
        assert len(calls) == 1
        assert totals(spy, w) == {changed.name: 2, other.name: 1}, 'an honest reread of the changed registry only'
        assert spy.writes == [] and spy.locks == []
        readers = replay_readers(w)
        assert readers[str(conflicted)] is None, 'the replay saw the conflicting row; nothing stale, nothing guessed'
        assert all(readers[str(path)] == 'fake' for path in w.typed[changed.name] if path != conflicted)
        assert all(readers[str(path)] == 'fake' for path in w.typed[other.name])
    finally:
        w.svc.ledger.close()


def test_a_row_appended_during_the_pass_is_seen_by_the_replay_without_another_parse(tmp_path, monkeypatch):
    w = accounting(tmp_path, monkeypatch, names=('synthetic-seat-q',))
    seat = w.seats[0]
    registry = record_owner.registry(seat.folder)
    fresh = touch(seat, 'z')
    (seat.folder / 'record').write_text(f'{fresh}\n')  # a pin no registry or launch-journal row names yet
    real = record_owner.launch_reader
    monkeypatch.setattr(record_owner, 'launch_reader', lambda launched: 'fake' if launched is None else real(launched))
    spy = Spy(monkeypatch)
    calls = replays(monkeypatch)
    try:
        w.svc.account()
        assert len(calls) == 1
        assert rows_of(registry).count({'record': str(fresh), 'harness': 'fake'}) == 1
        assert len(spy.writes) == 1 and [op for _, op in spy.locks] == [fcntl.LOCK_EX]
        # The ownership read, the recheck under the lock, one read after the append; the replay shares the last.
        assert spy.total(registry) == 3
        assert w.svc.ledger.ingested(fresh) == 1
        assert replay_readers(w)[str(fresh)] == 'fake', 'only the appended registry row names this reader'
        w.svc.account()
        assert spy.total(registry) == 4, 'the next pass is back to one parse'
        assert len(spy.writes) == 1 and len(spy.locks) == 1
        assert replay_readers(w)[str(fresh)] == 'fake'
    finally:
        w.svc.ledger.close()


def test_an_unreadable_registry_is_skipped_by_the_replay_and_never_answered_from_an_earlier_reading(tmp_path, monkeypatch):
    w = accounting(tmp_path, monkeypatch)
    broken, healthy = w.seats
    registry = record_owner.registry(broken.folder)
    good = registry.read_text()
    spy = Spy(monkeypatch)
    replays(monkeypatch)
    try:
        w.svc.account()
        assert all(replay_readers(w)[str(path)] == 'fake' for path in w.typed[broken.name])
        registry.write_text('{\n')
        spy.writes.clear()  # the test's own corrupting write is not the framework's
        before = totals(spy, w)
        w.svc.account()
        assert totals(spy, w)[healthy.name] == before[healthy.name] + 1, 'a healthy registry still reads once'
        assert any(event == 'error' and data.get('seat') == broken.name and data.get('record') == record_owner.REGISTRY
                   and 'unreadable record ownership' in data.get('error', '') for event, data in w.logs)
        readers = replay_readers(w)
        first, *rest = w.typed[broken.name]
        assert readers[str(first)] == 'fake', 'the launch journal is still read for that folder'
        assert all(readers[str(path)] is None for path in rest), 'skipped as before, not answered from pass one'
        assert all(readers[str(path)] == 'fake' for path in w.typed[healthy.name])
        assert spy.writes == [] and spy.locks == []
        registry.write_text(good + json.dumps({'record': str(rest[1]), 'harness': 'codex'}) + '\n')
        w.svc.account()
        readers = replay_readers(w)
        assert readers[str(rest[0])] == 'fake' and readers[str(rest[1])] is None, 'the repaired registry reads fresh'
    finally:
        w.svc.ledger.close()


def test_the_replays_readers_inside_a_pass_equal_a_fresh_replay_outside_any_pass(tmp_path, monkeypatch):
    w = accounting(tmp_path, monkeypatch)
    first = w.seats[0]
    registry = record_owner.registry(first.folder)
    spy = Spy(monkeypatch)
    try:
        w.svc.account()  # every typed record is ingested first, so the replay has a turn row for each
        registry.write_text(registry.read_text() + json.dumps({'record': str(w.typed[first.name][2]), 'harness': 'codex'}) + '\n')
        w.svc.account()
        inside = {record: (source[0][0], source[2]) for record, source in w.svc.ledger.attributions.sources.items()}
        assert inside[str(w.typed[first.name][2])][0] is None and inside[str(w.typed[first.name][1])][0] == 'fake'
        before = totals(spy, w)
        outside = attribution_cache.Attribution(w.svc.ledger.db, create=False)
        outside.replay(w.svc.store, w.svc.runtime, commit=False)
        w.svc.ledger.db.rollback()
        assert totals(spy, w) == {name: count + 1 for name, count in before.items()}, 'outside a pass: a fresh read'
        assert {record: (source[0][0], source[2]) for record, source in outside.sources.items()} == inside
    finally:
        w.svc.ledger.close()
