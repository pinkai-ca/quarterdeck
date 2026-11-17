"""The accounting's health file is written when what it shows changes, or a job ends or fails, at once; otherwise once
a pass, as the publisher's own rule has it, however many drains step a running job. Synthetic records only."""

import time

from quarterdeck import accounting_health, accounting_slices
from quarterdeck.accounting_slices import Driver
from quarterdeck.service import Service
from .test_accounting_slices import world


def counted(svc, monkeypatch):
    """The service's real publisher, its writes counted; a pass long enough that only a forced write repeats."""
    writes = []
    monkeypatch.setattr(accounting_health, 'publish', lambda runtime, health: writes.append(health.shown()))
    svc.publish_accounting_health = Service.publish_accounting_health.__get__(svc)
    svc.accounting_health_due = 0.0
    svc.ship = svc.ship.__class__(**{**svc.ship.__dict__, 'tick': 3600.0})  # one pass for the whole test
    return writes


def test_drains_of_a_running_job_write_the_health_at_most_once_a_pass_and_its_end_at_once(tmp_path, monkeypatch):
    svc, _, _, _ = world(tmp_path, monkeypatch, 300)
    monkeypatch.setattr(accounting_slices, 'BUDGET', 0.002)
    writes = counted(svc, monkeypatch)
    driver = Driver()
    try:
        steps, ended_at = 0, None
        until = time.monotonic() + 30
        while time.monotonic() < until:
            driver.step(svc) if not driver.active else driver.drain(svc)
            steps += 1
            if driver.job and not driver.active:
                ended_at = len(writes)
                break
        assert ended_at is not None and steps > 10, 'a job that took many steps, and ended'
        assert ended_at <= 2, f'{ended_at} writes over {steps} steps: once a pass, then its end at once'
        assert writes[-1] is None, "the job's end, published at once within the pass"
    finally:
        driver.close()
        svc.ledger.close()


def test_a_failure_within_a_drain_is_published_at_once(tmp_path, monkeypatch):
    svc, _, _, _ = world(tmp_path, monkeypatch, 300)
    monkeypatch.setattr(accounting_slices, 'BUDGET', 0.002)
    writes = counted(svc, monkeypatch)
    driver = Driver()
    try:
        until = time.monotonic() + 30
        while not driver.active and time.monotonic() < until:
            driver.step(svc)
        assert driver.active
        before = len(writes)

        def broken():
            raise OSError('synthetic read failure')
        monkeypatch.setattr(driver, 'pump', broken)
        driver.drain(svc)
        assert svc.accounting_health.failures == 1
        assert len(writes) == before + 1, 'the failure written at once, within the same pass'
    finally:
        driver.close()
        svc.ledger.close()
