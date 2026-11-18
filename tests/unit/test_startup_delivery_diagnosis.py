"""Ordinary read-only status and doctor checks of synthetic startup evidence."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from quarterdeck import cli, post, servicectl, shutdown
from quarterdeck.ship import load


pytestmark = pytest.mark.usefixtures('synthetic_disk_room')
PREFIX = 'startup held by service.delivery: '
STAMP = '2026-01-01T00:00:00+00:00'
PRIVATE_WORDS = 'synthetic journal words outside the diagnosis'


@pytest.fixture
def ship(drydock, tmux, monkeypatch):
    monkeypatch.setenv('QD_TMUX_SOCKET', tmux.socket)
    return load(drydock)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + '\n')


def old_owner(runtime, *, save=True):
    # An actual short-lived process supplies its stamp. It has ended before the lock stamp is saved.
    with subprocess.Popen([sys.executable, '-I', '-c', 'import sys; sys.stdin.read()'],
                          stdin=subprocess.PIPE) as prior:
        start = shutdown.process_start(prior.pid)
        prior.communicate(timeout=5)
    assert start is not None and prior.returncode == 0
    with servicectl.acquire_lock(runtime) as lock:
        info = os.fstat(lock.fileno())
        expected = dict(version=1, pid=prior.pid, start=start, token='synthetic service',
                        device=info.st_dev, inode=info.st_ino)
        if save:
            lock.write(json.dumps(expected) + '\n')
    assert shutdown.process_gone(expected)
    return expected


def evidence(runtime, saved_owner, case):
    seat = 'worker-1'
    stored = post.Store(runtime).new('order', 'operator', seat, 'Synthetic pending check')
    attempts = post.Attempts(runtime)
    attempted = attempts.open(stored, seat, '%10', saved_owner['pid'], STAMP)
    attempts.close(attempted, 'blind', 'Synthetic unfinished input')
    journal = runtime / 'post' / 'pending' / seat / 'synthetic-journal.json'
    token = 'synthetic boundary'
    kind = 'pending'
    if case == 'early-attempt':
        journal, kind = attempted.path, 'attempt'
    else:
        data = dict(seat=seat, phase='submitting', payload=PRIVATE_WORDS, message=stored.id,
                    identity=dict(pane='%10', pid=saved_owner['pid'], launch=STAMP, harness='fake'),
                    attempts=[attempted.path.name], record=None, shown=None, at=STAMP, since=STAMP,
                    service_owner=saved_owner, delivery_token='another synthetic identity' if case == 'mismatch' else token)
        if case == 'unreadable':
            journal.parent.mkdir(parents=True)
            journal.write_text('{ ' + PRIVATE_WORDS)
        elif case == 'displaced':
            journal.parent.mkdir(parents=True)
            write_json(runtime / 'evidence' / 'synthetic-displaced.json', data)
        else:
            write_json(journal, data)
    write_json(shutdown.pointer_path(runtime),
               dict(version=1, owner=saved_owner, token=token, kind=kind,
                    before_paste=kind == 'attempt', journal=journal.relative_to(runtime).as_posix()))
    return journal


def snapshot(runtime):
    return {
        str(path.relative_to(runtime)): (path.stat().st_ino, path.read_bytes() if path.is_file() else None)
        for path in runtime.rglob('*')
    }


def readings(ship, capsys):
    found = {}
    for command in (('status', '--line'), ('status',), ('doctor',)):
        result = cli.main(['--ship', str(ship.folder), *command])
        captured = capsys.readouterr()
        assert not captured.err
        assert result in ((0, 1) if command == ('doctor',) else (0,))
        found[command] = captured.out
    return found


@pytest.mark.parametrize(('case', 'reason'), [
    ('unreadable', 'unreadable delivery journal'),
    ('displaced', 'displaced delivery journal'),
    ('mismatch', 'saved delivery identity mismatch'),
    ('unknown-owner', 'unknown service owner stamp'),
])
def test_commands_name_left_startup_pointer_without_effect(ship, capsys, case, reason):
    journal = evidence(ship.runtime, old_owner(ship.runtime, save=case != 'unknown-owner'), case)
    before = snapshot(ship.runtime)
    views = readings(ship, capsys)
    for view in views.values():
        assert view.count(PREFIX + reason) == 1
        assert PRIVATE_WORDS not in view
        assert str(journal) not in view and journal.name not in view
    assert views[('status', '--line')].startswith('SERVICE DOWN')
    assert f'FAIL  delivery startup · {PREFIX}{reason}' in views[('doctor',)]
    assert snapshot(ship.runtime) == before


@pytest.mark.parametrize('case', ['none', 'active', 'resolvable', 'early-attempt'])
def test_commands_keep_normal_startup_and_active_delivery_quiet(ship, capsys, case):
    def quiet():
        before = snapshot(ship.runtime)
        assert all(PREFIX not in view for view in readings(ship, capsys).values())
        assert snapshot(ship.runtime) == before

    if case == 'active':
        running = shutdown.Stop(ship.runtime)
        with running.installed():
            evidence(ship.runtime, running.owner, 'unreadable')
            quiet()
    else:
        if case != 'none':
            evidence(ship.runtime, old_owner(ship.runtime), case)
        quiet()
