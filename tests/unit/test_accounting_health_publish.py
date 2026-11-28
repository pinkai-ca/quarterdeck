"""The accounting's health file is written when what it shows changes, or a job ends or fails, at once; otherwise once
a pass, as the publisher's own rule has it, however many drains step a running job. Synthetic records only."""

from types import SimpleNamespace

from quarterdeck import accounting_health, accounting_slices, service
from quarterdeck.accounting_slices import Driver
from quarterdeck.service import Service
from .test_accounting_slices import finish, world


def counted(svc, monkeypatch):
    """Count writes from the real publisher without advancing its pass clock."""
    writes = []
    monkeypatch.setattr(accounting_health, 'publish', lambda runtime, health: writes.append(health.shown()))
    svc.publish_accounting_health = Service.publish_accounting_health.__get__(svc)
    svc.accounting_health_due = 0.0
    monkeypatch.setattr(service, 'time', SimpleNamespace(monotonic=lambda: 0.0))
    return writes


def test_drains_of_a_running_job_write_the_health_at_most_once_a_pass_and_its_end_at_once(tmp_path, monkeypatch):
    svc, _, _, _ = world(tmp_path, monkeypatch, 300)
    writes = counted(svc, monkeypatch)
    driver = Driver()
    try:
        # Keep the request from the child until the repeated drains have run.
        with monkeypatch.context() as running:
            running.setattr(accounting_slices, 'time', SimpleNamespace(monotonic=lambda: 0.0))
            running.setattr(driver, 'pump', lambda: False)
            driver.step(svc, wait=False)
            assert driver.active and driver.outgoing
            for _ in range(12):
                driver.drain(svc)
            assert driver.active and len(writes) == 1
        finish(driver, svc)
        assert len(writes) == 2, 'one write per pass and a forced write at completion'
        assert writes[-1] is None, "the job's end, published at once within the pass"
    finally:
        driver.close()
        svc.ledger.close()


def test_a_failure_within_a_drain_is_published_at_once(tmp_path, monkeypatch):
    svc, _, _, _ = world(tmp_path, monkeypatch, 300)
    writes = counted(svc, monkeypatch)
    driver = Driver()
    monkeypatch.setattr(accounting_slices, 'time', SimpleNamespace(monotonic=lambda: 0.0))
    monkeypatch.setattr(driver, 'pump', lambda: False)
    try:
        driver.step(svc, wait=False)
        assert driver.active and driver.outgoing
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
