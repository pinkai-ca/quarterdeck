"""Saturated daily checks defer without running programs or owning live servers."""

import fcntl
import os
from datetime import timedelta, timezone
import subprocess
from types import SimpleNamespace

import pytest

from quarterdeck import check_capacity, check_outcome, daily_live
from quarterdeck.harness.limits import Usage, Window
from quarterdeck.ledger import Ledger
from tests.testrunner.layout import Slot
from .test_daily_live import NOW, YESTERDAY, reading, ship


@pytest.fixture
def slots(tmp_path, monkeypatch):
    root = tmp_path / 'slots'
    root.mkdir(mode=0o700)
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    protocol = SimpleNamespace(path=root, fd=directory)
    # The actual test tool creates the files. The daily reservation must use
    # these same names/locks, rather than a similarly named second budget.
    tool = Slot(protocol)
    monkeypatch.setattr(check_capacity, 'folder', lambda origin: root)
    try:
        yield root, protocol, tool
    finally:
        tool.release()
        os.close(directory)


def forbid(*args, **kwargs):
    raise AssertionError('saturation started a check, copied a ship or opened a server')


def no_launch(monkeypatch):
    monkeypatch.setattr(daily_live, 'ship_root', forbid)
    monkeypatch.setattr(daily_live, 'installed_qd', forbid)
    monkeypatch.setattr(daily_live, 'invoke', forbid)
    monkeypatch.setattr(daily_live, 'open_private', forbid)
    monkeypatch.setattr(daily_live.shutil, 'copytree', forbid)


def test_recorded_subscription_wall_is_not_a_failed_check_and_names_reset(tmp_path, monkeypatch, slots):
    origin = ship(tmp_path / 'origin')
    origin.runtime.mkdir()
    reset = NOW + timedelta(days=4)
    with Ledger(origin.runtime / 'ledger.sqlite') as book:
        book.record_usage('check-grok', Usage((Window('7d', 1.0, reset),), NOW))
    no_launch(monkeypatch)
    # The meter is checked before even consulting capacity or creating scratch.
    monkeypatch.setattr(check_capacity, 'reserve', forbid)
    report, failed = daily_live.run(origin, now=NOW, readings=[reading('grok', ended=YESTERDAY)],
                                    scratch=tmp_path / 'scratch')
    assert not failed
    assert 'grok: not tested: pool check-grok is at its limit' in report.body
    assert 'reset ' + reset.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC') in report.body
    assert 'next night' in report.body and 'check failed' not in report.body
    assert daily_live.exclusions()['muse'] in report.body
    assert not (tmp_path / 'scratch').exists()
    assert list(origin.runtime.glob('minimal-checks')) == []
    assert list(origin.runtime.glob('seats')) == []
    assert check_outcome.collect(origin.runtime) == ([], [])


def test_a_spent_first_pool_does_not_fall_through_to_another_paid_pool(tmp_path, slots):
    from quarterdeck.ship import load
    origin = ship(tmp_path / 'origin')
    declaration = origin.file.read_text() + '\n[pools.other-grok]\nprovider="xai"\n'
    origin.file.write_text(declaration)
    origin = load(origin.folder)
    origin.runtime.mkdir()
    with Ledger(origin.runtime / 'ledger.sqlite') as book:
        book.record_usage('check-grok', Usage((Window('7d', 1.0, NOW + timedelta(days=1)),), NOW))
        book.record_usage('other-grok', Usage((Window('7d', .1, NOW + timedelta(days=1)),), NOW))
    reason = daily_live.wall_reason(origin, 'grok', NOW)
    assert reason is not None and 'check-grok' in reason


def test_a_wall_recorded_while_reserving_capacity_still_precedes_the_copy(tmp_path, monkeypatch, slots):
    from contextlib import contextmanager
    origin = ship(tmp_path / 'origin')
    origin.runtime.mkdir()
    with Ledger(origin.runtime / 'ledger.sqlite') as book:
        book.record_usage('check-grok', Usage((Window('7d', .9, NOW + timedelta(days=1)),), NOW))
    no_launch(monkeypatch)
    real_reserve = check_capacity.reserve
    @contextmanager
    def reserve_then_wall(ship):
        with real_reserve(ship) as reason:
            with Ledger(origin.runtime / 'ledger.sqlite') as book:
                book.record_usage('check-grok', Usage((Window('7d', 1.0, NOW + timedelta(days=1)),), NOW))
            yield reason
    monkeypatch.setattr(check_capacity, 'reserve', reserve_then_wall)
    report, failed = daily_live.run(origin, now=NOW, readings=[reading('grok')])
    assert not failed and 'pool check-grok is at its limit' in report.body


def test_all_four_actual_tool_locks_defer_once_without_resources_or_alarm(tmp_path, monkeypatch, slots):
    root, protocol, tool = slots
    tool.acquire({'verb': 'synthetic holders'}, count=4)
    before = {path.name: path.read_bytes() for path in root.iterdir()}
    origin = ship(tmp_path / 'origin')
    no_launch(monkeypatch)
    attempts = []
    actual = check_capacity.reserve_at
    def observe(path):
        attempts.append(path)
        return actual(path)
    monkeypatch.setattr(check_capacity, 'reserve_at', observe)
    report, failed = daily_live.run(origin, now=NOW, readings=[reading('grok', ended=YESTERDAY)],
                                    scratch=tmp_path / 'scratch')
    assert not failed and attempts == [root]
    assert 'grok: not tested: all four test slots are occupied' in report.body
    assert 'next night' in report.body and 'check failed' not in report.body
    assert not (tmp_path / 'scratch').exists()
    assert not (origin.runtime / 'seats').exists()
    assert not (origin.runtime / 'minimal-checks').exists()
    assert check_outcome.collect(origin.runtime) == ([], [])
    assert {path.name: path.read_bytes() for path in root.iterdir()} == before


def test_one_slot_remains_reserved_through_check_and_is_released_after(tmp_path, monkeypatch, slots):
    root, protocol, held = slots
    held.acquire({'verb': 'three synthetic holders'}, count=3)
    origin = ship(tmp_path / 'origin')
    calls = []
    def runner(*args):
        calls.append(args[1])
        probe = Slot(protocol)
        try:
            from tests.testrunner.layout import Refused
            with pytest.raises(Refused, match='all 4 test slots are busy'):
                probe.acquire({'verb': 'synthetic probe'})
        finally:
            probe.release()
        return daily_live.CheckRun('grok', 0, 'stood in')
    report, failed = daily_live.run(origin, now=NOW, readings=[reading('grok', ended=YESTERDAY)],
                                    runner=runner, scratch=tmp_path / 'scratch', qd='stand-in')
    assert not failed and calls == ['grok']
    assert 'check passed' in report.body
    probe = Slot(protocol)
    try:
        probe.acquire({'verb': 'released-slot probe'})
    finally:
        probe.release()


def test_a_deferred_program_is_tried_on_the_next_night(tmp_path, monkeypatch, slots):
    root, protocol, held = slots
    held.acquire({'verb': 'synthetic holders'}, count=4)
    origin = ship(tmp_path / 'origin')
    calls = []
    def runner(*args):
        calls.append(args[1])
        return daily_live.CheckRun(args[1], 0, '')
    first, failed = daily_live.run(origin, now=NOW, readings=[reading('grok')], runner=runner, qd='stand-in')
    assert not failed and 'not tested' in first.body and not calls
    held.release()
    held.fds, held.held = [], []  # the fixture may close an already released holder
    second, failed = daily_live.run(origin, now=NOW + timedelta(days=1), readings=[reading('grok')],
                                    runner=runner, scratch=tmp_path / 'scratch', qd='stand-in')
    assert not failed and calls == ['grok']
    assert 'check passed' in second.body


def test_capacity_folder_is_shared_by_linked_worktrees(tmp_path, monkeypatch):
    import quarterdeck.install as install
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', '-C', str(repo), 'init', '--quiet'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Synthetic', '-c',
                    'user.email=synthetic@example.invalid', 'commit', '--quiet', '--allow-empty', '-m', 'fixture'], check=True)
    linked = tmp_path / 'linked'
    subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '--quiet', '--detach', str(linked)], check=True)
    monkeypatch.setattr(install, 'source_repo', lambda origin: repo)
    assert check_capacity.folder(None) == repo / '.testruns'
    monkeypatch.setattr(install, 'source_repo', lambda origin: linked)
    assert check_capacity.folder(None) == repo / '.testruns'


def test_unavailable_capacity_is_deferred_without_creating_a_budget(tmp_path, monkeypatch):
    origin = ship(tmp_path / 'origin')
    missing = tmp_path / 'absent-slots'
    monkeypatch.setattr(check_capacity, 'folder', lambda origin: missing)
    no_launch(monkeypatch)
    report, failed = daily_live.run(origin, now=NOW, readings=[reading('grok')])
    assert not failed and 'shared test slot availability is unknown' in report.body
    assert not missing.exists()
