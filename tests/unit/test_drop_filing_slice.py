"""Bounded post turns and folder-only age alerts, using ordinary synthetic drops."""

from dataclasses import replace
import os
import time
from types import SimpleNamespace

import pytest

from quarterdeck import group_post, post, service, ship as shipmod, ship_locks, watch
from .test_check_lifetime import synthetic_ship


def drop(store, sender='worker', body='synthetic private payload'):
    message = store.prepare('report', sender, 'ship', body)
    return message, post.write_drop(store.dir.parent.parent, message)


def test_slices_resume_complete_file_operations_and_release_the_lock(tmp_path, monkeypatch):
    store = post.Store(tmp_path / 'runtime')
    messages = [drop(store, sender=name)[0] for name in ('first', 'first', 'first', 'last', 'last')]
    filed = []
    publish = store.publish_text
    def observed(message, text):
        filed.append(message.id)
        return publish(message, text)
    monkeypatch.setattr(store, 'publish_text', observed)
    filing = post.FilingSlice(seconds=0, operations=1)
    calls = 0
    while calls < 30:
        before = len(filed)
        post.file_drops(store, filing=filing)
        calls += 1
        assert len(filed) - before <= 1
        # No filing/admission lease survives a yielded slice.
        with ship_locks.held(ship_locks.path(tmp_path / 'runtime', 'filing.lock'), 'synthetic peer', wait=0):
            pass
        if filing.steps is None:
            break
    assert filing.steps is None and calls > 1
    assert sorted(filed) == sorted(message.id for message in messages)
    assert len(filed) == len(set(filed))
    assert {message.id for message in store.all()} == set(filed)
    assert not list((tmp_path / 'runtime/post/drop').glob('*/*.json'))


def test_whole_filing_between_slices_invalidates_the_paused_snapshot(tmp_path):
    store = post.Store(tmp_path / 'runtime')
    first, _ = drop(store)
    filing = post.FilingSlice(seconds=0, operations=1)
    post.file_drops(store, filing=filing)  # folder boundary
    post.file_drops(store, filing=filing)  # snapshot contains the first file
    post.file_drops(store)  # a different caller files it while the scan is paused
    second, _ = drop(store)
    for _ in range(10):
        post.file_drops(store, filing=filing)
        if filing.steps is None:
            break
    assert filing.steps is None
    assert {message.id for message in store.all()} == {first.id, second.id}


def test_group_intents_finish_between_slices_without_retaining_admission(tmp_path):
    from pathlib import Path
    template = Path(__file__).parents[2] / 'ships/forum/ship.toml'
    (tmp_path / 'ship.toml').write_text(template.read_text())
    ship = shipmod.load(tmp_path)
    store = post.Store(ship.runtime)
    roots = [group_post.stage(ship, store, 'reviewers', 'lead', f'synthetic group {n}')[0]
             for n in range(2)]
    filing = post.FilingSlice(seconds=0, operations=1)
    for _ in range(40):
        post.file_drops(store, ship, filing=filing)
        with post.admission(ship.runtime, '@groups', wait=0):
            pass
        if filing.steps is None:
            break
    assert filing.steps is None
    assert len(store.all()) == len(roots) * (1 + len(ship.groups['reviewers'].members))
    assert all(store.get(root.id) == root for root in roots)
    assert not list((ship.runtime / 'post/drop').glob('*/*.json'))


def test_cooperative_deadline_limits_progress_without_restarting_the_scan(tmp_path, monkeypatch):
    store = post.Store(tmp_path / 'runtime')
    clock = SimpleNamespace(value=0.0)
    visited = []
    def steps(*args, **kwargs):
        for number in range(20):
            clock.value += 0.01
            visited.append(number)
            yield
    monkeypatch.setattr(post, '_drop_steps', steps)
    monkeypatch.setattr(post, 'time', SimpleNamespace(monotonic=lambda: clock.value))
    filing = post.FilingSlice(seconds=0.025)
    post.file_drops(store, filing=filing)
    assert visited == [0, 1, 2]
    post.file_drops(store, filing=filing)
    assert visited == [0, 1, 2, 3, 4, 5]


@pytest.mark.parametrize('raise_after_drop', [False, True])
def test_first_and_later_service_duties_file_post_before_the_next_duty(tmp_path, monkeypatch, raise_after_drop):
    ship = synthetic_ship(tmp_path)
    svc = service.Service(ship, SimpleNamespace())
    # No other duties or native work run in this boundary test.
    names = ('server_recovery', 'reconcile', 'groups', 'cascades', 'fleet_post', 'inboxes',
             'recoveries', 'refusals', 'answers', 'looks', 'deliveries', 'follow', 'requests',
             'compactions', 'person_quiet', 'fresh_after_order', 'rebrief', 'push_reminders',
             'credentials', 'authentications', 'model_differences', 'failover', 'account',
             'refresh_usage', 'pool_forecasts', 'retire_post', 'heartbeat', 'status_due', 'drain_accounting')
    for name in names:
        monkeypatch.setattr(svc, name, lambda: None)
    svc.drop_filer = post.FilingSlice(seconds=float('inf'))
    waiting = []
    def first_duty():
        assert svc.store.get(waiting[-1].id) == waiting[-1]
        waiting.append(drop(svc.store)[0])
        if raise_after_drop:
            raise OSError('synthetic operational duty failure')
    def following_duty():
        assert svc.store.get(waiting[-1].id) == waiting[-1]
    def later_duty():
        waiting.append(drop(svc.store)[0])
    svc.server_recovery, svc.reconcile = first_duty, following_duty
    svc.groups, svc.cascades = later_duty, following_duty
    try:
        for _ in range(2):  # first pass after start, then an ordinary pass
            waiting.append(drop(svc.store)[0])
            svc._tick_steps()
        assert {message.id for message in svc.store.all()} == {message.id for message in waiting}
    finally:
        svc.ledger.close()


def test_old_drop_alert_reads_metadata_and_keeps_only_the_folder_on_status(tmp_path, monkeypatch):
    ship = synthetic_ship(tmp_path)
    store = post.Store(ship.runtime)
    message, path = drop(store, body='PRIVATE_BODY_MUST_NOT_APPEAR')
    at = time.time()
    os.utime(path, (at - 119, at - 119))
    assert post.drop_waits(ship.runtime, at=at) == []
    os.utime(path, (at - 121, at - 121))
    with monkeypatch.context() as metadata_only:
        def no_content(*args, **kwargs):
            raise AssertionError('age alert read message content')
        metadata_only.setattr(post, '_read_regular', no_content)
        alert, = post.drop_waits(ship.runtime, at=at)
    assert alert == 'post drop waiting: drop/worker (over 2m)'
    for _ in range(2):
        facts = watch.facts(ship, SimpleNamespace(), walls=[], login=[], check_reading=([], []))
        assert alert in facts.line()
        assert alert in replace(facts, pending=['synthetic wait ' * 30]).line()
        assert message.id not in facts.line() and message.body not in facts.line()
    post.file_drops(store, ship)
    assert post.drop_waits(ship.runtime, at=at) == []
