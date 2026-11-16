"""A due hold starts an accounting job though nothing the gate watches has changed.

The child reads a record whose turn state is unknown only for a time and leaves it held until that time. The
parent's gate certifies that no input changed, so without the hold the record would wait for the ten-minute
backstop. A due hold is a reason of its own to start a job for its seat: the start event names its class and a
count, never a seat, path or record. Every clock here is injected.
"""

from contextlib import nullcontext
from datetime import timedelta
import io
import json
from types import SimpleNamespace
import tempfile

import pytest

from quarterdeck.accounting_gate import BACKSTOP
from quarterdeck.accounting_health import Health
from quarterdeck.accounting_slices import Driver, frame, receive
from quarterdeck.harness import claude, held_reading
from tests.unit.test_accounting_gate import example  # noqa: F401  (fixture)
from tests.unit.test_accounting_scoped_jobs import change, with_scopes, world  # noqa: F401  (fixture)
from tests.unit.test_resume_notice_ordinary import AT


UNTIL = AT + timedelta(seconds=claude.NOTICE_GRACE_SECONDS)


def reader_at(monkeypatch, seconds):
    monkeypatch.setattr(claude._Reader, 'clock', staticmethod(lambda: UNTIL + timedelta(seconds=seconds)))


def held():
    return held_reading.Hold(UNTIL, claude._Reader)


def test_a_due_hold_scopes_a_job_to_its_seat_and_names_only_its_class(world, monkeypatch):
    e = world
    e.gate.hold('ordinary-alpha', held())
    for seconds in (-3600, -1, -0.001):
        reader_at(monkeypatch, seconds)
        assert not e.gate.allowed(e.service)
        assert e.gate.decision == {}
    reader_at(monkeypatch, 0)
    assert e.clock.mono < BACKSTOP
    assert e.gate.allowed(e.service)
    assert e.gate.scope == {'ordinary-alpha'}
    assert e.gate.decision['held-reading'] == 1
    encoded = json.dumps(e.gate.decision)
    for identity in (str(e.runtime), 'ordinary-alpha', 'ordinary-beta', 'ordinary-native-record'):
        assert identity not in encoded
    # The scope the hold added is the job's own, not a race: confirm() keeps it.
    assert e.begin({'ordinary-alpha'})
    e.gate.taken({'ordinary-alpha'})
    e.finish({'ordinary-alpha'})
    assert not e.gate.allowed(e.service)


def test_a_due_hold_joins_a_changed_seat(world, monkeypatch):
    e = world
    change(e.paths['ordinary-beta'])
    e.gate.hold('ordinary-alpha', held())
    reader_at(monkeypatch, 0)
    assert e.gate.allowed(e.service)
    assert e.gate.scope == {'ordinary-alpha', 'ordinary-beta'}
    assert e.gate.decision['held-reading'] == 1 and e.gate.decision['changed-input'] == {'record': 1}
    assert e.begin({'ordinary-alpha', 'ordinary-beta'})


def test_a_hold_for_a_seat_the_partitions_do_not_know_starts_a_full_job(world, monkeypatch):
    e = world
    e.gate.hold('ordinary-retired', held())
    reader_at(monkeypatch, 0)
    assert e.gate.allowed(e.service)
    assert e.gate.scope is None and e.gate.decision['held-reading'] == 1


def test_a_hold_taken_by_a_job_starts_nothing_more(world, monkeypatch):
    e = world
    e.gate.hold('ordinary-alpha', held())
    e.gate.hold('ordinary-beta', held())
    reader_at(monkeypatch, 0)
    e.gate.taken({'ordinary-alpha'})
    assert e.gate.allowed(e.service) and e.gate.scope == {'ordinary-beta'}
    e.gate.taken(None)
    assert not e.gate.allowed(e.service)


def test_without_partitions_a_due_hold_starts_a_job_before_the_backstop(example, monkeypatch):
    e = example
    e.complete()
    e.gate.hold('ordinary-builder', held())
    reader_at(monkeypatch, -1)
    assert not e.gate.allowed(e.service)
    reader_at(monkeypatch, 0)
    assert e.clock.monotonic < BACKSTOP
    assert e.gate.allowed(e.service)
    assert e.gate.decision['held-reading'] == 1 and set(e.gate.decision) <= {'held-reading', 'previous-job'}


def driver_in(e, rows):
    e.service.ledger.record_pass = lambda: nullcontext(e.service.ledger)
    e.service.accounting_health = Health()
    e.service.publish_accounting_health = lambda **kwargs: None
    e.service.accounting_shown_changed = lambda previous: None
    e.service.heartbeat = lambda: None
    e.service.log = lambda event, **data: rows.append(dict(event=event, **data))
    driver = Driver()
    driver.gate = e.gate
    driver.process = SimpleNamespace(poll=lambda: None)  # the child is idle; no process is launched
    return driver


def test_the_driver_starts_a_job_for_a_due_hold_before_the_backstop(world, monkeypatch):
    e = world
    rows, replies = [], []
    driver = driver_in(e, rows)

    def collect(service):
        for item in e.inventory:
            if driver.scope is None or item['name'] in driver.scope:
                driver.seats.append(dict(item))
                yield

    def pump():
        if driver.active and not driver.events and not driver.ready:
            driver.ready.extend(with_scopes(driver, replies.pop(0) if replies else [('done',)]))
        return False

    monkeypatch.setattr(driver, 'collect', collect)
    monkeypatch.setattr(driver, 'pump', pump)
    reader_at(monkeypatch, -30)
    with tempfile.TemporaryFile() as driver.plan:
        # A changed record starts a job; the child reads it and leaves it held, as a fresh notice does.
        change(e.paths['ordinary-alpha'])
        replies.append([('hold', 'ordinary-alpha', held()), ('done',)])
        driver.step(e.service)
        assert driver.job == 1 and not driver.active
        # Nothing changes and the hold is not yet due: no job, whatever the monotonic clock short of the backstop.
        for mono in (1.0, 10.0, BACKSTOP / 2):
            e.clock.mono = mono
            driver.step(e.service)
        assert driver.job == 1
        # The hold falls due on its reader's clock, long before the backstop: one job, for its seat alone.
        reader_at(monkeypatch, 0)
        driver.step(e.service)
        assert driver.job == 2 and not driver.active
        starts = [row for row in rows if row['event'] == 'accounting-gate-start']
        assert starts[-1]['decision']['held-reading'] == 1
        assert 'held-reading' not in starts[0]['decision']
        assert [row['scope'] for row in rows if row['event'] == 'accounting-job-start'] == [['ordinary-alpha']] * 2
        encoded = json.dumps(starts[-1])
        for identity in (str(e.runtime), 'ordinary-alpha', 'ordinary-native-record'):
            assert identity not in encoded
        # The child read it again and left no hold: no further job.
        e.clock.mono += 1
        driver.step(e.service)
        assert driver.job == 2


def test_the_child_reports_a_records_hold_by_its_seat_through_a_frame(tmp_path, monkeypatch):
    from quarterdeck.accounting_slices import held_packet
    record = tmp_path / 'ordinary-native-record'
    service = SimpleNamespace(read_holds={record: held()})
    packet = held_packet(service, 'ordinary-alpha', str(record))
    assert packet == ('hold', 'ordinary-alpha', held())
    assert held_packet(service, 'ordinary-alpha', str(tmp_path / 'another-record')) is None
    assert held_packet(SimpleNamespace(), 'ordinary-alpha', str(record)) is None
    received = receive(io.BytesIO(frame(packet)))
    assert received == packet
    reader_at(monkeypatch, -1)
    assert not held_reading.due(received[2])
    reader_at(monkeypatch, 0)
    assert held_reading.due(received[2])
