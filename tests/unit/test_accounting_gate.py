"""Ordinary synthetic inputs for the accounting duty's private reuse certificate."""

from pathlib import Path
from contextlib import nullcontext
import sqlite3
import time
from types import SimpleNamespace

import pytest

from quarterdeck import accounting_gate
from quarterdeck.accounting_health import Health
from quarterdeck.accounting_slices import Driver
from quarterdeck.accounting_gate import BACKSTOP, Gate


@pytest.fixture
def example(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    folder = runtime / 'seats' / 'ordinary-builder'
    home = folder / 'home'
    home.mkdir(parents=True)
    trusted = runtime / 'trusted' / 'ordinary-builder'
    trusted.mkdir(parents=True)
    record = home / 'ordinary-record'
    record.write_text('Ordinary native evidence.\n')
    (tmp_path / 'ship.toml').write_text('Ordinary declaration.\n')
    for name in ('launch.json', 'launch-price.json', 'record', 'pool',
                 'record-readers.jsonl', 'record-launches.jsonl'):
        (trusted / name).write_text('Ordinary saved metadata.\n')
    (home / 'conversations.owned').write_text('Ordinary historical binding.\n')
    for name in ('store', 'imports', 'forwarded', 'addressed'):
        (runtime / 'post' / name).mkdir(parents=True)
    db = sqlite3.connect(runtime / 'ledger.sqlite')
    db.execute('CREATE TABLE turns (record TEXT, seat TEXT)')
    db.execute('CREATE TABLE output (value TEXT)')
    db.execute('INSERT INTO turns VALUES (?,?)', (str(record), 'ordinary-builder'))
    db.commit()
    clock = SimpleNamespace(wall=time.time_ns() + 10_000_000_000, monotonic=0.0)
    monkeypatch.setattr(accounting_gate.time, 'time_ns', lambda: clock.wall)
    monkeypatch.setattr(accounting_gate.time, 'monotonic', lambda: clock.monotonic)
    version = ['ordinary-engine']
    monkeypatch.setattr(accounting_gate.reader_stamp, 'attributions',
                        lambda: {'fake': ['ordinary-reader', version[0]]})
    seat = SimpleNamespace(name=folder.name, home=home, harness=SimpleNamespace(name='fake'),
                           spec=SimpleNamespace(model='ordinary-model'),
                           pool=SimpleNamespace(name='ordinary-pool'), session=SimpleNamespace(human=False))
    service = SimpleNamespace(runtime=runtime, ship=SimpleNamespace(folder=tmp_path, seats=['ordinary-builder']),
                              ledger=SimpleNamespace(db=db), resolved_seats=lambda: [seat],
                              accounting_epoch='ordinary-epoch')
    gate = Gate()
    def complete():
        gate.begin(service, [str(record)])
        gate.finish(service, db.total_changes, full=True)
        assert gate.completed is not None
    value = SimpleNamespace(service=service, seat=seat, db=db, gate=gate, complete=complete,
                            clock=clock, version=version, runtime=runtime, folder=folder, trusted=trusted, record=record)
    yield value
    db.close()


def test_quiet_ship_starts_no_job_between_backstops(example):
    e = example
    assert e.gate.allowed(e.service)
    e.complete()
    for elapsed in (0.0, 0.1, 10.0, BACKSTOP - 0.001):
        e.clock.monotonic = elapsed
        assert not e.gate.allowed(e.service)
    e.clock.monotonic = BACKSTOP
    assert e.gate.allowed(e.service)
    e.complete()
    assert not e.gate.allowed(e.service)


@pytest.mark.parametrize('name', ['native', 'wal', 'launch.json', 'launch-price.json', 'record', 'pool',
                                 'record-readers.jsonl', 'record-launches.jsonl', 'conversations.owned',
                                 'declaration', 'store', 'imports', 'forwarded', 'addressed'])
def test_each_file_or_immutable_post_publication_starts_a_job(example, name):
    e = example
    e.complete()
    if name == 'native':
        target = e.record
    elif name == 'wal':
        target = Path(str(e.record) + '-wal')
    elif name == 'declaration':
        target = e.service.ship.folder / 'ship.toml'
    elif name == 'conversations.owned':
        target = e.seat.home / name
    elif name in ('store', 'imports', 'forwarded', 'addressed'):
        target = e.runtime / 'post' / name / 'ordinary-publication.json'
    else:
        target = e.trusted / name
    target.write_text('Changed ordinary evidence.\n')
    assert e.gate.allowed(e.service)
    e.complete()
    assert not e.gate.allowed(e.service)


@pytest.mark.parametrize('declared', [False, True])
def test_retired_and_unresolved_seat_binding_changes_start_jobs(example, declared):
    e = example
    folder = e.runtime / 'trusted' / 'ordinary-unresolved-or-retired'
    folder.mkdir()
    journal = folder / 'record-launches.jsonl'
    journal.write_text('Ordinary retained launch evidence.\n')
    if declared:
        e.service.ship.seats.append(folder.name)
    # This seat is deliberately absent from resolved_seats in both cases.
    e.complete()
    journal.write_text('Changed ordinary retained launch evidence.\n')
    assert e.gate.allowed(e.service)


def test_changed_settings_reader_epoch_and_seat_declaration_are_inputs(example):
    e = example
    for change in (lambda: setattr(e.seat.spec, 'model', 'another-model'),
                   lambda: e.version.__setitem__(0, 'another-engine'),
                   lambda: setattr(e.service, 'accounting_epoch', 'another-epoch'),
                   lambda: e.service.ship.seats.append('another-declared-seat')):
        e.complete()
        change()
        assert e.gate.allowed(e.service)
    e.complete()
    assert not e.gate.allowed(e.service)


def test_same_connection_and_external_ledger_writes_start_jobs(example):
    e = example
    e.complete()
    e.db.execute("INSERT INTO output VALUES ('ordinary same-connection input')")
    e.db.commit()
    assert e.gate.allowed(e.service)
    e.complete()
    other = sqlite3.connect(e.runtime / 'ledger.sqlite')
    try:
        other.execute("INSERT INTO output VALUES ('ordinary external input')")
        other.commit()
    finally:
        other.close()
    assert e.gate.allowed(e.service)


def test_only_driver_committed_writes_are_excluded_from_completion(example):
    e = example
    e.gate.begin(e.service, [str(e.record)])
    before = e.db.total_changes
    e.db.execute("INSERT INTO output VALUES ('ordinary accounted output')")
    e.db.commit()
    e.gate.own(before, e.db.total_changes)
    e.gate.finish(e.service, e.db.total_changes, full=True)
    assert e.gate.completed is not None
    assert not e.gate.allowed(e.service)
    # A different duty's write between slices is not included in own_changes.
    e.gate.begin(e.service, [])
    e.db.execute("INSERT INTO output VALUES ('ordinary intervening input')")
    e.db.commit()
    e.gate.finish(e.service, e.db.total_changes, full=True)
    assert e.gate.completed is None
    assert e.gate.allowed(e.service)


def test_last_slice_output_is_excluded_only_after_completion(example):
    e = example
    e.gate.begin(e.service, [str(e.record)])
    before = e.db.total_changes
    e.db.execute("INSERT INTO output VALUES ('ordinary final-slice output')")
    e.db.commit()
    e.gate.finish(e.service, before, full=True)
    assert not e.gate.allowed(e.service)


def test_recent_or_clock_reversed_observation_grants_no_reuse(example):
    e = example
    e.complete()
    # The fingerprint is identical, but its timestamp can no longer certify
    # immutability when the clock falls back inside the filesystem tick.
    e.clock.wall = e.record.stat().st_ctime_ns + 1
    assert e.gate.allowed(e.service)
    e.gate.begin(e.service, [])
    e.gate.finish(e.service, e.db.total_changes, full=True)
    assert e.gate.completed is None
    e.clock.wall += 10_000_000_000
    e.complete()
    assert not e.gate.allowed(e.service)


def test_missing_metadata_replaced_folder_and_unknown_readers_do_not_hide_inputs(example, monkeypatch):
    e = example
    e.complete()
    e.record.unlink()
    assert e.gate.allowed(e.service)
    e.complete()
    e.record.write_text('Ordinary source returned.\n')
    assert e.gate.allowed(e.service)
    e.complete()
    target = e.runtime / 'post' / 'forwarded'
    target.rename(target.with_name('ordinary-old-folder'))
    target.mkdir()
    assert e.gate.allowed(e.service)
    e.complete()
    monkeypatch.setattr(accounting_gate.reader_stamp, 'attributions', lambda: {'fake': None})
    assert e.gate.allowed(e.service)


def test_failed_partial_restart_and_targeted_retry_certify_nothing(example):
    e = example
    e.complete()
    assert e.gate.allowed(e.service, retry=True)
    assert Gate().allowed(e.service)
    e.gate.begin(e.service, [])
    e.gate.finish(e.service, e.db.total_changes, full=False)
    assert e.gate.completed is None
    e.complete()
    e.gate.reject()
    assert e.gate.allowed(e.service)


def test_input_change_during_a_job_prevents_reuse(example):
    e = example
    e.gate.begin(e.service, [str(e.record)])
    e.record.write_text('Ordinary source advanced during the job.\n')
    e.gate.finish(e.service, e.db.total_changes, full=True)
    assert e.gate.completed is None


def test_unreadable_input_cannot_certify_a_quiet_ship(example, monkeypatch):
    e = example
    e.complete()
    original = Path.stat
    def unavailable(path, *args, **kwargs):
        if path == e.record:
            raise PermissionError('ordinary unavailable input')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'stat', unavailable)
    assert e.gate.allowed(e.service)


def test_driver_starts_only_for_changed_inputs_or_backstop(example, monkeypatch):
    e = example
    e.complete()
    e.service.ledger.record_pass = lambda: nullcontext(e.service.ledger)
    e.service.ledger.catching_up = lambda: False
    e.service.accounting_health = Health()
    e.service.publish_accounting_health = lambda **kwargs: None
    e.service.accounting_shown_changed = lambda previous: None
    e.service.log = lambda *args, **kwargs: None
    driver = Driver()
    driver.gate = e.gate
    monkeypatch.setattr(driver, 'collect', lambda service: iter(()))
    monkeypatch.setattr(driver, 'pump', lambda: False)
    def finish_one(service):
        driver.started = e.clock.monotonic
        driver.job += 1
        driver.gate.confirm(service, [str(e.record)])
        driver.ready.append(('done',))
    monkeypatch.setattr(driver, 'start', finish_one)
    for elapsed in (0.0, 1.0, BACKSTOP - 1):
        e.clock.monotonic = elapsed
        driver.step(e.service)
    assert driver.job == 0 and driver.process is None
    e.clock.monotonic = BACKSTOP
    driver.step(e.service)
    assert driver.job == 1
    e.clock.monotonic += 1
    driver.step(e.service)
    assert driver.job == 1
    e.record.write_text('Ordinary changed input starts one job.\n')
    driver.step(e.service)
    assert driver.job == 2
    e.clock.monotonic += 1
    driver.step(e.service)
    assert driver.job == 2


@pytest.mark.parametrize('change,jobs', [('launch.json', 2), ('record-launches.jsonl', 2), ('native', 2),
                                         ('new-record', 2), ('found-unchanged', 1)])
def test_a_change_made_while_collecting_starts_the_next_job(example, monkeypatch, change, jobs):
    """The certificate's 'before' precedes collection: what changes during it is newer than the certificate.

    A record found while collecting that predates the reading changes nothing and starts no job.
    """
    import os
    import tempfile
    from quarterdeck import post
    e = example
    found = e.folder / 'home' / 'ordinary-found-record'
    if change == 'found-unchanged':
        found.write_text('Ordinary record the gate has not yet watched.\n')
    e.complete()
    e.service.ledger.record_pass = lambda: nullcontext(e.service.ledger)
    e.service.ledger.catching_up = lambda: False
    e.service.accounting_health = Health()
    e.service.publish_accounting_health = lambda **kwargs: None
    e.service.accounting_shown_changed = lambda previous: None
    e.service.log = lambda *args, **kwargs: None
    driver = Driver()
    driver.gate = e.gate
    driver.process = SimpleNamespace(poll=lambda: None)  # the child is idle; no process is launched
    collections = []
    def collect(service):
        sources = [e.record] + ([found] if change in ('new-record', 'found-unchanged') else [])
        if not collections:  # once, between the gate's reading and the end of collection
            if change == 'native':
                e.record.write_text('Ordinary input changed while collecting.\n')
            elif change == 'new-record':
                found.write_text('Ordinary record that appeared while collecting.\n')
                os.utime(found, ns=(e.clock.wall, e.clock.wall))  # written after the gate's reading
                e.clock.wall += 3 * post.POST_TIMESTAMP_TICK_NS
            elif change == 'found-unchanged':
                pass
            else:
                (e.trusted / change).write_text('Ordinary metadata changed while collecting.\n')
        collections.append(change)
        driver.seats.append(dict(name=e.seat.name, sources=[(str(path), 'fake', None, False) for path in sources]))
        yield
    def pump():
        if driver.active and not driver.events and not driver.ready:
            driver.ready.append(('done',))
        return False
    monkeypatch.setattr(driver, 'collect', collect)
    monkeypatch.setattr(driver, 'pump', pump)
    e.clock.monotonic = BACKSTOP
    with tempfile.TemporaryFile() as driver.plan:
        driver.step(e.service)
        assert driver.job == 1 and not driver.active and len(collections) == 1
        e.clock.monotonic += 1
        driver.step(e.service)  # the next tick, long before the backstop
        assert driver.job == jobs
        e.clock.monotonic += 1
        driver.step(e.service)  # nothing changed during the last job: it certified and the ship waits
        assert driver.job == jobs


def reason_start(e):
    """Use the actual start logger/encoding without launching a planning process."""
    import tempfile
    rows = []
    e.service.log = lambda event, **data: rows.append(dict(event=event, **data))
    driver = Driver()
    driver.gate = e.gate
    driver.process = object()
    with tempfile.TemporaryFile() as driver.plan:
        driver.start(e.service)
    reasons = [row for row in rows if row['event'] == 'accounting-gate-start']
    assert len(reasons) == 1
    return reasons[0]


def original_eligibility(e, *, retry=False):
    return (retry or e.gate.completed is None or e.clock.monotonic >= e.gate.due
            or accounting_gate.capture(e.service, e.gate.records) != e.gate.completed)


@pytest.mark.parametrize('change,kind', [
    ('record', 'record'), ('wal', '-wal'), ('launch', 'launch-metadata'),
    ('post', 'post-folder'), ('settings', 'settings'), ('hashes', 'hashes'),
])
def test_start_reason_names_only_the_changed_input_class(example, change, kind):
    e = example
    e.complete()
    if change == 'record':
        e.record.write_text('Ordinary appended record evidence.\n')
    elif change == 'wal':
        Path(str(e.record) + '-wal').write_bytes(b'ordinary WAL metadata')
    elif change == 'launch':
        (e.trusted / 'launch.json').write_text('Ordinary changed launch metadata.\n')
    elif change == 'post':
        (e.runtime / 'post' / 'store' / 'ordinary-publication').write_text('Ordinary publication.\n')
    elif change == 'settings':
        e.seat.spec.model = 'ordinary-changed-model'
    else:
        e.version[0] = 'ordinary-changed-engine'
    expected = original_eligibility(e)
    assert e.gate.allowed(e.service) == expected is True
    row = reason_start(e)
    assert row['decision']['changed-input'] == {kind: 1}
    import json
    encoded = json.dumps(row)
    for identity in (str(e.runtime), e.seat.name, e.record.name, e.seat.pool.name,
                     e.seat.spec.model, e.version[0]):
        assert identity not in encoded


@pytest.mark.parametrize('reason', ['retry', 'no-completed-certificate', 'backstop'])
def test_start_reason_preserves_short_circuit_decisions(example, monkeypatch, reason):
    e = example
    e.complete()
    if reason == 'no-completed-certificate':
        e.gate.reject()
    elif reason == 'backstop':
        e.clock.monotonic = BACKSTOP
    def unexpected(*args, **kwargs):
        raise AssertionError('a short-circuit decision must not capture inputs')
    monkeypatch.setattr(accounting_gate, 'capture', unexpected)
    retry = reason == 'retry'
    assert e.gate.allowed(e.service, retry=retry) == original_eligibility(e, retry=retry) is True
    assert e.gate.decision[reason] == 1


@pytest.mark.parametrize('failure', ['tick-young', 'unreadable'])
def test_invalid_reading_reason_is_class_only_and_decision_is_unchanged(example, monkeypatch, failure):
    e = example
    e.complete()
    original = Path.stat
    def changed(path, *args, **kwargs):
        if path == e.record:
            if failure == 'unreadable':
                raise PermissionError('an ordinary path must never enter the log')
            value = original(path, *args, **kwargs)
            return SimpleNamespace(st_dev=value.st_dev, st_ino=value.st_ino, st_mode=value.st_mode,
                                   st_size=value.st_size, st_mtime_ns=e.clock.wall,
                                   st_ctime_ns=value.st_ctime_ns)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'stat', changed)
    assert e.gate.allowed(e.service) == original_eligibility(e) is True
    row = reason_start(e)
    assert row['decision'][failure] == {'record': 1}
    assert 'ordinary' not in str(row)
    assert str(e.record) not in str(row)


def test_missing_hash_reason_contains_no_hash_value(example):
    e = example
    e.complete()
    e.version[0] = None
    # The reader mapping remains nonempty, but a missing top-level hash does not.
    from unittest.mock import patch
    with patch.object(accounting_gate.reader_stamp, 'attributions', return_value={'fake': None}):
        assert e.gate.allowed(e.service) == original_eligibility(e) is True
        assert reason_start(e)['decision']['unreadable'] == {'hashes': 1}


@pytest.mark.parametrize('external', [False, True])
def test_sql_revision_start_separates_connection_and_other_writer_deltas(example, external):
    e = example
    e.complete()
    if external:
        db = sqlite3.connect(e.runtime / 'ledger.sqlite')
        try:
            db.execute('INSERT INTO output VALUES (?)', ('ordinary other writer',))
            db.commit()
        finally:
            db.close()
    else:
        e.db.execute('INSERT INTO output VALUES (?)', ('ordinary same-connection duty',))
        e.db.commit()
    assert e.gate.allowed(e.service) == original_eligibility(e) is True
    decision = reason_start(e)['decision']
    assert decision['sql-revision'] == 1
    assert 'changed-input' not in decision
    assert decision['sql']['driver_changes'] == 0
    assert decision['sql']['other_changes'] == (0 if external else 1)
    assert decision['sql']['data_version'] == (1 if external else 0)


@pytest.mark.parametrize('other_write', [False, True])
def test_prior_job_diagnostics_preserve_actual_own_write_exclusion(example, other_write):
    e = example
    e.complete()
    e.gate.begin(e.service, [str(e.record)])
    before = e.db.total_changes
    e.db.execute('INSERT INTO output VALUES (?)', ('ordinary driver slice',))
    e.db.commit()
    e.gate.own(before, e.db.total_changes)
    if other_write:
        e.db.execute('INSERT INTO output VALUES (?)', ('ordinary intervening duty',))
        e.db.commit()
    final_slice = e.db.total_changes
    e.db.execute('INSERT INTO output VALUES (?)', ('ordinary driver completion',))
    e.db.commit()
    e.gate.finish(e.service, final_slice, full=True)
    assert (e.gate.completed is None) == other_write
    if not other_write:
        assert not e.gate.allowed(e.service)
        e.clock.monotonic = e.gate.due
    assert e.gate.allowed(e.service) == original_eligibility(e) is True
    prior = reason_start(e)['decision']['previous-job']
    assert prior['completed'] == int(not other_write)
    assert prior['end']['sql']['driver_changes'] == 2
    assert prior['end']['sql']['other_changes'] == int(other_write)
    assert ('sql-revision' in prior['end']) == other_write


def test_no_certificate_start_retains_the_failed_begin_class(example):
    e = example
    e.complete()
    e.clock.wall = e.record.stat().st_mtime_ns
    e.gate.begin(e.service, [str(e.record)])
    e.clock.wall += 10_000_000_000
    e.gate.finish(e.service, e.db.total_changes, full=True)
    assert e.gate.completed is None
    assert e.gate.allowed(e.service)
    prior = reason_start(e)['decision']['previous-job']
    assert prior['begin_valid'] == 0 and prior['end_valid'] == 1
    assert prior['begin']['tick-young']['record'] == 1
    assert 'previous-job' not in prior  # one generation, never a growing history


def test_quiet_waits_emit_no_line_and_are_counted_in_minute_summary(example):
    from quarterdeck.service_timing import Timings
    e = example
    e.complete()
    rows = []
    timings = Timings()
    e.service.timings = lambda: timings
    e.service.accounting_health = Health()
    e.service.publish_accounting_health = lambda **kwargs: None
    e.service.accounting_shown_changed = lambda before: None
    e.service.log = lambda event, **data: rows.append(dict(event=event, **data))
    driver = Driver()
    driver.gate = e.gate
    for _ in range(100):
        driver.step(e.service)
    assert rows == [] and driver.process is None and timings.accounting_waits == 100
    e.clock.monotonic = 60.0
    timings.publish(e.service.log)
    assert len(rows) == 1 and rows[0]['event'] == 'service-timing'
    assert rows[0]['duties']['accounting_waits']['calls'] == 100 and timings.accounting_waits == 0
    assert 'ordinary' not in str(rows) and str(e.runtime) not in str(rows)


def test_rejected_job_does_not_misreport_an_older_completed_job(example):
    e = example
    e.complete()
    e.gate.begin(e.service, [str(e.record)])
    e.gate.reject()
    assert e.gate.allowed(e.service)
    prior = reason_start(e)['decision']['previous-job']
    assert prior['completed'] == 0 and prior['rejected'] == 1
    assert prior['begin_valid'] == 1 and 'end' not in prior
