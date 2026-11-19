"""Extra-usage evidence from made refusals and the ordinary accounting paths."""

from dataclasses import replace
from datetime import timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from quarterdeck import doctor, post, watch
from quarterdeck.accounting_slices import Driver
from quarterdeck.harness import claude_extra_usage as extra_usage
from quarterdeck.harness.claude_limits import extra_usage_at_refusal, rejected_limit_info

from .test_accounting_slices import finish
from .test_claude_model_allowances import scoped  # noqa: F401
from .test_cross_program_failover import running  # noqa: F401


def refusal(at, **scalars):
    return {
        'type': 'assistant', 'isApiErrorMessage': True, 'error': 'rate_limit', 'apiError': 'usage_limit_reached',
        'timestamp': at.isoformat(), 'uuid': 'made-uuid-' + at.isoformat(),
        'apiErrorParams': {'rate_limit_info': {
            'status': 'rejected', 'rateLimitType': 'seven_day', 'resetsAt': int((at + timedelta(days=1)).timestamp()),
            **scalars,
        }},
        'message': {'id': 'made-message-' + at.isoformat(), 'role': 'assistant', 'stop_reason': 'stop_sequence',
                    'content': [{'type': 'text', 'text': 'made record prose never projected'}]},
    }


def append(r, **values):
    at = post.now().astimezone(timezone.utc)
    path = r.seat.record()
    assert path == r.created[-1].path
    with path.open('a') as stream:
        stream.write(json.dumps(refusal(at, **values)) + '\n')
    return at


def readings(r):
    return {row['pool']: row for row in extra_usage.readings(r.seat.ship)}


@pytest.mark.parametrize(('fields', 'setting', 'detail', 'literals'), [
    ({'overageStatus': 'rejected', 'overageDisabledReason': 'org_level_disabled'},
     'off', 'org_level_disabled', None),
    ({'overageStatus': 'allowed'}, 'unverified', None, ('allowed', None)),
    ({'overageStatus': True, 'overageDisabledReason': 'another_form'}, 'unverified', None, (True, 'another_form')),
    ({}, 'unavailable', 'setting missing', None),
    ({'overageStatus': ['made arbitrary data']}, 'unavailable', 'setting malformed', None),
    ({'overageDisabledReason': 'made arbitrary data'}, 'unavailable', 'setting malformed', None),
])
def test_parse_only_bounded_scalar_metadata(fields, setting, detail, literals):
    class MetadataRow(dict):
        def get(self, key, *args):
            assert key not in ('message', 'content')
            return super().get(key, *args)

    at = post.now()
    row = MetadataRow(refusal(at, **fields))
    observed = extra_usage_at_refusal(rejected_limit_info(row), at)
    assert (observed.at, observed.setting, observed.detail, observed.literals) == (at, setting, detail, literals)


@pytest.mark.parametrize('sliced', [False, True], ids=['direct', 'sliced'])
def test_verified_off_retains_native_time_and_paying_pool(scoped, sliced):
    r = scoped
    assert readings(r)['second']['read_at'] is None
    at = append(r, overageStatus='rejected', overageDisabledReason='org_level_disabled')
    if sliced:
        driver = Driver()
        try:
            finish(driver, r.svc)
        finally:
            driver.close()
    else:
        r.svc.account()

    rows = readings(r)
    assert rows['second']['setting'] == 'off'
    assert rows['second']['read_at'] == at.isoformat()
    assert rows['second']['overageDisabledReason'] == 'org_level_disabled'
    assert rows['third']['setting'] == 'unavailable' and rows['third']['read_at'] is None
    assert all(rows['second'][key] is None for key in ('cumulative_spend', 'monthly_limit', 'currency', 'period'))
    [old] = extra_usage.collect(r.svc.ledger.db, ['second'], at + timedelta(hours=1))
    assert old['setting'] == 'stale' and old['read_at'] == at.isoformat()
    assert 'last reported off (org_level_disabled)' in extra_usage.describe(old)


def test_status_and_doctor_show_the_same_reading(scoped, monkeypatch):
    r = scoped
    at = append(r, overageStatus='rejected', overageDisabledReason='org_level_disabled')
    r.svc.account()
    monkeypatch.setattr(watch, 'seats', lambda *args: [r.seat])
    status = watch.status_reading(r.seat.ship, r.seat.tmux)
    expect = f'extra usage: off (org_level_disabled) · read {at.isoformat()}'
    assert any(line.startswith('second: ') and expect in line for line in watch.render_table(status))
    monkeypatch.setattr(doctor, 'pool_check', lambda pool: doctor.Check(f'pool {pool.name}', True, 'made readiness'))
    monkeypatch.setattr(doctor, 'seat_readiness', lambda *args, **kwargs: [])
    monkeypatch.setattr(doctor, 'program_version', lambda *args: 'made version')
    found = doctor.checks(r.seat.ship)
    assert any(row.name == 'pool second' and row.passed and expect in row.detail for row in found)
    assert 'made record prose' not in json.dumps(status['extra_usage'])


def test_unverified_changes_notice_desk_once_and_retry_the_same_post(scoped, monkeypatch):
    r = scoped
    desk = 'made-recipient'
    r.seat.ship.seats[desk] = replace(r.seat.spec, name=desk, role='desk')
    r.svc.pool_forecasts()
    assert not r.svc.store.for_seat(desk)

    at = append(r, overageStatus='allowed')
    r.svc.account()
    assert readings(r)['second']['setting'] == 'unverified'
    assert readings(r)['second']['overageStatus'] == 'allowed'
    save = extra_usage.durable.replace_file

    def interrupted(path, text):
        if path.name == 'claude-extra-usage-alerts.json' and '"published": true' in text:
            raise OSError('made interruption after publication')
        save(path, text)

    with monkeypatch.context() as cut:
        cut.setattr(extra_usage.durable, 'replace_file', interrupted)
        with pytest.raises(OSError, match='after publication'):
            r.svc.pool_forecasts()
    [notice] = r.svc.store.for_seat(desk)
    assert 'unverified' in notice.body and 'overageStatus="allowed"' in notice.body and at.isoformat() in notice.body
    successor = SimpleNamespace(ship=r.svc.ship, runtime=r.svc.runtime, store=post.Store(r.svc.runtime))
    extra_usage.notify(successor)
    extra_usage.notify(successor)
    assert successor.store.for_seat(desk) == [notice]

    missing_at = append(r)
    r.svc.account()
    missing = readings(r)['second']
    assert missing['setting'] == 'unavailable' and missing['read_at'] == missing_at.isoformat()
    append(r, overageStatus='allowed')
    r.svc.account()
    extra_usage.notify(successor)
    assert successor.store.for_seat(desk) == [notice]

    append(r, overageStatus='unmeasured_form', overageDisabledReason=True)
    r.svc.account()
    extra_usage.notify(successor)
    extra_usage.notify(successor)
    reports = successor.store.for_seat(desk)
    assert len(reports) == 2 and 'overageDisabledReason=true' in reports[-1].body
    assert all(report.to == desk and 'made record prose' not in report.body for report in reports)
