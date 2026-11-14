"""Ordinary report filing by a check host, with and without a live service."""

import fcntl
import os
from types import SimpleNamespace

from quarterdeck import check, post, post_dir, servicectl


def waiting_report(tmp_path):
    runtime = tmp_path / 'runtime'
    store = post.Store(runtime)
    name = 'check-fake-synthetic'
    seat = SimpleNamespace(name=name, ship=SimpleNamespace(runtime=runtime))
    order = store.new('order', 'ship', name, 'Synthetic check task')
    report = store.prepare('report', name, 'ship', 'written', re=order.id)
    drop = post.write_drop(runtime, report)
    return seat, store, order, report, drop


def test_host_files_check_report_and_closes_order_without_service(tmp_path):
    seat, store, order, report, drop = waiting_report(tmp_path)
    assert not servicectl.alive(seat.ship.runtime)
    assert check.closing_report(seat, store, order) == report
    assert not drop.exists()
    assert not store.is_open(order)
    assert check.closing_report(seat, store, order) == report
    assert store.all() == [order, report]


def test_host_stands_aside_for_live_service_then_files_after_it_ends(tmp_path):
    seat, store, order, report, drop = waiting_report(tmp_path)
    with servicectl.acquire_lock(seat.ship.runtime):
        assert servicectl.alive(seat.ship.runtime)
        assert check.closing_report(seat, store, order) is None
        assert drop.exists() and store.is_open(order)
        # The service still uses the same filing door during its lifetime.
        assert post.file_drops(store) == []
        assert check.closing_report(seat, store, order) == report
    assert not drop.exists()


def test_host_and_service_yield_while_shared_filing_lock_is_owned(tmp_path):
    seat, store, order, report, drop = waiting_report(tmp_path)
    folder = seat.ship.runtime / 'locks'
    folder.mkdir(mode=0o700)
    descriptor = post_dir.open_lock(folder / 'filing.lock', mode=0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert check.closing_report(seat, store, order) is None
        assert post.file_drops(store) == []
        assert drop.exists() and store.is_open(order)
    finally:
        os.close(descriptor)
    assert check.closing_report(seat, store, order) == report


def test_filing_lock_lives_under_runtime_locks(tmp_path):
    seat, store, order, report, drop = waiting_report(tmp_path)
    assert check.closing_report(seat, store, order) == report
    folder = seat.ship.runtime / 'locks'
    assert (folder / 'filing.lock').is_file()
    assert folder.stat().st_mode & 0o777 == 0o700
    assert not (seat.ship.runtime / 'post' / 'filing.lock').exists()
