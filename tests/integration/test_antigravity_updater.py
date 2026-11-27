"""Antigravity's updater setting as seen by the child on each invocation path."""

from dataclasses import replace
import json
from pathlib import Path
import sys

import pytest

from quarterdeck import check, doctor
from quarterdeck.drill import Check
from quarterdeck.harness import antigravity, antigravity_quota
from quarterdeck.seat import Seat
from quarterdeck.service import Service
from quarterdeck.ship import Pool, SeatSpec, Session, Ship


@pytest.fixture
def fake_agy(tmp_path, monkeypatch):
    log = tmp_path / 'agy.jsonl'
    program = tmp_path / 'agy'
    program.write_text(f'''#!{sys.executable}
import json
import os
import signal
import sys

args = sys.argv[1:]
with open({str(log)!r}, 'a') as out:
    print(json.dumps([args, os.environ.get('AGY_CLI_DISABLE_AUTO_UPDATE')]), file=out)
if args == ['--version']:
    print('Antigravity 1.5.18', file=sys.stderr)
elif args == ['-p', '/usage', '--output-format', 'json']:
    print('{{}}')
else:
    signal.pause()
''')
    program.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path), prepend=':')
    monkeypatch.setenv('AGY_CLI_DISABLE_AUTO_UPDATE', 'false')
    monkeypatch.setattr(antigravity, 'STATE_DIR', tmp_path / 'empty-state')
    # Enable real adapter code in place of the suite's no-real-usage stub.
    monkeypatch.setattr(antigravity, 'fetch_usage', antigravity_quota.fetch_usage)
    return lambda: [json.loads(line) for line in log.read_text().splitlines()]


@pytest.fixture
def google_ship(tmp_path):
    spec = SeatSpec('worker', 'crew', 'worker', 'antigravity', ('google',), cwd=tmp_path)
    return Ship(tmp_path, 'synthetic', 'operator', tmp_path, {'google': Pool('google', 'google')},
                {'crew': Session('crew', 'Synthetic')}, {spec.name: spec}, code='checkout')


def test_version_forces_updater_off(fake_agy):
    assert antigravity.AntigravityHarness().version() == '1.5.18'
    assert fake_agy() == [[['--version'], 'true']]


def test_doctor_forces_updater_off(fake_agy, google_ship, synthetic_disk_room):
    row = next(row for row in doctor.checks(google_ship) if row.name == 'program antigravity')
    assert row.passed and row.detail.startswith('agy 1.5.18')
    assert fake_agy() == [[['--version'], 'true']]


def test_check_forces_updater_off(fake_agy, google_ship, tmux, monkeypatch):
    def launch_only(ship, seat, orders):
        assert check._probe_launch(seat, []) is not None
        return [Check('synthetic launch', True)]

    monkeypatch.setattr(check, 'probe', launch_only)
    installed, *_ = check.check(google_ship, 'antigravity', tmux)
    assert installed.passed and installed.detail.startswith('antigravity 1.5.18')
    assert_launch_calls(fake_agy())


def test_background_usage_forces_updater_off(fake_agy, google_ship, tmux):
    service = Service(replace(google_ship, seats={}), tmux)
    try:
        service.usage_pending = True
        service.refresh_usage()
    finally:
        service.ledger.close()
    assert fake_agy() == [[['-p', '/usage', '--output-format', 'json'], 'true']]


def test_seat_launch_forces_updater_off(fake_agy, google_ship, tmux):
    seat = Seat(google_ship, google_ship.seats['worker'], tmux)
    seat.up(resume=False)
    assert_launch_calls(fake_agy())


def assert_launch_calls(calls):
    version, launch = calls
    assert version == [['--version'], 'true']
    argv, updater = launch
    assert updater == 'true'
    permissions, settings, path, opening, brief = argv
    assert (permissions, settings, opening) == ('--dangerously-skip-permissions', '--settings', '--prompt-interactive')
    assert Path(path).name == 'settings.json' and brief
