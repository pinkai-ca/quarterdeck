"""A ship service that still runs after its run's stops holds the cleanup: nothing is removed, and the outcome is never
complete. The process is this test's own sleeper; the tree kill is made to fail."""
import json
import subprocess
import sys
from pathlib import Path

from tests.testrunner import clean, procs, services


def sleeper(ship: Path):
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)', '--ship', str(ship), 'service'],
                               start_new_session=True)
    for _ in range(100):
        if procs.start_time(process.pid):
            break
    return process, procs.start_time(process.pid)


def test_a_service_that_outlives_both_stops_holds_the_cleanup(tmp_path, monkeypatch):
    scratch = tmp_path / 'scratch'
    ship = scratch / 'ship'
    ship.mkdir(parents=True)
    process, start = sleeper(ship)
    try:
        monkeypatch.setattr(procs, 'end_tree', lambda pid, start, **_: {pid: start})  # the kill fails
        stopped = services.stop(tmp_path / 'records', scratch)
        assert stopped['outcome'] == 'held'
        [entry] = stopped['entries']
        assert (entry['pid'], entry['outcome'], entry['left']) == (process.pid, 'held', [process.pid])
        assert entry['found_by'] == 'its command line', 'found by its ship in the run\'s scratch, unrecorded'
    finally:
        process.kill()
        process.wait()


def test_a_held_service_keeps_the_runs_scratch_whole(tmp_path, monkeypatch):
    removed = []
    monkeypatch.setattr(clean, 'kept', lambda root, run: None)
    monkeypatch.setattr(clean.storage, 'path', lambda root, run: tmp_path / 'scratch')
    monkeypatch.setattr(clean.services, 'stop', lambda folder, scratch: {'outcome': 'held', 'entries': [{'pid': 1}]})
    monkeypatch.setattr(clean.owned, 'retire', lambda *args, **kwargs: removed.append(args))
    root = type('Root', (), {'path': tmp_path, 'project': tmp_path})()
    cleanup = clean._retire_run(root, -1, 'run-synthetic', {})
    assert cleanup['outcome'] == 'held' and 'still runs' in cleanup['reason'] and removed == []


def test_records_and_the_scan_name_one_service_once(tmp_path):
    scratch = tmp_path / 'scratch'
    ship = scratch / 'ship'
    ship.mkdir(parents=True)
    process, start = sleeper(ship)
    try:
        folder = tmp_path / 'records'
        folder.mkdir()
        (folder / f'{process.pid}-{start}.json').write_text(json.dumps(dict(ship=str(ship), pid=process.pid,
                                                                             start=start)))
        stopped = services.stop(folder, scratch)
        assert [e['pid'] for e in stopped['entries']] == [process.pid] and stopped['outcome'] == 'stopped'
        assert procs.alive({process.pid: start}) == {}
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


def test_a_service_of_another_folder_is_never_touched(tmp_path):
    elsewhere = tmp_path / 'elsewhere' / 'ship'
    elsewhere.mkdir(parents=True)
    process, start = sleeper(elsewhere)
    try:
        stopped = services.stop(tmp_path / 'records', tmp_path / 'scratch')
        assert stopped == {'outcome': 'none', 'entries': []} and process.poll() is None
    finally:
        process.kill()
        process.wait()


def test_servicectl_records_each_service_it_starts_for_the_test_run(tmp_path, monkeypatch):
    """Under the test tool, a started service is recorded at once: its ship, runtime, pid and start, scope, and the
    interpreter and package that started it; with no such folder named, nothing is written."""
    import os
    from types import SimpleNamespace
    from quarterdeck import servicectl
    ship = SimpleNamespace(folder=tmp_path / 'ship', runtime=tmp_path / 'ship' / 'runtime')
    monkeypatch.delenv(servicectl.TEST_RUN_RECORDS, raising=False)
    servicectl.record_for_test_run(ship, os.getpid(), [])
    assert not (tmp_path / 'records').exists()
    monkeypatch.setenv(servicectl.TEST_RUN_RECORDS, str(tmp_path / 'records'))
    servicectl.record_for_test_run(ship, os.getpid(), ['systemd-run', '--unit=qd-service-x-0000.scope', '--'])
    [path] = list((tmp_path / 'records').iterdir())
    data = json.loads(path.read_text())
    assert data['pid'] == os.getpid() and data['start'] == procs.start_time(os.getpid())
    assert data['ship'] == str(ship.folder) and data['runtime'] == str(ship.runtime)
    assert data['unit'] == 'qd-service-x-0000.scope' and data['python'] == sys.executable
    assert (Path(data['package']) / 'quarterdeck' / 'servicectl.py').is_file()
    assert servicectl.TEST_RUN_RECORDS == services.RECORD_ENV, 'the tool and the framework name one folder'


def test_a_record_naming_a_ship_outside_the_scratch_is_never_stopped_and_holds_the_cleanup(tmp_path):
    """Whatever process wrote it, a record of a ship outside the run's own scratch never leads the tool to stop that
    service: it is left running, named in the result, and the cleanup is held."""
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    elsewhere = tmp_path / 'real-ship'
    elsewhere.mkdir()
    process, start = sleeper(elsewhere)
    try:
        folder = tmp_path / 'records'
        folder.mkdir()
        (folder / f'{process.pid}-{start}.json').write_text(json.dumps(dict(
            ship=str(elsewhere), runtime=str(elsewhere / 'runtime'), pid=process.pid, start=start,
            python=sys.executable, package=str(tmp_path / 'package'))))
        stopped = services.stop(folder, scratch)
        assert stopped['outcome'] == 'held'
        [entry] = stopped['entries']
        assert entry['outcome'] == 'held' and 'outside' in entry['reason'] and 'own_stop' not in entry
        assert process.poll() is None and procs.alive({process.pid: start}), 'left running, never signalled'
    finally:
        process.kill()
        process.wait()
