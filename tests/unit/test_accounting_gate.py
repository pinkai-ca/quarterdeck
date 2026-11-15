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
    record = home / 'ordinary-record'
    record.write_text('Ordinary native evidence.\n')
    (tmp_path / 'ship.toml').write_text('Ordinary declaration.\n')
    for name in ('launch.json', 'launch-price.json', 'record', 'pool',
                 'record-readers.jsonl', 'record-launches.jsonl'):
        (folder / name).write_text('Ordinary saved metadata.\n')
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
                            clock=clock, version=version, runtime=runtime, folder=folder, record=record)
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
        target = e.folder / name
    target.write_text('Changed ordinary evidence.\n')
    assert e.gate.allowed(e.service)
    e.complete()
    assert not e.gate.allowed(e.service)


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
        driver.gate.begin(service, [str(e.record)])
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
