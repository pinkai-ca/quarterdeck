"""Synthetic record -> Watch -> Service guarded-resume composition; no program is launched."""
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from quarterdeck import auth_failure, recovery, watch
from quarterdeck.harness import claude
from quarterdeck.harness.types import AUTHENTICATION
from quarterdeck.service import Service
from quarterdeck.ship import Pool
from tests.authentication_shapes import START, write_record


@pytest.mark.parametrize('mode', [
    'valid-newer', 'renewable', 'refresh-key-only', 'expired-grant', 'missing-refresh', 'no-renewal-deadline',
    'invalid-expiry', 'invalid-renewal-deadline', 'invalid-file', 'absent-file', 'human', 'human-renewable',
])
def test_recorded_authentication_is_visible_and_only_eligible_crew_resumes(tmp_path, monkeypatch, mode):
    record = write_record(tmp_path / 'synthetic-record.jsonl')
    now = datetime.now(timezone.utc)
    source = tmp_path / '.credentials.json'
    old_access = mode not in ('valid-newer', 'human')
    oauth = {'expiresAt': int((now + timedelta(hours=-1 if old_access else 1)).timestamp() * 1000)}
    if mode != 'missing-refresh':
        oauth['refreshToken'] = None if mode == 'refresh-key-only' else 'made-up nonempty marker'
    if mode not in ('no-renewal-deadline', 'human-renewable'):
        expiry = now + timedelta(days=-1 if mode == 'expired-grant' else 7)
        oauth['refreshTokenExpiresAt'] = int(expiry.timestamp() * 1000)
    if mode == 'invalid-expiry':
        oauth['expiresAt'] = float('inf')
    if mode == 'invalid-renewal-deadline':
        oauth['refreshTokenExpiresAt'] = 'malformed deadline'
    if mode != 'absent-file':
        source.write_text('missing JSON' if mode == 'invalid-file' else json.dumps({'claudeAiOauth': oauth}))
    pool = Pool('synthetic-pool', 'anthropic', config_dir=tmp_path)
    launched = SimpleNamespace(pool=pool.name, at=START, pane='synthetic-pane',
                               pid=12345, harness='claude')
    ship = SimpleNamespace(runtime=tmp_path / 'runtime', pools={pool.name: pool})
    seat = SimpleNamespace(name='synthetic-seat', ship=ship, session=SimpleNamespace(human=mode.startswith('human')),
                           folder=ship.runtime / 'seats' / 'synthetic-seat', trusted=ship.runtime / 'seats' / 'synthetic-seat',
                           launched=lambda: launched, record=Mock(return_value=record),
                           pane=lambda: SimpleNamespace(dead=False, id=launched.pane, pid=launched.pid),
                           join=lambda: SimpleNamespace(record=record), is_up=True,
                           harness=claude.ClaudeHarness(), restart=Mock(return_value='synthetic resumed'))
    monkeypatch.setattr(recovery, 'held', lambda s: None)
    activity = watch.activity(seat)
    assert activity.state == AUTHENTICATION
    assert activity.state != 'idle'
    if mode in ('expired-grant', 'missing-refresh'):
        assert 'login needed' in activity.detail
    elif mode in ('invalid-expiry', 'invalid-renewal-deadline', 'invalid-file', 'absent-file'):
        assert 'pool sign-in validity unknown' in activity.detail
    elif mode in ('renewable', 'refresh-key-only', 'no-renewal-deadline'):
        assert auth_failure.RENEWABLE in activity.detail
        assert auth_failure.waiting(auth_failure.status(seat))
    elif mode.startswith('human'):
        assert 'human session needs attention' in activity.detail
        if mode == 'human-renewable':
            assert auth_failure.RENEWABLE in activity.detail
    service = Service.__new__(Service)
    service.ship = ship
    service.resolved_seats = lambda: [seat]
    service.log = Mock()
    service.needs_human = Mock()
    service.authentications()
    if mode in ('valid-newer', 'renewable', 'refresh-key-only', 'no-renewal-deadline'):
        seat.restart.assert_called_once_with()
        service.authentications()  # same failed episode cannot relaunch again
        seat.restart.assert_called_once_with()
        proof = service.log.call_args.kwargs['proof']
        assert 'relaunch with resume (1/3)' in proof
        if mode in ('renewable', 'refresh-key-only', 'no-renewal-deadline'):
            assert auth_failure.RENEWABLE in proof
    else:
        seat.restart.assert_not_called()
        assert not auth_failure._path(seat).exists()
    if mode.startswith('human'):
        seat.record.assert_not_called()
