"""A wall-clock reversal does not latch a temporary usage hold forever."""
from datetime import datetime, timedelta, timezone
import sqlite3
from types import SimpleNamespace

from quarterdeck import quota


def test_backward_clock_hold_recovers_without_human_action(monkeypatch):
    real_datetime = datetime
    now = [datetime(2030, 1, 1, tzinfo=timezone.utc)]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now[0]
    db = sqlite3.connect(':memory:')
    book = quota.Quotas(db)
    at = now[0]
    deadline = at + timedelta(hours=1)
    db.execute('INSERT INTO usage_holds VALUES (?,?,?,?)',
               ('synthetic', deadline.isoformat(), at.isoformat(), 'native refusal'))
    events = []
    service = SimpleNamespace(ledger=SimpleNamespace(quota=book), usage_hold={}, usage_logged={},
                              log=lambda *args, **fields: events.append(fields))
    monkeypatch.setattr(quota, 'datetime', Clock)
    assert quota.held(service, 'synthetic')
    now[0] = at - timedelta(seconds=30)
    assert quota.held(service, 'synthetic')
    assert len(events) == 1 and events[0]['no_call']
    # The invalid-future diagnosis is recomputed, not stored as an indefinite hold.
    now[0] = at + timedelta(seconds=1)
    assert quota.held(service, 'synthetic')
    assert book.usage_hold('synthetic', now[0]) == deadline
    now[0] = deadline
    assert not quota.held(service, 'synthetic')
    assert len(events) == 1
    assert db.execute('SELECT retry_at FROM usage_holds').fetchone()[0] == deadline.isoformat()
    db.close()
