"""Service restarts retain finished history until its reading code changes."""

import json
from types import SimpleNamespace

import pytest

from quarterdeck import presentation
from quarterdeck.harness import reader_stamp
from quarterdeck.service import Service
from quarterdeck.ship import Ship
from tests.unit.test_reader_stamp import copied_modules
from tests.unit.test_post_retirement import delivery
from tests.unit.test_accounting_slices import finish
from quarterdeck.accounting_slices import Driver


def setup(tmp_path, monkeypatch):
    root = copied_modules(tmp_path / 'package')
    native = reader_stamp.attributions
    monkeypatch.setattr(reader_stamp, 'attributions', lambda: native(root))
    monkeypatch.setattr(presentation, 'framework_stamp', lambda: ['synthetic initial package'])
    ship = Ship(tmp_path / 'ship', 'synthetic', 'operator', tmp_path, {}, {}, {})
    return root, ship


def start(ship):
    svc = Service(ship, SimpleNamespace())
    svc.resolved_seats = lambda: []
    svc.heartbeat = lambda: None
    svc.log = lambda *args, **kwargs: None
    svc.publish_accounting_health = lambda **kwargs: None
    return svc


@pytest.mark.parametrize('module', ['cli.py', 'post.py', 'fleet_post.py'])
def test_unrelated_landing_keeps_finished_catchup_and_allows_retirement(tmp_path, monkeypatch, module):
    root, ship = setup(tmp_path, monkeypatch)
    svc = start(ship)
    assert svc.ledger.catching_up()
    svc.account_records()
    assert not svc.ledger.catching_up()
    epoch = svc.accounting_epoch
    svc.ledger.close()
    source = reader_stamp.Path(reader_stamp.__file__).parent.parent / module
    (root / module).write_bytes(source.read_bytes() + b'\n# synthetic listing or transport revision\n')
    monkeypatch.setattr(presentation, 'framework_stamp', lambda: ['synthetic replacement package'])
    svc = start(ship)
    try:
        assert svc.accounting_epoch == epoch
        assert svc.status_framework == ['synthetic replacement package']
        assert not svc.ledger.catching_up()
        _, attempt, journal = delivery(svc)
        from quarterdeck.post_retirement import run, retired
        assert run(svc) == dict(attempts=1, journals=1)
        assert retired(attempt.path, svc.runtime).is_file()
        assert retired(journal, svc.runtime).is_file()
    finally:
        svc.ledger.close()


@pytest.mark.parametrize('module', ['harness/grok_record.py', 'order_attribution.py', 'post_message.py'])
def test_reader_or_attribution_change_reopens_and_child_finishes_the_same_epoch(tmp_path, monkeypatch, module):
    root, ship = setup(tmp_path, monkeypatch)
    svc = start(ship)
    svc.account_records()
    before = svc.accounting_epoch
    svc.ledger.close()
    path = root / module
    path.write_bytes(path.read_bytes() + b'\n# synthetic reading revision\n')
    svc = start(ship)
    driver = Driver()
    try:
        assert svc.accounting_epoch != before
        assert svc.ledger.catching_up()
        finish(driver, svc)
        assert not svc.ledger.catching_up()
        stored = svc.ledger.db.execute('SELECT epoch,pending FROM accounting_progress').fetchone()
        assert stored == (json.dumps(svc.accounting_epoch), 0)
    finally:
        driver.close()
        svc.ledger.close()


def test_unfinished_catchup_stays_pending_across_unrelated_restart(tmp_path, monkeypatch):
    _, ship = setup(tmp_path, monkeypatch)
    svc = start(ship)
    before = svc.accounting_epoch
    svc.ledger.close()
    monkeypatch.setattr(presentation, 'framework_stamp', lambda: ['synthetic replacement package'])
    svc = start(ship)
    try:
        assert svc.accounting_epoch == before
        assert svc.ledger.catching_up()
        svc.account_records()
        assert not svc.ledger.catching_up()
    finally:
        svc.ledger.close()


@pytest.mark.parametrize('module', ['harness/grok_record.py', 'order_attribution.py', 'post_message.py'])
def test_unreadable_code_does_not_certify_catchup_completion(tmp_path, monkeypatch, module):
    root, ship = setup(tmp_path, monkeypatch)
    svc = start(ship)
    svc.account_records()
    svc.ledger.close()
    (root / module).unlink()
    for _ in range(2):
        svc = start(ship)
        try:
            assert svc.accounting_epoch is None
            assert svc.ledger.catching_up()
            svc.account_records()
            assert svc.ledger.catching_up()
        finally:
            svc.ledger.close()
