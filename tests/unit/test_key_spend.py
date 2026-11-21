"""What each key has spent, and a one-off prepaid credit as its limit. No key is read here."""

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from quarterdeck.ledger import Budget, Ledger
from quarterdeck.ledger_reading import key_spends
from quarterdeck.ship import Pool, ShipError

NOW = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
PREPAID = '50 USD from 2026-11-21 until 2026-12-15'


def book(tmp_path, spends):
    ledger = Ledger(tmp_path / 'ledger.sqlite')
    ledger.spend = lambda pool, since: spends.get(pool, (None, 0))
    return ledger


def ship(*pools):
    return SimpleNamespace(pools={pool.name: pool for pool in pools})


def test_a_prepaid_budget_reads_its_amount_and_dates():
    budget = Budget.parse(PREPAID)
    assert (budget.amount, budget.currency, budget.start, budget.end) == (50.0, 'USD', '2026-11-21', '2026-12-15')
    assert budget.period_start(NOW) == datetime(2026, 11, 21, tzinfo=timezone.utc)
    assert budget.shown == '50 USD prepaid until 2026-12-15'


def test_a_prepaid_budget_is_plain_data_for_json():
    assert json.loads(json.dumps(asdict(Budget.parse(PREPAID))))['end'] == '2026-12-15'


def test_a_recurring_budget_reads_as_before():
    budget = Budget.parse('200 USD/month')
    assert (budget.amount, budget.period) == (200.0, 'month') and not hasattr(budget, 'start')
    assert budget.shown == '200 USD/month' and not budget.ended(NOW)


@pytest.mark.parametrize('text', ['50 USD from 2026-12-15 until 2026-11-21', '50 USD from 2026-13-01 until 2026-12-15',
                                  '50 USD until 2026-12-15', 'a lot'])
def test_a_budget_that_cannot_be_read_is_refused(text):
    with pytest.raises(ShipError, match='budget'):
        Budget.parse(text)


@pytest.mark.parametrize('now,ended', [(datetime(2026, 12, 15, 23, 59, tzinfo=timezone.utc), False),
                                       (datetime(2026, 12, 16, 0, 0, tzinfo=timezone.utc), True)])
def test_a_prepaid_credit_lasts_through_its_last_date(now, ended):
    assert Budget.parse(PREPAID).ended(now) is ended


@pytest.mark.parametrize('spent,now,walled', [
    (49.99, NOW, False), (50.0, NOW, True), (1.0, NOW + timedelta(days=15), True), (None, NOW + timedelta(days=15), True),
    (None, NOW, False),
])
def test_the_wall_stands_when_the_amount_is_spent_or_the_end_has_passed(tmp_path, spent, now, walled):
    pool = Pool('paid', 'anthropic', api_key='keys:anthropic', budget=PREPAID)
    ledger = book(tmp_path, {'paid': (spent, 0)})
    try:
        assert ('paid budget' in ledger.walls(ship(pool), now)) is walled
    finally:
        ledger.close()


def test_two_pools_on_one_key_share_its_limit(tmp_path):
    one = Pool('one', 'anthropic', api_key='keys:anthropic', budget=PREPAID)
    two = Pool('two', 'local', endpoint='https://example.invalid/v1', api_key='keys:anthropic', budget=PREPAID)
    other = Pool('other', 'openai', api_key='keys:openai')
    ledger = book(tmp_path, {'one': (30.0, 1), 'two': (25.0, 2), 'other': (99.0, 0)})
    try:
        assert ledger.key_spend(ship(one, two, other), one, NOW) == (55.0, 3)
        walls = ledger.walls(ship(one, two, other), NOW)
        assert 'one budget' in walls and 'two budget' in walls and 'other budget' not in walls
    finally:
        ledger.close()


def test_each_key_says_what_it_has_spent_and_what_is_left(tmp_path):
    paid = Pool('paid', 'anthropic', api_key='keys:anthropic', budget=PREPAID)
    open_ended = Pool('plain', 'openai', api_key='keys:openai')
    unread = Pool('unread', 'xai', api_key='keys:xai')
    subscription = Pool('sub', 'anthropic')
    ledger = book(tmp_path, {'paid': (12.5, 0), 'plain': (3.0, 2)})
    try:
        lines = key_spends(ship(paid, open_ended, unread, subscription), ledger, NOW)
    finally:
        ledger.close()
    assert lines[0] == 'key anthropic: 12.50 USD spent since 2026-11-21 of 50 USD prepaid until 2026-12-15, 37.50 left'
    assert lines[1].startswith('key openai: ') and 'since 2026-12-01' in lines[1] and '(2 turns with no price)' in lines[1]
    assert lines[2] == 'key xai: no cost stated since 2026-12-01'
    assert len(lines) == 3, 'a subscription has no key'
