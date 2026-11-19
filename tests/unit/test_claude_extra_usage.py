"""Extra-usage evidence from made refusals and the ordinary accounting paths."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from types import SimpleNamespace

import pytest

from quarterdeck import compaction_alert, doctor, post, watch
from quarterdeck.accounting_slices import Driver
from quarterdeck.harness import claude_extra_usage as extra_usage
from quarterdeck.harness.claude_limits import extra_usage_at_refusal, rejected_limit_info
from quarterdeck.ledger import Ledger

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


def append(r, *, at=None, **values):
    at = at or post.now().astimezone(timezone.utc)
    path = r.seat.record()
    assert path == r.created[-1].path
    with path.open('a') as stream:
        stream.write(json.dumps(refusal(at, **values)) + '\n')
    return at


def readings(r):
    return {row['pool']: row for row in extra_usage.readings(r.seat.ship)}


def add_desk(r):
    desk = 'made-recipient'
    r.seat.ship.seats[desk] = replace(r.seat.spec, name=desk, role='desk')
    return desk


def restarted(service):
    return SimpleNamespace(ship=service.ship, runtime=service.runtime, store=post.Store(service.runtime))


def legacy_table(path, rows):
    with sqlite3.connect(path) as old:
        old.execute('CREATE TABLE claude_extra_usage(pool TEXT, read_at TEXT, setting TEXT, detail TEXT, '
                    'literals TEXT, PRIMARY KEY(pool,read_at))')
        old.executemany('INSERT INTO claude_extra_usage VALUES (?,?,?,?,?)', rows)


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
    desk = add_desk(r)
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
    successor = restarted(r.svc)
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


def test_late_out_of_order_evidence_still_notices_without_replacing_latest_status(scoped, monkeypatch):
    r = scoped
    desk = add_desk(r)
    earlier = post.now().astimezone(timezone.utc)
    later = earlier + timedelta(minutes=1)
    now = later + timedelta(hours=3)
    monkeypatch.setattr(post, 'now', lambda: now)

    append(r, at=later, overageStatus='first_seen')
    r.svc.account()
    r.svc.pool_forecasts()
    [first] = r.svc.store.for_seat(desk)
    assert first.body.startswith('second: ') and 'first_seen' in first.body and '10800 seconds' in first.body

    append(r, at=earlier, overageStatus='older_seen_later')
    r.svc.account()
    successor = restarted(r.svc)
    extra_usage.notify(successor)
    extra_usage.notify(successor)
    reports = successor.store.for_seat(desk)
    assert len(reports) == 2 and first in reports
    [late] = [report for report in reports if report != first]
    assert late.body.startswith('second: ') and 'older_seen_later' in late.body and '10860 seconds' in late.body
    assert readings(r)['second']['read_at'] == later.isoformat()


def test_malformed_notice_is_content_free_repeated_evidence_and_exact_off_are_quiet(scoped, monkeypatch):
    r = scoped
    desk = add_desk(r)
    at = post.now().astimezone(timezone.utc)
    monkeypatch.setattr(post, 'now', lambda: at + timedelta(hours=2))

    append(r, at=at, overageStatus=['made private payload'])
    r.svc.account()
    r.svc.pool_forecasts()
    [first] = r.svc.store.for_seat(desk)
    assert first.body == (f'second: Claude reported malformed extra-usage fields at {at.isoformat()}. '
                          'Reading age when queued: 7200 seconds.')

    append(r, at=at + timedelta(seconds=1), overageDisabledReason='secret_made')
    append(r, at=at + timedelta(seconds=2))
    append(r, at=at + timedelta(seconds=3), overageStatus='rejected', overageDisabledReason='org_level_disabled')
    append(r, at=at + timedelta(seconds=3))  # missing evidence cannot turn a known row into a conflict
    r.svc.account()
    successor = restarted(r.svc)
    extra_usage.notify(successor)
    extra_usage.notify(successor)
    assert successor.store.for_seat(desk) == [first]
    assert readings(r)['second']['overageDisabledReason'] == 'org_level_disabled'

    append(r, at=at + timedelta(seconds=4), overageStatus=['made private payload'])
    r.svc.account()
    extra_usage.notify(successor)
    reports = successor.store.for_seat(desk)
    assert len(reports) == 2 and all('made private payload' not in report.body for report in reports)


def test_same_time_conflict_follows_unchanged_pending_notice(scoped, monkeypatch):
    r = scoped
    desk = add_desk(r)
    at = append(r, overageStatus='one_form')
    r.svc.account()

    def cut(*args):
        raise OSError('made interruption before publication')

    with monkeypatch.context() as broken:
        broken.setattr(compaction_alert, 'publish', cut)
        with pytest.raises(OSError, match='before publication'):
            r.svc.pool_forecasts()
    assert r.svc.store.for_seat(desk) == []
    journal = r.svc.runtime / 'claude-extra-usage-alerts.json'
    pending = post.Message.from_json(json.loads(journal.read_text())['second']['intent'])

    append(r, at=at, overageStatus='other_form')
    r.svc.account()
    assert readings(r)['second']['reason'] == 'conflicting evidence'
    successor = restarted(r.svc)
    extra_usage.notify(successor)
    extra_usage.notify(successor)
    reports = successor.store.for_seat(desk)
    assert pending in reports and len(reports) == 2
    [conflict] = [report for report in reports if report != pending]
    assert conflict.body.startswith(f'second: Claude extra-usage readings conflict at {at.isoformat()}.')
    assert 'Reading age when queued:' in conflict.body and 'other_form' not in conflict.body


def test_migration_retries_legacy_outbox_and_keeps_late_new_and_unreportable_evidence(tmp_path, monkeypatch):
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(post, 'now', lambda: at + timedelta(hours=2))
    ship = SimpleNamespace(pools={'made': SimpleNamespace(provider='claude', kind='subscription', name='made')},
                           seats={'desk': SimpleNamespace(role='desk', name='desk')})
    service = SimpleNamespace(runtime=tmp_path, ship=ship, store=post.Store(tmp_path))
    path = tmp_path / 'ledger.sqlite'
    key = '["original",null]'
    legacy = service.store.prepare('report', post.SHIP, 'desk', f'made: Claude extra usage is unverified. At '
                                   f'{at.isoformat()} it reported overageStatus="original", overageDisabledReason=unknown.')
    journal = tmp_path / 'claude-extra-usage-alerts.json'
    journal.write_text(json.dumps({'made': dict(at=at.isoformat(), key=key, intent=legacy.to_json(), published=False)}))
    legacy_table(path, [
        ('made', (at - timedelta(minutes=4)).isoformat(), 'unverified', None, '["prior",null]'),
        ('made', (at - timedelta(minutes=2)).isoformat(), 'unavailable', 'setting malformed', None),
        ('made', (at - timedelta(minutes=1)).isoformat(), 'unavailable', 'conflicting evidence', None),
        ('made', at.isoformat(), 'unavailable', 'conflicting evidence', None),
    ])
    with Ledger(path) as book:
        older = at - timedelta(minutes=3)
        observed = extra_usage_at_refusal(rejected_limit_info(refusal(older, overageStatus='new_late')), older)
        extra_usage.remember(book.db, 'made', observed)
        book.db.commit()
    with Ledger(path) as book:  # a second initialization must not enqueue the same retained evidence
        assert book.db.execute('SELECT COUNT(*) FROM claude_extra_usage_changes').fetchone() == (5,)
    monkeypatch.setattr(extra_usage, 'NOTICE_BATCH', 2)
    for count in (2, 3, 4, 4):
        extra_usage.notify(restarted(service))
        assert len(service.store.for_seat('desk')) == count
    reports = service.store.for_seat('desk')
    assert legacy in reports and len(reports) == 4
    [summary] = [report.body for report in reports if 'upgrade summary' in report.body]
    assert '3 retained readings' in summary and '(1 conflicting, 1 malformed, 1 unverified)' in summary
    assert f'Oldest: {(at - timedelta(minutes=4)).isoformat()}' in summary
    assert f'Newest: {(at - timedelta(minutes=1)).isoformat()}' in summary
    assert f'Latest retained reading: conflicting, observed at {at.isoformat()}' in summary
    assert 'cannot be proved either way' in summary and 'overageStatus="prior"' not in summary
    [conflict] = [report.body for report in reports if 'readings conflict at' in report.body]
    assert f'readings conflict at {at.isoformat()}' in conflict
    [arrived] = [report.body for report in reports if 'new_late' in report.body]
    assert '7380 seconds' in arrived


def test_upgrade_summary_is_frozen_and_retried_once_per_pool(tmp_path, monkeypatch):
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    earlier = (at - timedelta(minutes=3)).isoformat()
    monkeypatch.setattr(post, 'now', lambda: at + timedelta(hours=2))
    names = ('alpha', 'bravo', 'quiet')
    ship = SimpleNamespace(pools={name: SimpleNamespace(provider='claude', kind='subscription', name=name)
                                  for name in names}, seats={'desk': SimpleNamespace(role='desk', name='desk')})
    service = SimpleNamespace(runtime=tmp_path, ship=ship, store=post.Store(tmp_path))
    path = tmp_path / 'ledger.sqlite'
    journal = tmp_path / 'claude-extra-usage-alerts.json'
    journal.write_text(json.dumps({name: dict(at=at.isoformat(), key=None, intent=None, published=True)
                                   for name in names}))
    legacy_table(path, [
        ('alpha', earlier, 'unavailable', 'setting malformed', None),
        ('bravo', earlier, 'unverified', None, '["form",null]'),
        ('quiet', earlier, 'off', 'org_level_disabled', None),
        *[(name, at.isoformat(), 'off', 'org_level_disabled', None) for name in names],
    ])
    with Ledger(path):
        pass

    save = extra_usage.durable.replace_file

    def interrupted(path, text):
        if path == journal and json.loads(text)['alpha'].get('upgrade', {}).get('published'):
            raise OSError('made interruption after upgrade publication')
        save(path, text)

    with monkeypatch.context() as cut:
        cut.setattr(extra_usage.durable, 'replace_file', interrupted)
        with pytest.raises(OSError, match='after upgrade publication'):
            extra_usage.notify(service)
    pending = json.loads(journal.read_text())['alpha']['upgrade']
    assert not pending['published']
    fixed = post.Message.from_json(pending['intent'])
    assert service.store.for_seat('desk') == [fixed]
    assert '1 malformed' in fixed.body and 'Latest retained reading: off (org_level_disabled)' in fixed.body

    later = at + timedelta(minutes=1)
    with Ledger(path) as book:
        observed = extra_usage_at_refusal(rejected_limit_info(refusal(later, overageStatus='new_form')), later)
        extra_usage.remember(book.db, 'alpha', observed)
        book.db.commit()
    for _ in range(2):
        extra_usage.notify(restarted(service))
    reports = service.store.for_seat('desk')
    assert fixed in reports and len(reports) == 3
    [bravo] = [report.body for report in reports if report.body.startswith('bravo:')]
    assert 'upgrade summary' in bravo and '1 unverified' in bravo
    [new] = [report.body for report in reports if 'new_form' in report.body]
    assert 'upgrade summary' not in new
    settled = json.loads(journal.read_text())
    assert settled['alpha']['upgrade'] == dict(pending, published=True)
    assert settled['quiet']['upgrade'] == dict(evidence=None, intent=None, published=True)


def test_recovered_ledger_reuses_low_sequences_without_losing_or_repeating_notices(tmp_path, monkeypatch):
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(post, 'now', lambda: at + timedelta(hours=2))
    pool = SimpleNamespace(provider='claude', kind='subscription', name='made')
    ship = SimpleNamespace(pools={'made': pool}, seats={'desk': SimpleNamespace(role='desk', name='desk')})
    service = SimpleNamespace(runtime=tmp_path, ship=ship, store=post.Store(tmp_path))
    path = tmp_path / 'ledger.sqlite'
    journal = tmp_path / 'claude-extra-usage-alerts.json'
    journal.write_text(json.dumps({'made': dict(at=at.isoformat(), key=None, intent=None, published=True)}))
    earlier = at - timedelta(minutes=10)
    last = at + timedelta(seconds=100)
    future = at + timedelta(hours=3)
    legacy_table(path, [
        ('made', earlier.isoformat(), 'unverified', None, '["summarized",null]'),
        ('made', at.isoformat(), 'off', 'org_level_disabled', None),
    ])

    def retain(book, when, value):
        observed = extra_usage_at_refusal(rejected_limit_info(refusal(when, overageStatus=value)), when)
        extra_usage.remember(book.db, 'made', observed)

    with Ledger(path) as book:
        for second in range(1, 91):
            retain(book, at + timedelta(seconds=second), 'settled')
        retain(book, future, 'clock_ahead')
        book.db.commit()
    extra_usage.notify(service)
    extra_usage.notify(service)
    original = service.store.for_seat('desk')
    assert len(original) == 2 and sum('upgrade summary' in report.body for report in original) == 1

    def cut(*args):
        raise OSError('made interruption before publication')

    with Ledger(path) as book:
        retain(book, last, 'pending_before_rebuild')
        book.db.commit()
    with monkeypatch.context() as broken:
        broken.setattr(compaction_alert, 'publish', cut)
        with pytest.raises(OSError, match='before publication'):
            extra_usage.notify(service)
    old = json.loads(journal.read_text())['made']
    assert old['sequence'] > 90 and old['deferred'] and old['upgrade']['published'] and not old['published']
    pending = post.Message.from_json(old['intent'])

    path.write_bytes(b'made malformed SQLite ledger')
    with Ledger(path) as book:
        assert book.catching_up()
        new_time = at - timedelta(minutes=20)  # earlier than both the checkpoint and the settled history
        for when, value in ((at + timedelta(seconds=2), 'settled'), (new_time, 'new_after_recovery'),
                            (last, 'pending_before_rebuild'), (earlier, 'summarized'),
                            (new_time + timedelta(seconds=1), 'new_after_recovery')):
            retain(book, when, value)
        book.db.commit()
        assert book.db.execute('SELECT sequence FROM claude_extra_usage_changes ORDER BY sequence').fetchall() == [
            (1,), (2,), (3,), (4,), (5,)]
    for _ in range(2):
        extra_usage.notify(restarted(service))
    final = json.loads(journal.read_text())['made']
    assert final['instance'] != old['instance'] and final['sequence'] == 5 and final['deferred'] == []
    assert final['upgrade'] == old['upgrade']
    reports = service.store.for_seat('desk')
    assert len(reports) == 4 and all(report in reports for report in (*original, pending))
    [new] = [report for report in reports if 'new_after_recovery' in report.body]
    assert new_time.isoformat() in new.body and '8400 seconds' in new.body
