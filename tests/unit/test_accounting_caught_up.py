"""The accounting says it has caught up at the first valid full job with no retry after the service starts or a
catch-up begins, whether or not that job wrote, and says whether it wrote; never again until a catch-up begins anew.
Synthetic records only."""

from quarterdeck.accounting_slices import Driver
from .test_accounting_slices import finish, world


def caught_up(notices):
    return [kwargs for args, kwargs in notices if args == ('accounting-caught-up',)]


def full_job(driver, svc):
    """One more full job: the gate's certificate dropped, as after a rejected job."""
    driver.gate.reject()
    finish(driver, svc)
    assert driver.scope is None and not driver.invalid, 'a valid full job with no retry'


def test_caught_up_once_after_a_start_then_again_only_when_a_catch_up_begins_anew(tmp_path, monkeypatch):
    svc, _, _, _ = world(tmp_path, monkeypatch)
    notices = []
    svc.log = lambda *args, **kwargs: notices.append((args, kwargs))
    driver = Driver()
    try:
        finish(driver, svc)  # the first job after the start reads the record and writes its turns
        assert driver.scope is None and not driver.invalid and not svc.ledger.catching_up()
        first = caught_up(notices)
        assert len(first) == 1 and first[0]['wrote'] is True and first[0]['seconds'] >= 0

        full_job(driver, svc)  # nothing new to write: current, and already said so
        assert caught_up(notices) == first

        # A catch-up begun anew (a new epoch): its first valid full job says caught up, though it writes nothing.
        svc.accounting_epoch = {'synthetic': 'another epoch'}
        svc.ledger.begin_catchup(svc.accounting_epoch)
        assert svc.ledger.catching_up()
        full_job(driver, svc)
        assert not svc.ledger.catching_up()
        again = caught_up(notices)[1:]
        assert len(again) == 1 and again[0]['wrote'] is False

        full_job(driver, svc)
        assert len(caught_up(notices)) == 2
    finally:
        driver.close()
        svc.ledger.close()


def test_a_first_full_job_with_nothing_to_write_still_says_caught_up(tmp_path, monkeypatch):
    svc, _, _, _ = world(tmp_path, monkeypatch)
    driver = Driver()
    try:
        finish(driver, svc)  # a first service's job, which writes
    finally:
        driver.close()
    notices = []
    svc.log = lambda *args, **kwargs: notices.append((args, kwargs))
    restarted = Driver()  # the service starts again over a ledger that is current
    try:
        finish(restarted, svc)
        assert restarted.scope is None and not restarted.invalid
        said = caught_up(notices)
        assert len(said) == 1 and said[0]['wrote'] is False, said
    finally:
        restarted.close()
        svc.ledger.close()
