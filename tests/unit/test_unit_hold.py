"""Read-only systemd state decisions from scripted replies."""

from types import SimpleNamespace
import subprocess

import pytest

from quarterdeck import unit_hold


def reply(stdout, code=0):
    return SimpleNamespace(stdout=stdout, returncode=code)


def systemd(monkeypatch, *, service=None, timer=None, deadlines=None, code=0):
    calls = []

    def invoke(*command):
        calls.append(command)
        assert command[1] == '--user'
        if command[0] == 'systemctl':
            assert command[2] == 'show'
            if command[-1] == 'example.service':
                return reply(service, code)
            assert command[-1] == 'example.timer'
            return reply(timer, code)
        assert command[0] == 'busctl'
        if command[2] == 'call':
            assert command[-3:] == ('GetUnit', 's', 'example.timer')
            return reply('o "/org/freedesktop/systemd1/unit/example_2etimer"\n')
        assert command[2] == 'get-property'
        assert command[-2:] == ('NextElapseUSecRealtime', 'NextElapseUSecMonotonic')
        return reply(deadlines)

    monkeypatch.setattr(unit_hold, '_run', invoke)
    monkeypatch.setattr(unit_hold.time, 'time_ns', lambda: 2_000_000_000_000)
    monkeypatch.setattr(unit_hold.time, 'monotonic_ns', lambda: 1_000_000_000_000)
    monkeypatch.setattr(unit_hold.time, 'clock_gettime_ns', lambda *_: 1_100_000_000_000)
    return calls


def test_no_declaration_makes_no_inquiry(monkeypatch):
    calls = systemd(monkeypatch)
    assert unit_hold.reason(None) is None
    assert calls == []


@pytest.mark.parametrize('code', [0, 1])
def test_absent_unit_is_no_hold(monkeypatch, code):
    absent = 'LoadState=not-found\nActiveState=inactive\n'
    calls = systemd(monkeypatch, service=absent, timer=absent, code=code)
    assert unit_hold.reason('example.service') is None
    assert unit_hold.reason('example.timer') is None
    assert len(calls) == 2


def test_running_service_and_disabled_timer(monkeypatch):
    active = 'LoadState=loaded\nActiveState=active\n'
    calls = systemd(monkeypatch, service=active + 'TriggeredBy=example.timer\n')
    assert unit_hold.reason('example.service') == unit_hold.RUNNING
    assert len(calls) == 1
    inactive = 'LoadState=loaded\nActiveState=inactive\n'
    timer = inactive + 'Unit=example.service\nSubState=dead\nWakeSystem=no\n'
    calls = systemd(monkeypatch, service=inactive + 'TriggeredBy=example.timer\n', timer=timer)
    assert unit_hold.reason('example.service') is None
    assert len(calls) == 2
    systemd(monkeypatch, service=active, timer=timer)
    assert unit_hold.reason('example.timer') == unit_hold.RUNNING


@pytest.mark.parametrize('property_values,wakeup,expected', [
    ('t 2300000000\nt 0\n', 'no', unit_hold.SOON),
    ('t 2300000001\nt 0\n', 'no', None),
    ('t 0\nt 1300000000\n', 'no', unit_hold.SOON),
    ('t 0\nt 1300000001\n', 'no', None),
    ('t 0\nt 1320000000\n', 'yes', unit_hold.SOON),
    ('t 0\nt 0\n', 'no', unit_hold.UNKNOWN),
    ('t 0\nt 100000000000000000000\n', 'no', unit_hold.UNKNOWN),
    ('t 0\ns tomorrow\n', 'no', unit_hold.UNKNOWN),
])
def test_waiting_timer_uses_its_earliest_real_or_monotonic_deadline(monkeypatch, property_values, wakeup, expected):
    active = f'LoadState=loaded\nActiveState=active\nUnit=example.service\nSubState=waiting\nWakeSystem={wakeup}\n'
    systemd(monkeypatch, service='LoadState=loaded\nActiveState=inactive\n', timer=active, deadlines=property_values)
    assert unit_hold.reason('example.timer') == expected


def test_unreadable_configured_unit_is_a_hold(monkeypatch):
    systemd(monkeypatch, service='', code=1)
    assert unit_hold.reason('example.service') == unit_hold.UNKNOWN

    def timeout(*_):
        raise subprocess.TimeoutExpired('synthetic', 2)

    monkeypatch.setattr(unit_hold, '_run', timeout)
    assert unit_hold.reason('example.service') == unit_hold.UNKNOWN
