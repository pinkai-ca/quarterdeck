"""Only a parent's current full generation can finish its identity retry."""

import time
import os
import signal
from types import SimpleNamespace

from quarterdeck import accounting_gate, accounting_slices, record_owner
from quarterdeck.accounting_slices import Driver, FullContinuation
from .test_accounting_slices import finish, world


FENCE = ('UPDATE accounting_progress SET pending=0 WHERE singleton=1', (), False)


def ready(tmp_path, monkeypatch):
    svc, seat, record, _ = world(tmp_path, monkeypatch)
    svc.ship.file.write_text('[ship]\nname="ordinary"\ncode="checkout"\n')
    seat.spec = SimpleNamespace(model='ordinary-model')
    seat.pool = SimpleNamespace(name='ordinary-pool')
    monkeypatch.setattr(accounting_gate.time, 'time_ns', lambda: 9_000_000_000_000_000_000)
    driver = Driver()
    finish(driver, svc)
    assert driver.gate.partitions.baseline is not None
    record.write_text(record.read_text() + '\n')
    return svc, seat, record, driver


def test_record_change_job_cannot_finish_full_continuation_with_the_same_seats(tmp_path, monkeypatch):
    svc, seat, _, driver = ready(tmp_path, monkeypatch)
    callbacks = driver.callbacks
    held = []
    def complete(service, deadline):
        if driver.scope == {seat.name} and any(packet[0] == 'done' for packet, _ in driver.events):
            assert driver.continuing is None and driver.completion == []
            continuation = FullContinuation(driver.full_generation, (FENCE,), {seat.name})
            driver.full_continuation = continuation
            driver.retry_generation = driver.full_generation
            held.append(continuation)
            service.ledger.begin_catchup(service.accounting_epoch, force=True)
        return callbacks(service, deadline)
    monkeypatch.setattr(driver, 'callbacks', complete)
    try:
        finish(driver, svc)
        assert driver.scope == {seat.name} and len(held) == 1
        assert svc.ledger.catching_up()
        assert held[0].remaining == {seat.name}
        assert not driver.retry_seats, 'a record-triggered scope acquired no full continuation'
        monkeypatch.setattr(driver, 'callbacks', callbacks)
        finish(driver, svc)
        assert driver.scope is None and not svc.ledger.catching_up()
    finally:
        driver.close()
        svc.ledger.close()


def test_record_scope_rejects_a_child_global_completion(tmp_path, monkeypatch):
    svc, seat, _, driver = ready(tmp_path, monkeypatch)
    callbacks = driver.callbacks
    injected = []
    def complete(service, deadline):
        if driver.scope == {seat.name} and any(packet[0] == 'done' for packet, _ in driver.events):
            service.ledger.begin_catchup(service.accounting_epoch, force=True)
            driver.completion.append(FENCE)
            injected.append(True)
        return callbacks(service, deadline)
    monkeypatch.setattr(driver, 'callbacks', complete)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            driver.step(svc)
            if injected:
                break
        assert injected and not driver.active
        assert svc.accounting_health.failures == 1
        assert svc.ledger.catching_up(), 'the scoped child never executes a global fence'
        assert driver.full_continuation is None
    finally:
        driver.close()
        svc.ledger.close()


def test_identity_retry_is_refused_when_its_full_generation_moves(tmp_path, monkeypatch):
    svc, seat, _, _ = world(tmp_path, monkeypatch)
    class ChurnOnce(Driver):
        def start(self, service):
            result = super().start(service)
            if self.job == 1:
                with record_owner.registry(seat.home.parent).open('a') as stream:
                    stream.write('\n')
            return result
    driver = ChurnOnce()
    try:
        finish(driver, svc)
        assert driver.full_continuation is not None and driver.retry_seats == {seat.name}
        old = driver.full_continuation
        driver.full_generation += 1
        driver.continuing = old
        assert not driver.current_continuation()
        driver.continuing = None
        finish(driver, svc)
        assert driver.scope is None, 'stale retry must be replaced by a full job'
        assert not svc.ledger.catching_up()
        assert driver.full_continuation is None
    finally:
        driver.close()
        svc.ledger.close()


def test_active_record_scope_yields_to_the_full_backstop(tmp_path, monkeypatch):
    svc, seat, _, driver = ready(tmp_path, monkeypatch)
    start = driver.start
    def stopped(service):
        result = start(service)
        assert driver.scope == {seat.name}
        os.kill(driver.process.pid, signal.SIGSTOP)
        assert os.WIFSTOPPED(os.waitpid(driver.process.pid, os.WUNTRACED)[1])
        return result
    monkeypatch.setattr(driver, 'start', stopped)
    monkeypatch.setattr(accounting_slices, 'BUDGET', 0.02)
    try:
        driver.step(svc)
        assert driver.active and driver.scope == {seat.name}
        scoped_generation = driver.full_generation
        driver.gate.partitions.due = time.monotonic() - 1
        monkeypatch.setattr(driver, 'start', start)
        finish(driver, svc)
        assert driver.scope is None and not svc.ledger.catching_up()
        assert driver.full_generation > scoped_generation
        assert driver.gate.partitions.due > time.monotonic()
    finally:
        driver.close()
        svc.ledger.close()
