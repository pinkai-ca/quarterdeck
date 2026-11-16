"""Dirty seats below unchanged global barriers; all sources here are synthetic."""

import os
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace

import pytest

from quarterdeck import accounting_gate, trusted_files
from quarterdeck.accounting_gate import BACKSTOP, Gate


@pytest.fixture
def world(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    (tmp_path / 'ship.toml').write_text('Synthetic declaration.\n')
    for name in ('store', 'imports', 'forwarded', 'addressed'):
        (runtime / 'post' / name).mkdir(parents=True)
    seats, inventory, paths = [], [], {}
    db = sqlite3.connect(runtime / 'ledger.sqlite')
    db.execute('CREATE TABLE turns(record TEXT, seat TEXT)')
    db.execute('CREATE TABLE output(value TEXT)')
    for name in ('ordinary-alpha', 'ordinary-beta'):
        home = runtime / 'seats' / name / 'home'
        home.mkdir(parents=True)
        record = home / 'ordinary-native-record'
        record.write_text('Ordinary synthetic native input.\n')
        (trusted_files.make(runtime, name) / trusted_files.READERS).write_text('Synthetic binding.\n')
        seats.append(SimpleNamespace(name=name, home=home, harness=SimpleNamespace(name='fake'),
                     spec=SimpleNamespace(model='ordinary-model'), pool=SimpleNamespace(name='ordinary-pool'),
                     session=SimpleNamespace(human=False)))
        inventory.append(dict(name=name, sources=[(str(record), 'fake', None, False)]))
        paths[name] = record
        db.execute('INSERT INTO turns VALUES (?,?)', (str(record), name))
    db.commit()
    clock = SimpleNamespace(wall=time.time_ns() + 10_000_000_000, mono=0.0)
    monkeypatch.setattr(accounting_gate.time, 'time_ns', lambda: clock.wall)
    monkeypatch.setattr(accounting_gate.time, 'monotonic', lambda: clock.mono)
    monkeypatch.setattr(accounting_gate.reader_stamp, 'attributions', lambda: {'fake': ['reader', 'engine']})
    pending = [False]
    service = SimpleNamespace(runtime=runtime,
                ship=SimpleNamespace(folder=tmp_path, seats=[s.name for s in seats]),
                ledger=SimpleNamespace(db=db, catching_up=lambda: pending[0]),
                accounting_epoch='ordinary-engine', resolved_seats=lambda: seats)
    gate = Gate()
    def begin(scope=None):
        chosen = [item for item in inventory if scope is None or item['name'] in scope]
        sources = [path for item in chosen for path, *_ in item['sources']]
        gate.configure(service, chosen, scope)
        gate.begin(service, sources)
        return gate.confirm(service, sources)
    def finish(scope=None, *, committed=True):
        gate.finish(service, db.total_changes, full=scope is None, committed=committed)
    assert begin()
    finish()
    assert not gate.allowed(service)
    yield SimpleNamespace(service=service, gate=gate, db=db, paths=paths, clock=clock,
                          inventory=inventory, seats=seats, pending=pending,
                          begin=begin, finish=finish, runtime=runtime)
    db.close()


def change(path):
    path.write_text(path.read_text() + 'Ordinary append.\n')


@pytest.mark.parametrize('wal', [False, True])
def test_one_changed_record_or_wal_selects_only_its_seat(world, wal):
    e = world
    record = e.paths['ordinary-alpha']
    target = Path(str(record) + '-wal') if wal else record
    if wal:
        target.write_text('Ordinary WAL append.\n')
    else:
        change(target)
    assert e.gate.allowed(e.service)
    assert e.gate.scope == {'ordinary-alpha'}
    due = e.gate.due
    assert e.begin(e.gate.scope)
    e.finish({'ordinary-alpha'})
    assert e.gate.completed is None, 'a scoped job does not mint a global full-job certificate'
    assert not e.gate.allowed(e.service)
    assert e.gate.due == due


@pytest.mark.parametrize('barrier', ['post', 'launch', 'registry', 'settings', 'hash', 'sql', 'external-sql'])
def test_each_global_barrier_requires_full_work(world, monkeypatch, barrier):
    e = world
    change(e.paths['ordinary-alpha'])
    if barrier == 'post':
        (e.runtime / 'post/store/ordinary-message').write_text('Ordinary immutable publication.\n')
    elif barrier == 'launch':
        (trusted_files.of_home(e.seats[1].home) / trusted_files.LAUNCH).write_text('Ordinary new launch.\n')
    elif barrier == 'registry':
        change(trusted_files.of_home(e.seats[1].home) / trusted_files.READERS)
    elif barrier == 'settings':
        e.seats[1].spec.model = 'another-ordinary-model'
    elif barrier == 'hash':
        monkeypatch.setattr(accounting_gate.reader_stamp, 'attributions', lambda: {'fake': ['reader', 'new-engine']})
    elif barrier == 'sql':
        e.db.execute("INSERT INTO output VALUES ('ordinary other duty')")
        e.db.commit()
    else:
        other = sqlite3.connect(e.runtime / 'ledger.sqlite')
        try:
            other.execute("INSERT INTO output VALUES ('ordinary other writer')")
            other.commit()
        finally:
            other.close()
    assert e.gate.allowed(e.service)
    assert e.gate.scope is None


def test_continuously_young_record_stays_dirty_without_invalidating_other_seats(world):
    e = world
    path = e.paths['ordinary-alpha']
    old = e.gate.partitions.marks[str(path)]
    due = e.gate.due
    for step in range(12):
        e.clock.mono += 5
        e.clock.wall += 5_000_000_000
        change(path)
        os.utime(path, ns=(e.clock.wall, e.clock.wall))
        assert e.gate.allowed(e.service) and e.gate.scope == {'ordinary-alpha'}
        assert e.begin(e.gate.scope)
        e.finish({'ordinary-alpha'})
        assert str(path) in e.gate.partitions.dirty
        assert e.gate.partitions.marks[str(path)] == old
        assert e.gate.due == due
    e.clock.wall += 3_000_000_000
    assert e.gate.allowed(e.service) and e.gate.scope == {'ordinary-alpha'}
    assert e.begin(e.gate.scope)
    e.finish({'ordinary-alpha'})
    assert not e.gate.allowed(e.service)


@pytest.mark.parametrize('phase', ['before-start', 'during-job'])
@pytest.mark.parametrize('global_change', [False, True])
def test_concurrent_outside_or_global_change_forces_a_full_followup(world, phase, global_change):
    e = world
    change(e.paths['ordinary-alpha'])
    assert e.gate.allowed(e.service) and e.gate.scope == {'ordinary-alpha'}
    if phase == 'during-job':
        assert e.begin(e.gate.scope)
    if global_change:
        (e.runtime / 'post/store/ordinary-message').write_text('Ordinary concurrent publication.\n')
    else:
        change(e.paths['ordinary-beta'])
    if phase == 'before-start':
        assert e.begin({'ordinary-alpha'}) is False
    else:
        e.finish({'ordinary-alpha'})
    assert e.gate.allowed(e.service) and e.gate.scope is None


def test_an_ambiguous_or_retired_owner_never_selects_one_seat(world):
    e = world
    e.inventory[1]['sources'].append(e.inventory[0]['sources'][0])
    assert e.begin()
    e.finish()
    change(e.paths['ordinary-alpha'])
    assert e.gate.allowed(e.service) and e.gate.scope is None
    e.inventory[1]['sources'].pop()
    e.db.execute('UPDATE turns SET seat=? WHERE record=?', ('ordinary-retired', str(e.paths['ordinary-alpha'])))
    e.db.commit()
    assert e.begin()
    e.finish()
    change(e.paths['ordinary-alpha'])
    assert e.gate.allowed(e.service) and e.gate.scope is None


def test_scoped_jobs_never_postpone_the_full_backstop(world):
    e = world
    due = e.gate.due
    for now in (1, 50, 100, 300, 599):
        e.clock.mono = now
        change(e.paths['ordinary-alpha'])
        assert e.gate.allowed(e.service) and e.gate.scope == {'ordinary-alpha'}
        assert e.begin(e.gate.scope)
        e.finish({'ordinary-alpha'})
        assert e.gate.due == due == BACKSTOP
    e.clock.mono = BACKSTOP
    assert e.gate.allowed(e.service) and e.gate.scope is None
    assert e.gate.decision['backstop'] == 1
    assert e.begin()
    e.finish()
    assert e.gate.due == 2 * BACKSTOP


def test_pending_global_catchup_forces_full_even_when_records_would_be_scoped(world):
    e = world
    change(e.paths['ordinary-alpha'])
    e.pending[0] = True
    assert e.gate.allowed(e.service) and e.gate.scope is None
    assert e.gate.decision['catch-up'] == 1
    assert e.gate.force_full


@pytest.mark.parametrize('kind', ['failed', 'retry'])
def test_failed_or_targeted_retry_does_not_create_partition_certificates(world, kind):
    e = world
    change(e.paths['ordinary-alpha'])
    if kind == 'retry':
        assert e.gate.allowed(e.service, retry=True)
    else:
        assert e.gate.allowed(e.service)
    assert e.begin({'ordinary-alpha'})
    e.finish({'ordinary-alpha'}, committed=kind != 'failed')
    assert e.gate.partitions.baseline is None
    assert e.gate.allowed(e.service) and e.gate.scope is None


def test_a_scoped_job_never_drops_an_owner_it_did_not_read(world):
    """A record two seats list keeps both owners after a scoped job for one of them, so a later change to it still
    calls for the full job; a record only the scoped seat lists updates as it always did."""
    e = world
    alpha, beta = e.inventory
    extra = e.paths['ordinary-alpha'].with_name('ordinary-second-record')
    extra.write_text('Ordinary synthetic native input.\n')
    alpha['sources'].append((str(extra), 'fake', None, False))
    e.db.execute('INSERT INTO turns VALUES (?,?)', (str(extra), 'ordinary-alpha'))
    e.db.commit()
    beta['sources'].append(alpha['sources'][0])  # beta lists alpha's record too, by its inventory alone
    assert e.begin()
    e.finish()
    shared = str(Path(os.path.abspath(e.paths['ordinary-alpha'])))
    assert e.gate.partitions.owners[shared] == {'ordinary-alpha', 'ordinary-beta'}
    change(extra)  # alpha's own record: a scoped job for alpha alone
    assert e.gate.allowed(e.service) and e.gate.scope == {'ordinary-alpha'}
    assert e.begin(e.gate.scope)
    e.finish({'ordinary-alpha'})
    assert e.gate.partitions.owners[shared] == {'ordinary-alpha', 'ordinary-beta'}, 'beta was not read: it stays'
    change(e.paths['ordinary-alpha'])
    assert e.gate.allowed(e.service) and e.gate.scope is None, 'the shared record still calls for the full job'
    assert e.begin()
    e.finish()
    change(extra)
    assert e.gate.allowed(e.service) and e.gate.scope == {'ordinary-alpha'}, 'only alpha lists it: alpha alone'


def test_a_scoped_seat_whose_read_failed_keeps_its_owners_and_stays_dirty(world):
    """A scoped job reads only what it collected: a seat whose guarded read failed (its collect skipped it) keeps its
    owners as they stood, and nothing of it is marked, so a record it shares still calls for the full job."""
    e = world
    alpha, beta = e.inventory
    beta['sources'].append(alpha['sources'][0])  # beta lists alpha's record too, by its inventory alone
    assert e.begin()
    e.finish()
    shared = str(Path(os.path.abspath(e.paths['ordinary-alpha'])))
    own = str(Path(os.path.abspath(e.paths['ordinary-beta'])))
    assert e.gate.partitions.owners[shared] == {'ordinary-alpha', 'ordinary-beta'}
    change(e.paths['ordinary-beta'])  # beta's own record: a scoped job for beta alone
    assert e.gate.allowed(e.service) and e.gate.scope == {'ordinary-beta'}
    # beta's read fails: the job collects no seat, as accounting_slices.collect skips a guarded seat.
    e.gate.configure(e.service, [], {'ordinary-beta'})
    e.gate.begin(e.service, [])
    assert e.gate.confirm(e.service, [])
    e.finish({'ordinary-beta'})
    assert e.gate.partitions.owners[shared] == {'ordinary-alpha', 'ordinary-beta'}, 'beta was not read: it stays'
    stamp = dict(e.gate.partitions.baseline.inputs[0])[own]
    assert e.gate.partitions.marks.get(own) != stamp and own in e.gate.partitions.dirty, 'an unread change is not marked'
    assert e.gate.allowed(e.service) and e.gate.scope == {'ordinary-beta'}, 'beta is read again'
    change(e.paths['ordinary-alpha'])
    assert e.gate.allowed(e.service) and e.gate.scope is None, 'the shared record still calls for the full job'
