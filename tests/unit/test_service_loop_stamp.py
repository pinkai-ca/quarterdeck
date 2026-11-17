"""A service whose main loop is stuck reads as busy on every status line, never ok: the beat thread proves the
process alive, and the main loop's own pass stamp proves it is making passes. Injected clocks; a throwaway runtime."""
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from quarterdeck import accounting_health, accounting_slices, servicectl, watch
from quarterdeck.service import Service

NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    monkeypatch.setattr(watch.service, 'alive', lambda r: True)  # a service holds the ship's lock
    return runtime


def beat(runtime, at):
    path = servicectl.heartbeat_path(runtime)
    path.parent.mkdir(exist_ok=True)
    path.write_text(f'4242 {at.isoformat()}\n')


def stamp(runtime, duty, at):
    servicectl.write_pass(runtime, duty, now=at, pid=4242)


def test_a_loop_held_in_one_duty_past_the_bound_reads_busy_and_alerts_once(runtime):
    beat(runtime, NOW)  # the beat thread keeps beating: the process is alive
    stamp(runtime, 'deliveries', NOW - timedelta(seconds=servicectl.LOOP_STUCK_SECONDS + 5))
    state = watch.service_state(runtime, now=NOW)
    assert state.startswith(f'busy {int(servicectl.LOOP_STUCK_SECONDS) + 5}s in deliveries'), state
    assert state.count('main loop stuck') == 1, state
    facts = watch.Facts(state, 0, 0, 0)
    assert facts.line().count('main loop stuck') == 1


def test_a_slow_duty_under_the_bound_reads_busy_without_the_alert(runtime):
    beat(runtime, NOW)
    stamp(runtime, 'deliveries', NOW - timedelta(seconds=servicectl.BUSY_SECONDS + 3))
    state = watch.service_state(runtime, now=NOW)
    assert state == f'busy {int(servicectl.BUSY_SECONDS) + 3}s in deliveries', state


def test_a_pass_that_completes_clears_it(runtime):
    beat(runtime, NOW)
    stamp(runtime, 'deliveries', NOW - timedelta(seconds=servicectl.LOOP_STUCK_SECONDS + 5))
    assert watch.service_state(runtime, now=NOW) != 'ok'
    stamp(runtime, 'pass', NOW)  # the next pass begins
    assert watch.service_state(runtime, now=NOW) == 'ok'


def test_the_beat_thread_alone_never_makes_a_stuck_loop_read_ok(runtime):
    beat(runtime, NOW)
    assert watch.service_state(runtime, now=NOW) != 'ok', 'no loop stamp at all'
    stamp(runtime, 'pass', NOW - timedelta(hours=2))
    assert watch.service_state(runtime, now=NOW).startswith('busy 7200s in pass')


def test_a_gone_process_still_reads_down(runtime, monkeypatch):
    beat(runtime, NOW)
    stamp(runtime, 'pass', NOW)
    monkeypatch.setattr(watch.service, 'alive', lambda r: False)
    assert watch.service_state(runtime, now=NOW) == 'down'


def test_the_main_loop_stamps_each_pass_and_each_duty_and_the_slices_beat_does_not(tmp_path, monkeypatch):
    from .test_accounting_slices import world
    svc, _, _, _ = world(tmp_path, monkeypatch)
    seen = {}
    def step(name):
        def duty():
            seen[name] = servicectl.read_pass(svc.runtime)[2]
            if name == 'deliveries':
                before = servicectl.read_pass(svc.runtime)
                svc.heartbeat()  # what a long duty's slices call: a beat, never a new pass stamp
                assert servicectl.read_pass(svc.runtime)[1] >= before[1] - 1e-6
        duty.__name__ = name
        return duty
    names = [s.__name__ for s in (svc.server_recovery, svc.deliveries, svc.account, svc.retire_post)]
    for name in ('server_recovery', 'relaunch_intents', 'reconcile', 'file_drops', 'groups', 'cascades', 'fleet_post',
                 'inboxes', 'views', 'recoveries', 'refusals', 'answers', 'looks', 'jailed_watches',
                 'write_bound_floor', 'deliveries', 'follow', 'requests', 'compactions', 'output_limits',
                 'person_quiet', 'fresh_after_order', 'rebrief', 'push_reminders', 'credentials', 'authentications',
                 'model_differences', 'failover', 'retire_hand_transcripts', 'account', 'refresh_usage',
                 'pool_forecasts', 'retire_post'):
        monkeypatch.setattr(svc, name, step(name))
    monkeypatch.setattr(svc, '_drain_between_duties', lambda: None)
    monkeypatch.setattr(svc, 'status_due', lambda: None)
    svc._tick_steps()
    assert set(names) <= set(seen) and all(seen[name] == name for name in seen), seen


def test_failing_accounting_that_recovers_logs_recovered_once_on_the_production_path(tmp_path, monkeypatch):
    from .test_accounting_slices import finish, world
    svc, _, _, _ = world(tmp_path, monkeypatch)
    logged = []
    svc.log = lambda event, **fields: logged.append((event, fields))
    health = svc.accounting_health
    health.failures, health.since, health.current = accounting_health.BOUND, '2030-01-01T00:00:00+00:00', 'unavailable'
    health.causes = {'unavailable': accounting_health.BOUND}
    driver = accounting_slices.Driver()
    try:
        finish(driver, svc)
    finally:
        driver.close()
        svc.ledger.close()
    recovered = [fields for event, fields in logged if event == 'accounting-recovered']
    assert len(recovered) == 1 and recovered[0]['failures'] == accounting_health.BOUND, logged
    assert recovered[0]['since'] == '2030-01-01T00:00:00+00:00'


def test_routine_status_work_reads_ok_and_names_status_only_past_the_busy_line(runtime):
    beat(runtime, NOW)
    stamp(runtime, 'status', NOW - timedelta(seconds=16))  # the status work's usual long run: 14 to 16 s
    assert watch.service_state(runtime, now=NOW) == 'ok'
    stamp(runtime, 'status', NOW - timedelta(seconds=servicectl.BUSY_SECONDS + 1))
    assert watch.service_state(runtime, now=NOW) == f'busy {int(servicectl.BUSY_SECONDS) + 1}s in status'


def test_the_work_between_duties_is_named_as_itself_and_the_next_duty_by_its_own_name(tmp_path, monkeypatch):
    from .test_accounting_slices import world
    svc, _, _, _ = world(tmp_path, monkeypatch)
    order = []
    def duty(name):
        def run():
            order.append((name, servicectl.read_pass(svc.runtime)[2]))
        run.__name__ = name
        return run
    for name in ('server_recovery', 'relaunch_intents', 'reconcile', 'file_drops', 'groups', 'cascades', 'fleet_post',
                 'inboxes', 'views', 'recoveries', 'refusals', 'answers', 'looks', 'jailed_watches',
                 'write_bound_floor', 'deliveries', 'follow', 'requests', 'compactions', 'output_limits',
                 'person_quiet', 'fresh_after_order', 'rebrief', 'push_reminders', 'credentials', 'authentications',
                 'model_differences', 'failover', 'retire_hand_transcripts', 'account', 'refresh_usage',
                 'pool_forecasts', 'retire_post'):
        monkeypatch.setattr(svc, name, duty(name))
    monkeypatch.setattr(svc, '_drain_between_duties', lambda: order.append(('drain', servicectl.read_pass(svc.runtime)[2])))
    monkeypatch.setattr(svc, 'status_due', lambda: order.append(('status', servicectl.read_pass(svc.runtime)[2])))
    svc._tick_steps()
    assert all(seen == name for name, seen in order), [row for row in order if row[0] != row[1]]
    assert order[:4] == [('drain', 'drain'), ('server_recovery', 'server_recovery'), ('drain', 'drain'),
                         ('status', 'status')]
