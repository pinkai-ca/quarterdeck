"""Synthetic record -> Watch -> Service guarded-resume composition; no program is launched."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from quarterdeck import auth_failure, recovery, watch
from quarterdeck.harness import claude
from quarterdeck.harness.types import AUTHENTICATION
from quarterdeck.service import Service
from tests.authentication_shapes import START, write_record


@pytest.mark.parametrize('mode', ['valid-newer', 'expired', 'human'])
def test_recorded_authentication_is_visible_and_only_eligible_crew_resumes(tmp_path, monkeypatch, mode):
    record = write_record(tmp_path / 'synthetic-record.jsonl')
    now = datetime.now(timezone.utc)
    source = tmp_path / 'synthetic-pool-metadata'
    source.write_text('synthetic metadata stand-in')
    pool = SimpleNamespace(name='synthetic-pool')
    launched = SimpleNamespace(pool=pool.name, at=START)
    ship = SimpleNamespace(runtime=tmp_path / 'runtime', pools={pool.name: pool})
    seat = SimpleNamespace(name='synthetic-seat', ship=ship, session=SimpleNamespace(human=mode == 'human'),
                           launched=lambda: launched, record=Mock(return_value=record),
                           pane=lambda: SimpleNamespace(dead=False),
                           join=lambda: SimpleNamespace(record=record), is_up=True,
                           harness=claude.ClaudeHarness(), restart=Mock(return_value='synthetic resumed'))
    monkeypatch.setattr(auth_failure.credential, 'pool_file', lambda p: source)
    expiry = now.timestamp() + (3600 if mode != 'expired' else -3600)
    monkeypatch.setattr(auth_failure.credential, 'freshness', lambda p, f: (expiry, source.stat().st_mtime))
    monkeypatch.setattr(auth_failure.credential, 'grant_expiry', lambda p: now + timedelta(days=7))
    monkeypatch.setattr(recovery, 'held', lambda s: None)
    activity = watch.activity(seat)
    assert activity.state == AUTHENTICATION
    assert activity.state != 'idle'
    if mode == 'expired':
        assert 'login needed' in activity.detail
    elif mode == 'human':
        assert 'human session needs attention' in activity.detail
    service = Service.__new__(Service)
    service.ship = ship
    service.resolved_seats = lambda: [seat]
    service.log = Mock()
    service.needs_human = Mock()
    service.authentications()
    if mode == 'valid-newer':
        seat.restart.assert_called_once_with()
        service.authentications()  # same failed episode cannot relaunch again
        seat.restart.assert_called_once_with()
        assert 'one relaunch with resume' in service.log.call_args.kwargs['proof']
    else:
        seat.restart.assert_not_called()
        assert not auth_failure._path(seat).exists()
    if mode == 'human':
        seat.record.assert_not_called()
