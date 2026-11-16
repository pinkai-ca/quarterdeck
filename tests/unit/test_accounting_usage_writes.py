"""The ledger rows a duty proven to touch no accounting table writes (the usage refresh) are no other writer's:
the gate's readings take them out of the other-writers barrier. Synthetic seats, records and ledger only."""

import sqlite3
from types import SimpleNamespace

from quarterdeck.service import Service
from .test_accounting_scoped_jobs import change, world  # noqa: F401
from .test_accounting_stitched import full_then_retry


def usage_write(e, counted):
    """A row written by a duty: counted apart, as the usage refresh's are, or not."""
    before = e.db.total_changes
    e.db.execute("INSERT INTO output VALUES ('ordinary usage sample')")
    e.db.commit()
    if counted:
        e.service.non_accounting_writes = getattr(e.service, 'non_accounting_writes', 0) + e.db.total_changes - before


def test_a_counted_non_accounting_write_leaves_the_gate_quiet(world):
    e = world
    usage_write(e, counted=True)
    assert not e.gate.allowed(e.service), 'the usage refresh is no other writer'


def test_an_uncounted_write_still_forces_a_full_job(world):
    e = world
    usage_write(e, counted=False)
    assert e.gate.allowed(e.service) and e.gate.scope is None


def test_a_counted_write_during_a_full_job_lets_its_certificate_stand(world):
    e = world
    change(e.paths['ordinary-alpha'])
    e.clock.mono += 10_000  # past the backstop: a full job
    assert e.gate.allowed(e.service) and e.gate.scope is None
    assert e.begin()
    usage_write(e, counted=True)
    e.finish()
    assert e.gate.partitions.baseline is not None and not e.gate.allowed(e.service)


def test_a_counted_write_between_the_full_job_and_its_retry_lets_the_stitch_stand(world):
    e = world
    full_then_retry(e, between=[lambda: usage_write(e, counted=True)])
    assert e.gate.previous['stitched'] == 1


def usage_tables(db):
    db.execute('CREATE TABLE IF NOT EXISTS usage_reads (pool TEXT PRIMARY KEY, at TEXT NOT NULL, error TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS pools (pool TEXT, window TEXT)')
    db.commit()


def test_the_service_counts_only_its_non_accounting_duties_writes_to_usage_tables_apart():
    db = sqlite3.connect(':memory:')
    usage_tables(db)
    db.execute('CREATE TABLE turns (record TEXT, seat TEXT)')
    service = SimpleNamespace(ledger=SimpleNamespace(db=db))
    with Service.non_accounting(service, 'refresh_usage'):
        db.execute("INSERT INTO usage_reads VALUES ('ordinary-pool', '2032-01-02T12:00:00+00:00', NULL)")
        db.execute("INSERT INTO pools VALUES ('ordinary-pool', 'week')")
    assert service.non_accounting_writes == 2, 'the usage refresh, to usage tables'
    with Service.non_accounting(service, 'views'):
        db.execute("INSERT INTO usage_reads VALUES ('other-pool', '2032-01-02T12:00:00+00:00', NULL)")
    assert service.non_accounting_writes == 2, 'another duty: never counted apart'
    with Service.non_accounting(service, 'refresh_usage'):
        db.execute("INSERT INTO pools VALUES ('ordinary-pool', 'day')")
        db.execute("INSERT INTO turns VALUES ('ordinary-record', 'ordinary-seat')")
    assert service.non_accounting_writes == 2, 'a write to an accounting table leaves the whole duty another writer'
    db.execute("INSERT INTO turns VALUES ('ordinary-record', 'ordinary-seat-2')")  # the authorizer is gone after
    assert db.total_changes == 6


def test_a_write_to_an_accounting_table_in_the_usage_duty_still_breaks_the_certificate(world):
    """The captain's test: only the duty's writes to tables no accounting step reads are counted apart; one write to an
    accounting table (here, turns) inside the same duty leaves its rows another writer's, and the next job is full."""
    e = world
    usage_tables(e.db)
    assert e.begin()
    e.finish()
    assert not e.gate.allowed(e.service)
    e.service.ledger.db = e.db
    with Service.non_accounting(e.service, 'refresh_usage'):
        e.db.execute("INSERT INTO usage_reads VALUES ('ordinary-pool', '2032-01-02T12:00:00+00:00', NULL)")
        e.db.commit()
    assert not e.gate.allowed(e.service), 'a usage write alone: quiet'
    with Service.non_accounting(e.service, 'refresh_usage'):
        e.db.execute("INSERT INTO usage_reads VALUES ('other-pool', '2032-01-02T12:00:00+00:00', NULL)")
        e.db.execute("INSERT INTO turns VALUES ('ordinary-unowned-record', 'ordinary-alpha')")
        e.db.commit()
    assert e.gate.allowed(e.service) and e.gate.scope is None, 'an accounting table written: the barrier holds'
