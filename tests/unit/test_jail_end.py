"""A seat's sandbox ends whole: the hold, its lifting, the sweep's own bounds, its alarm, and a screen that meets
either. Synthetic names; the processes are this test's own children, never a seat's."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import alarms, jail_end, pr_screen, trusted_files, write_bound
from quarterdeck.service import Service


def sleeper():
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])
    start = write_bound._processes()[process.pid][1]
    return process, {process.pid: start}


def test_the_hold_never_waits_on_a_zombie(tmp_path):
    process, pids = sleeper()
    try:
        os.kill(process.pid, signal.SIGKILL)
        assert write_bound.tree(process.pid, pids[process.pid]) or True  # read while it still may run
        for _ in range(200):
            if jail_end._stat(process.pid)[2] == 'Z':
                break
            os.sched_yield()
        assert jail_end._stat(process.pid)[2] == 'Z', 'left unreaped: a zombie'
        assert write_bound.alive(pids) == {}, 'a zombie has ended'
        jail_end.hold(tmp_path, 'seat-fern', pids)
        assert jail_end.retry(tmp_path) is True and jail_end.held(tmp_path) is None
    finally:
        process.wait()


def test_the_hold_lifts_only_when_none_of_it_runs_and_never_by_age(tmp_path, monkeypatch):
    process, pids = sleeper()
    try:
        jail_end.hold(tmp_path, 'seat-fern', pids)
        data = json.loads((tmp_path / trusted_files.JAIL_HELD).read_text())
        data['at'] = '2000-01-01T00:00:00+00:00'  # old: age lifts nothing
        (tmp_path / trusted_files.JAIL_HELD).write_text(json.dumps(data))
        monkeypatch.setattr(write_bound, 'kill', lambda pids, rounds=10: [])  # the kill fails to end it
        assert jail_end.retry(tmp_path) is False and jail_end.held(tmp_path)['pids'] == pids
        assert '(1 process still running)' in jail_end.words('seat-fern', jail_end.held(tmp_path))
        monkeypatch.undo()
        assert jail_end.retry(tmp_path) is True and jail_end.held(tmp_path) is None
        assert write_bound.alive(pids) == {}
    finally:
        process.kill()
        process.wait()


def test_an_unreadable_hold_still_holds(tmp_path):
    (tmp_path / trusted_files.JAIL_HELD).write_text('{not json')
    assert jail_end.held(tmp_path) == dict(pids=None)
    assert jail_end.retry(tmp_path) is False and (tmp_path / trusted_files.JAIL_HELD).exists()
    assert 'unreadable; held' in jail_end.words('seat-fern', jail_end.held(tmp_path))


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def test_the_sweep_runs_at_most_once_a_minute_after_a_look_and_its_error_never_stops_the_alarm(monkeypatch):
    runs, logs = [], []
    service = SimpleNamespace(ship=None, log=lambda *args, **data: logs.append((args, data)))
    alarm = write_bound.Alarm(service)
    clock = Clock()

    def sweep(service):
        runs.append(clock())
        raise OSError('synthetic')

    monkeypatch.setattr(jail_end, 'sweep', sweep)
    alarm.sweep(clock)
    clock.now += jail_end.SWEEP_SECONDS - 1
    alarm.sweep(clock)
    clock.now += 1
    alarm.sweep(clock)
    assert runs == [1000.0, 1000.0 + jail_end.SWEEP_SECONDS]
    assert [data['step'] for args, data in logs] == ['jail-sweep', 'jail-sweep']


def test_a_sweep_past_its_budget_stops_and_says_so(tmp_path, monkeypatch):
    logs = []
    clock = Clock()
    service = SimpleNamespace(ship=SimpleNamespace(runtime=tmp_path),
                              tmux=SimpleNamespace(panes=lambda: [], server_pid=lambda: os.getppid()),
                              log=lambda event, **data: logs.append((event, data)))
    real = jail_end._read

    def slow(path):
        clock.now += 1.0  # each read takes a second on the injected clock
        return real(path)

    monkeypatch.setattr(jail_end, '_read', slow)
    assert jail_end.sweep(service, budget=2.0, clock=clock) == []
    assert logs[-1][0] == 'jail-sweep' and logs[-1][1]['outcome'] == 'cut'


def test_another_users_bwrap_is_never_a_candidate(tmp_path, monkeypatch):
    other = os.getuid() + 1
    monkeypatch.setattr(jail_end.os, 'getuid', lambda: other)
    found, whole = jail_end.orphans(tmp_path, set())
    assert found == [] and whole


def test_unreadable_panes_sweep_nothing(tmp_path):
    logs = []

    def panes():
        raise OSError('synthetic server gone')

    service = SimpleNamespace(ship=SimpleNamespace(runtime=tmp_path), tmux=SimpleNamespace(panes=panes),
                              log=lambda event, **data: logs.append((event, data)))
    assert jail_end.sweep(service) == []
    assert logs == [('jail-sweep', dict(outcome='unknown',
                                        reason="the ship's server or its panes could not be read; nothing swept"))]


def swept(runtime, seat, at, pid=4242, program=True):
    record = dict(seat=seat, pid=pid, start='123', ran_seconds=95, program_running=program, killed=[pid],
                  confirmed=True, left=[], at=at.isoformat())
    jail_end._record(runtime, seat, record)
    return record


def test_the_alarm_names_what_was_killed_and_a_second_within_a_day_goes_to_security(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    swept(tmp_path, 'hand-fern', now - timedelta(hours=1))
    key, words, second = jail_end.alarm(tmp_path, 'hand-fern')
    assert not second and 'pid 4242, start 123, ran for 95s, its program running' in words
    assert list((tmp_path / 'evidence' / jail_end.EVIDENCE).iterdir()), 'into the evidence'
    told = []
    monkeypatch.setattr(alarms, 'notify', lambda service, name, human, key, body, **options: told.append(
        (name, key, body, options['initial'], options['channel'])))
    ship = SimpleNamespace(seats={'hand-fern': SimpleNamespace(name='hand-fern', role='hand'),
                                  'systems-seat': SimpleNamespace(name='systems-seat', role='systems'),
                                  'security-seat': SimpleNamespace(name='security-seat', role='security')},
                           session_of=lambda spec: SimpleNamespace(human=False))
    service = object.__new__(Service)
    service.runtime, service.ship, service.log = tmp_path, ship, lambda *a, **k: None
    service.systems_recipients = lambda spec: ['systems-seat']
    service.jail_sweep_alarm(ship.seats['hand-fern'])
    swept(tmp_path, 'hand-fern', now - timedelta(minutes=5), pid=4343)
    service.jail_sweep_alarm(ship.seats['hand-fern'])
    assert [entry[3] for entry in told] == [['systems-seat'], ['systems-seat', 'security-seat']]
    assert told[0][4] == 'jail-sweep' and told[0][1] != told[1][1], 'a new sweep is a new alarm'
    assert '2 in the last day' in told[1][2]
    lines = jail_end.status(SimpleNamespace(runtime=tmp_path))
    assert len(lines) == 1 and 'pid 4343' in lines[0] and '2 in the last day' in lines[0]


def test_a_sweep_older_than_a_day_leaves_the_line_and_the_alarm(tmp_path):
    swept(tmp_path, 'hand-fern', datetime.now(timezone.utc) - timedelta(days=1, minutes=1))
    assert jail_end.alarm(tmp_path, 'hand-fern') == (None, None, False)
    assert jail_end.status(SimpleNamespace(runtime=tmp_path)) == []


# -- a screen that meets a hold or a sweep ---------------------------------------------------------------------------

from tests.unit.test_pr_screen import CORPUS, FIXTURES, Hands, honest  # noqa: E402


def prepared_piece():
    case = pr_screen.FixtureSource(FIXTURES, ['clean-docs'])
    pr = next(iter(case.open()))
    return pr_screen.prepare(case.texts(pr))


def test_a_held_hand_screens_nothing_and_its_piece_counts_as_flagged(tmp_path):
    runtime = tmp_path / 'runtime'
    trusted = trusted_files.make(runtime, 'hand-a')
    process, pids = sleeper()
    try:
        jail_end.hold(trusted, 'hand-a', pids)
        hands = Hands(runtime, tmp_path / 'hands', honest)
        prepared = prepared_piece()
        verdict = pr_screen.screen_piece(runtime, hands, 'hand-a', 'key', 0, prepared, list(prepared.files),
                                         corpus=CORPUS)
        assert not verdict.clean and 'not proved ended' in verdict.reason
        assert hands.calls == [] and hands.orders == {}, 'no fresh start, no instruction: nothing reached the hand'
    finally:
        process.kill()
        process.wait()


def test_a_hand_swept_while_it_screened_is_flagged_and_not_started_again(tmp_path):
    runtime = tmp_path / 'runtime'
    trusted_files.make(runtime, 'hand-a')

    def swept_meanwhile(hands, name, message_id, folder):
        honest(hands, name, message_id, folder)
        swept(runtime, name, datetime.now(timezone.utc))

    hands = Hands(runtime, tmp_path / 'hands', swept_meanwhile)
    prepared = prepared_piece()
    verdict = pr_screen.screen_piece(runtime, hands, 'hand-a', 'key', 0, prepared, list(prepared.files), corpus=CORPUS,
                                     answer_seconds=0)
    assert not verdict.clean and 'not proved ended after the piece' in verdict.reason
    assert ('fresh after', 'hand-a') not in hands.calls, 'the hand stays down'
    record = json.loads(next((runtime / pr_screen.ROOT / 'screens' / 'key').glob('*.verdict.json')).read_text())
    assert record['clean'] is False and 'a stray sandbox was ended' in record['sandbox']
