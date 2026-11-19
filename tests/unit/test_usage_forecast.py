"""Recorded numeric series, with invented pools/times and no native account data."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import post, usage_forecast as forecast
from quarterdeck.harness.limits import Usage, Window
from quarterdeck.ledger import Ledger
from quarterdeck.ledger_reading import collect as ledger_reading, render_pools
from quarterdeck.watch import Facts

AT = datetime(2030, 1, 1, tzinfo=timezone.utc)
RESET = AT + timedelta(days=7)


def record(book, points, *, pool='sample', reset=RESET, window='7d', **changes):
    for hours, used in points:
        at = AT + timedelta(hours=hours)
        book.record_usage(pool, Usage((Window(window, used, reset, **changes),), at))


@pytest.fixture
def book(tmp_path):
    with Ledger(tmp_path / 'ledger.sqlite') as ledger:
        yield ledger


def test_three_hour_rate_and_exhaustion_are_measured_from_endpoints(book):
    record(book, [(0, .1), (.5, .2), (1, .3), (2, .5), (3, .7)])
    result, = forecast.collect(book, AT + timedelta(hours=3))
    assert result.rate_per_hour == pytest.approx(.2)
    assert result.spent_at == AT + timedelta(hours=4.5)
    assert result.first_at == AT and result.samples == 5
    assert result.warning and result.resets_at == RESET
    assert '20.0%/h over 3.0h (5 readings)' in result.describe(detail=True)
    assert 'sample: spent by' in result.describe()


def test_reconstructed_grok_ramp_predicts_the_recorded_spent_endpoint(book):
    # Only the 11% reading and the later spent endpoint were retained. This is
    # an explicitly reconstructed steady ramp, not a recorded intermediate
    # series. The fixture keeps relative times/fractions and a shifted epoch,
    # without accounts, records, seats, native payloads or actual timestamps.
    source = Path(__file__).parents[1] / 'fixtures' / 'usage_forecast' / 'reconstructed_weekly_ramp.json'
    fixture = json.loads(source.read_text())
    assert fixture['kind'] == 'steady reconstruction from two recorded Grok readings'
    first, last = fixture['endpoints']
    elapsed = last['elapsed_seconds']
    used = first['used']
    rate_per_second = (last['used'] - used) / elapsed
    reset = AT + timedelta(seconds=fixture['reset_after_first_seconds'])
    record(book, [(hours, used + rate_per_second * hours * 3600) for hours in (0, .5, 1)], reset=reset)
    result, = forecast.collect(book, AT + timedelta(hours=1))
    assert result.warning
    assert result.rate_per_hour == pytest.approx(.17972737982835)
    assert abs((result.spent_at - (AT + timedelta(seconds=elapsed))).total_seconds()) < .001
    assert 4.9 < (result.spent_at - AT).total_seconds() / 3600 < 5
    assert result.spent_at < reset
    # The first point alone cannot forecast. Once an hour's evidence exists,
    # the ramp gives nearly four hours' warning before the reconstructed wall.
    assert (result.spent_at - result.read_at) > timedelta(hours=3.9)


def test_three_hour_window_ignores_older_slower_spending(book):
    record(book, [(0, .05), (1, .05), (2, .05), (3, .1), (4, .3), (5, .5), (6, .7)])
    result, = forecast.collect(book, AT + timedelta(hours=6))
    assert result.first_at == AT + timedelta(hours=3)
    assert result.spent_at == AT + timedelta(hours=7.5)
    assert result.samples == 4


@pytest.mark.parametrize('points', [
    [], [(0, .1)], [(0, .1), (1, .3)],  # not enough distinct observations
    [(0, .1), (.1, .2), (.2, .3)],  # short jump, not an hour of evidence
    [(0, .1), (1.5, .3), (2, .5)],  # gap too long
    [(0, .1), (.5, .1), (1, .1)],  # no spending
    [(0, .5), (.5, .4), (1, .3)],  # decreasing meter
    [(0, .7), (.5, .8), (1, 1)],  # already spent, not a warning
])
def test_insufficient_or_unmeasurable_series_has_no_forecast(book, points):
    record(book, points)
    at = AT + timedelta(hours=points[-1][0] if points else 0)
    assert forecast.collect(book, at) == []


def test_future_stale_failed_and_elapsed_predictions_are_silent(book):
    record(book, [(0, .1), (.5, .3), (1, .5)])
    assert forecast.collect(book, AT + timedelta(minutes=59)) == []
    assert forecast.collect(book, AT + timedelta(hours=2)) == []
    book.record_usage('sample', Usage((), AT + timedelta(hours=1, minutes=1), 'sample unavailable'))
    assert forecast.collect(book, AT + timedelta(hours=1, minutes=1)) == []


def test_prediction_after_reset_is_available_as_detail_without_warning(book):
    reset = AT + timedelta(hours=2)
    record(book, [(0, .1), (.5, .11), (1, .12)], reset=reset)
    result, = forecast.collect(book, AT + timedelta(hours=1))
    assert result.rate_per_hour == pytest.approx(.02)
    assert not result.warning
    assert result.spent_at > reset


def test_resets_and_group_or_bucket_changes_do_not_join_history(book):
    record(book, [(0, .1), (.5, .3), (1, .5)], group='sample-family', bucket='sample-bucket')
    original = book.usage.rows()[0]
    rows = book.db.execute('SELECT read_at,used,resets_at,group_name,bucket FROM usage_samples ORDER BY read_at').fetchall()
    for changed in (replace(original, resets_at=(RESET + timedelta(days=7)).isoformat()),
                    replace(original, group='other-family'), replace(original, bucket='other-bucket')):
        assert forecast.estimate(changed, rows, AT + timedelta(hours=1)) is None
    record(book, [(2, .02)], reset=RESET + timedelta(days=7), group='sample-family', bucket='sample-bucket')
    assert forecast.collect(book, AT + timedelta(hours=2)) == []
    record(book, [(2.5, .12), (3, .22)], reset=RESET + timedelta(days=7), group='sample-family', bucket='sample-bucket')
    result, = forecast.collect(book, AT + timedelta(hours=3))
    assert result.samples == 3 and result.first_at == AT + timedelta(hours=2)


def test_same_observation_does_not_become_three_samples_and_conflicts_are_unknown(book):
    record(book, [(0, .1)] * 4)
    assert book.db.execute('SELECT count(*) FROM usage_samples').fetchone() == (1,)
    assert forecast.collect(book, AT) == []
    record(book, [(.5, .3), (1, .5)])
    assert len(forecast.collect(book, AT + timedelta(hours=1))) == 1
    record(book, [(.5, .4)])  # older source is rejected, cannot overwrite a historical point
    assert book.db.execute('SELECT count(*) FROM usage_samples').fetchone() == (3,)
    record(book, [(1, .6)])  # same timestamp, contradictory fraction
    assert forecast.collect(book, AT + timedelta(hours=1)) == []
    assert book.db.execute('SELECT used FROM usage_samples ORDER BY read_at DESC LIMIT 1').fetchone() == (None,)


def test_missing_optional_bucket_uses_the_caches_established_identity(book):
    record(book, [(0, .1), (.5, .3)], bucket='sample-bucket')
    record(book, [(1, .5)])
    result, = forecast.collect(book, AT + timedelta(hours=1))
    assert result.samples == 3 and result.rate_per_hour == pytest.approx(.4)


def test_only_accepted_weekly_blocking_readings_enter_bounded_history(book):
    record(book, [(0, .1), (.5, .3), (1, .5)])
    for window, changes in [('5h', {}), ('7d', {'blocks': False}),
                            ('family / 7d', {'uncertainty': 'scope unknown'})]:
        record(book, [(2, .1)], pool='other', window=window, **changes)
    assert book.db.execute('SELECT DISTINCT pool FROM usage_samples').fetchall() == [('sample',)]
    record(book, [(25, .6)])
    assert book.db.execute('SELECT count(*) FROM usage_samples').fetchone() == (2,)
    assert forecast.collect(book, AT + timedelta(hours=25)) == []


def test_earliest_scoped_weekly_exhaustion_is_selected_per_declared_pool(book):
    record(book, [(0, .1), (.5, .2), (1, .3)])
    record(book, [(0, .1), (.5, .4), (1, .7)], window='family / 7d', group='family')
    record(book, [(0, .1), (.5, .4), (1, .7)], pool='other')
    result, = forecast.collect(book, AT + timedelta(hours=1), {'sample'})
    assert result.window == 'family / 7d' and result.spent_at == AT + timedelta(hours=1.5)


def test_older_book_is_read_without_a_schema_migration(book):
    path = book.path
    book.db.execute('DROP TABLE usage_samples')
    book.db.commit()
    with Ledger.reading(path) as reader:
        assert forecast.collect(reader, AT) == []
        assert not reader.db.execute("SELECT 1 FROM sqlite_master WHERE name='usage_samples'").fetchone()


def test_history_and_latest_tuple_share_the_existing_usage_transaction(book, monkeypatch):
    original = forecast.remember
    def fail_after_sample(*args):
        original(*args)
        raise RuntimeError('synthetic interruption')
    monkeypatch.setattr(forecast, 'remember', fail_after_sample)
    with pytest.raises(RuntimeError, match='synthetic interruption'):
        record(book, [(0, .1)])
    assert book.usage.rows() == []
    assert book.db.execute('SELECT count(*) FROM usage_samples').fetchone() == (0,)
    assert book.db.execute('SELECT count(*) FROM usage_reads').fetchone() == (0,)
    assert not book.db.in_transaction


def make_service(book):
    runtime = book.path.parent
    ship = SimpleNamespace(runtime=runtime,
                           pools={'sample': SimpleNamespace(name='sample', kind='subscription')},
                           seats={'system': SimpleNamespace(name='system', role='systems')})
    return SimpleNamespace(ship=ship, store=post.Store(runtime))


def test_alarm_once_per_pool_and_week_survives_restart_and_clear(book, monkeypatch):
    service = make_service(book)
    clock = AT + timedelta(hours=1)
    monkeypatch.setattr(post, 'now', lambda: clock)
    record(book, [(0, .1), (.5, .3), (1, .5)])
    before = [(row.pool, row.used) for row in book.usage.rows()]
    forecast.notify(service)
    first, = service.store.all()
    assert first.to == 'system' and first.kind == 'report' and first.sender == post.SHIP
    assert 'warning only' in first.body and '40.0%/h' in first.body
    assert before == [(row.pool, row.used) for row in book.usage.rows()]
    assert book.walls(now=clock) == []
    for _ in range(3):
        forecast.notify(make_service(book))
    record(book, [(1.5, .5), (2, .5)])
    clock = AT + timedelta(hours=2)
    forecast.notify(service)
    record(book, [(2.5, .7), (3, .9)])
    clock = AT + timedelta(hours=3)
    forecast.notify(make_service(book))
    assert [message.id for message in service.store.all()] == [first.id]
    # A new reset week gets its own alarm; repeated visits to the old week cannot rearm it.
    clock = AT + timedelta(days=7, hours=1)
    record(book, [(168, .1), (168.5, .3), (169, .5)], reset=RESET + timedelta(days=7))
    forecast.notify(make_service(book))
    assert len(service.store.all()) == 2


def test_notification_recovers_the_saved_identity_after_publication_interruption(book, monkeypatch):
    from quarterdeck import compaction_alert
    service = make_service(book)
    monkeypatch.setattr(post, 'now', lambda: AT + timedelta(hours=1))
    record(book, [(0, .1), (.5, .3), (1, .5)])
    publish = compaction_alert.publish
    def unavailable(*args):
        raise OSError('synthetic publication unavailable')
    monkeypatch.setattr(compaction_alert, 'publish', unavailable)
    with pytest.raises(OSError):
        forecast.notify(service)
    data = json.loads((service.ship.runtime / 'pool-forecast-alerts.json').read_text())
    intent = data['periods'][0]['reports'][0]
    identity = post.Message.from_json(intent).id
    monkeypatch.setattr(compaction_alert, 'publish', publish)
    forecast.notify(make_service(book))
    message, = service.store.all()
    assert message.id == identity and message.to_json() == intent


def test_no_system_recipient_keeps_the_warning_without_claiming_an_alarm(book, monkeypatch):
    service = make_service(book)
    service.ship.seats.clear()
    monkeypatch.setattr(post, 'now', lambda: AT + timedelta(hours=1))
    record(book, [(0, .1), (.5, .3), (1, .5)])
    forecast.notify(service)
    assert service.store.all() == []
    assert not (service.ship.runtime / 'pool-forecast-alerts.json').exists()
    assert forecast.readings(service.ship)[0].warning
    service.ship.seats['system'] = SimpleNamespace(name='system', role='systems')
    forecast.notify(service)
    assert len(service.store.all()) == 1


def test_warning_is_preserved_in_normal_and_overflow_status_lines(book):
    record(book, [(0, .1), (.5, .3), (1, .5)])
    result, = forecast.collect(book, AT + timedelta(hours=1))
    text, detail = result.headline(), result.describe()
    facts = Facts('ok', 0, 0, 0, pool_forecasts=[detail], pool_forecast_headlines=[text])
    for line in (facts.line(), replace(facts, needs_human=['synthetic-seat-' + str(n) for n in range(25)]).line()):
        assert text in line and 'reset' not in line and '%/h' not in line


def test_ledger_detail_exposes_rate_and_observation_span(book, monkeypatch):
    import quarterdeck.ledger_reading as module
    record(book, [(0, .1), (.5, .3), (1, .5)])
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return AT + timedelta(hours=1)
    monkeypatch.setattr(module, 'datetime', Clock)
    data = ledger_reading(book)
    result, = data['usage_forecasts']
    assert result.rate_per_hour == pytest.approx(.4)
    assert any('40.0%/h' in line for line in render_pools(data))


# The provider's reset time jitters below the second from read to read: the recorded shape is a reset at the same
# minute whose fraction of a second differs on every reading. Synthetic values, that shape only.
JITTER = (.258, .143, .331, .297, .973, .477)


def record_jittered(book, points, *, base=RESET, pool='sample'):
    for (hours, used), fraction in zip(points, JITTER * 4):
        at = AT + timedelta(hours=hours)
        book.record_usage(pool, Usage((Window('7d', used, base + timedelta(seconds=fraction)),), at))


def test_a_reset_that_jitters_below_the_second_is_one_week_and_gives_a_forecast(book):
    record_jittered(book, [(0, .1), (.5, .2), (1, .3), (2, .5), (3, .7)])
    result, = forecast.collect(book, AT + timedelta(hours=3))
    assert result.samples == 5 and result.first_at == AT
    assert result.rate_per_hour == pytest.approx(.2)
    assert result.warning and forecast.same_reset(result.resets_at, RESET)


def test_a_reset_that_truly_moves_starts_a_new_run(book):
    record(book, [(0, .1), (.5, .2), (1, .3)])
    record(book, [(1.5, .4), (2, .5)], reset=RESET + forecast.RESET_TOLERANCE + timedelta(seconds=1))
    assert forecast.collect(book, AT + timedelta(hours=2)) == []  # two points of the moved reset, not five
    assert forecast.same_reset(RESET, RESET + forecast.RESET_TOLERANCE)
    assert not forecast.same_reset(RESET, RESET + forecast.RESET_TOLERANCE + timedelta(microseconds=1))
    assert not forecast.same_reset(None, RESET) and not forecast.same_reset(RESET, None)


def test_one_alarm_per_week_though_every_reading_names_its_reset_a_little_differently(book, monkeypatch):
    service = make_service(book)
    clock = AT + timedelta(hours=1)
    monkeypatch.setattr(post, 'now', lambda: clock)
    record_jittered(book, [(0, .1), (.5, .3), (1, .5)])
    forecast.notify(service)
    for step, hours in enumerate((1.25, 1.5, 1.75, 2)):
        book.record_usage('sample', Usage((Window('7d', .5 + .1 * (step + 1), RESET + timedelta(seconds=JITTER[step + 1])),),
                                          AT + timedelta(hours=hours)))
        clock = AT + timedelta(hours=hours)
        forecast.notify(make_service(book))
    first, = service.store.all()
    journal = json.loads((book.path.parent / 'pool-forecast-alerts.json').read_text())
    assert len(journal['periods']) == 1
    # The next week, its reset jittering too, has its own one alarm.
    clock = AT + timedelta(days=7, hours=1)
    record_jittered(book, [(168, .1), (168.5, .3), (169, .5)], base=RESET + timedelta(days=7))
    forecast.notify(make_service(book))
    clock = AT + timedelta(days=7, hours=1.5)
    book.record_usage('sample', Usage((Window('7d', .7, RESET + timedelta(days=7, seconds=.9)),), AT + timedelta(days=7, hours=1.5)))
    forecast.notify(make_service(book))
    assert len(service.store.all()) == 2


def record_two(book, early_reset, later_reset):
    """One pool read with two weekly windows each time, as a provider reports them together. The family window
    spends faster and so runs out soonest (at 2.25h); the plain one runs out at 4.5h."""
    for hours, fast, slow in ((0, .1, .1), (.5, .3, .2), (1, .5, .3)):
        book.record_usage('sample', Usage((Window('family / 7d', fast, early_reset, group='family'),
                                           Window('7d', slow, later_reset)), AT + timedelta(hours=hours)))


def test_a_window_that_warns_is_shown_over_an_earlier_one_that_does_not(book):
    # The family window runs out soonest, but only after its own reset at 2h: no warning. The plain one runs out
    # later, at 4.5h, but before its reset: the real warning, which must not be hidden.
    record_two(book, AT + timedelta(hours=2), RESET)
    forecasts = [forecast.estimate(reading, book.db.execute(
        'SELECT read_at,used,resets_at,group_name,bucket FROM usage_samples WHERE window=? ORDER BY read_at',
        (reading.window,)).fetchall(), AT + timedelta(hours=1)) for reading in book.usage.rows()]
    assert sorted((f.window, f.warning) for f in forecasts) == [('7d', True), ('family / 7d', False)]
    result, = forecast.collect(book, AT + timedelta(hours=1))
    assert result.window == '7d' and result.warning and result.spent_at == AT + timedelta(hours=4.5)


def test_with_no_window_warning_the_earliest_exhaustion_is_still_the_detail(book):
    record_two(book, AT + timedelta(hours=2), AT + timedelta(hours=3))
    result, = forecast.collect(book, AT + timedelta(hours=1))
    assert not result.warning and result.window == 'family / 7d' and result.spent_at == AT + timedelta(hours=2.25)
