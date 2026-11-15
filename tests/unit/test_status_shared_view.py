"""Read-only presentation shares post work and refreshes between collections."""
from types import SimpleNamespace
from pathlib import Path

import pytest

from quarterdeck import fleet_post, forward_post, human_wake, post, post_dir, ship as shipmod
from quarterdeck.service import Service


def inventory(tmp_path):
    runtime = tmp_path / 'runtime'
    store, attempts = post.Store(runtime), post.Attempts(runtime)
    message = store.new('order', 'issuer', 'reader', 'Synthetic status work')
    attempt = attempts.close(attempts.open(message, 'reader', None, None, None), 'queued')
    return runtime, store, attempts, message, attempt


def test_attempt_view_uses_one_inventory_across_instances_then_refreshes(tmp_path, monkeypatch):
    runtime, store, attempts, message, attempt = inventory(tmp_path)
    calls = []
    read = post.Attempts._all_current
    def counted(reader, **kwargs):
        calls.append(reader)
        return read(reader, **kwargs)
    monkeypatch.setattr(post.Attempts, '_all_current', counted)
    with post.reading_pass(store=store, attempts=attempts):
        assert attempts.latest()[message.id] == 'queued'
        with post.reading_pass():
            assert post.Attempts(runtime).latest()[message.id] == 'queued'
        assert post.Attempts(runtime).latest()[message.id] == 'queued'
    assert calls == [attempts]
    attempts.close(attempt, 'delivered')
    with post.reading_pass(store=store, attempts=attempts):
        assert post.Attempts(runtime).latest()[message.id] == 'delivered'
    assert calls == [attempts, attempts]
    assert post.Attempts(runtime).latest()[message.id] == 'delivered'
    assert len(calls) == 3, 'ordinary readers outside a view still scan independently'


def test_attempt_views_are_separate_for_each_runtime(tmp_path):
    a, _, aa, am, _ = inventory(tmp_path / 'a')
    b, _, ba, bm, _ = inventory(tmp_path / 'b')
    with post.reading_pass():
        assert {x.message for x in aa.all()} == {am.id}
        assert {x.message for x in ba.all()} == {bm.id}
        assert {x.message for x in post.Attempts(a).all()} == {am.id}
        assert {x.message for x in post.Attempts(b).all()} == {bm.id}


def test_post_view_reuses_seat_projection_and_refreshes_next_reading(tmp_path, monkeypatch):
    runtime, store, _, message, _ = inventory(tmp_path)
    checks = []
    original = fleet_post.context_only
    def checked(folder, identifier):
        checks.append(identifier)
        return original(folder, identifier)
    monkeypatch.setattr(fleet_post, 'context_only', checked)
    with post.reading_pass(store=store):
        assert post.Store(runtime).for_seat('reader') == [message]
        assert store.for_seat('reader') == [message]
    assert checks == [message.id]
    another = store.new('order', 'issuer', 'reader', 'New status work')
    with post.reading_pass(store=store):
        assert [m.id for m in post.Store(runtime).for_seat('reader')] == [message.id, another.id]
    assert len(checks) == 3


def test_metadata_directory_inventory_is_local_to_the_presentation(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    listings, reads = [], []
    listing, stable = post_dir.list_children, fleet_post.stable_bytes
    def listed(folder):
        listings.append(Path(folder))
        return listing(folder)
    def read(path):
        reads.append(Path(path))
        return stable(path)
    monkeypatch.setattr(post_dir, 'list_children', listed)
    monkeypatch.setattr(fleet_post, 'stable_bytes', read)
    with post.reading_pass():
        for index in range(20):
            assert fleet_post.provenance(runtime, str(index)) is None
            assert forward_post.recorded(runtime, str(index)) is None
    assert listings.count(runtime / 'post/imports') == 1
    assert listings.count(runtime / 'post/forwarded') == 1
    assert reads == []
    folder = runtime / 'post/imports'
    folder.mkdir(parents=True)
    source = folder / 'sample.json'
    source.write_text('{"generation":1}')
    with post.reading_pass():
        assert fleet_post.provenance(runtime, 'sample') == {'generation': 1}
        assert fleet_post.provenance(runtime, 'sample') == {'generation': 1}
    assert reads == [source]
    source.write_text('{"generation":2}')
    with post.reading_pass():
        assert fleet_post.provenance(runtime, 'sample') == {'generation': 2}
    source.unlink()
    with post.reading_pass():
        assert fleet_post.provenance(runtime, 'sample') is None


def test_failed_read_is_not_stored_as_a_successful_view():
    calls = []
    def unavailable():
        calls.append(True)
        raise OSError('synthetic unavailable data')
    with post.reading_pass():
        for _ in range(2):
            with pytest.raises(OSError, match='synthetic unavailable'):
                post.reading_value('sample', 'one', unavailable)
    assert len(calls) == 2


def test_human_presentation_shares_the_callers_attempt_reader(tmp_path, monkeypatch):
    runtime, store, attempts, message, _ = inventory(tmp_path)
    launch = SimpleNamespace(pane='%synthetic', pid=123)
    pane = SimpleNamespace(id=launch.pane, pid=launch.pid, dead=False)
    seat = SimpleNamespace(name='reader', folder=runtime / 'seats/reader', ship=SimpleNamespace(runtime=runtime),
                           launched=lambda: launch, pane=lambda: pane)
    monkeypatch.setattr(post, 'watch_reading', lambda *args: post.WatchReading(True))
    scans = []
    read = post.Attempts._all_current
    def counted(reader, **kwargs):
        scans.append(reader)
        return read(reader, **kwargs)
    monkeypatch.setattr(post.Attempts, '_all_current', counted)
    with post.reading_pass(store=store, attempts=attempts):
        assert attempts.latest()[message.id] == 'queued'
        for _ in range(5):
            assert human_wake.status(seat) is None
    assert scans == [attempts]


def test_short_passes_keep_the_change_check_deadline_and_periodic_refresh(tmp_path, monkeypatch):
    ship = shipmod.Ship(tmp_path, 'synthetic', 'operator', tmp_path, {}, {}, {})
    svc = Service.__new__(Service)
    svc.ship = ship
    svc.status_checked = None
    svc.next_status = 0.0
    svc.tmux = SimpleNamespace(clients_attached=lambda sessions: True)
    svc.rebind = lambda: None
    svc.structure_passes = SimpleNamespace(begin=lambda: None, finish=lambda: None)
    svc.heartbeat = lambda: None
    svc.log = lambda kind, **kwargs: None if kind == 'service-timing' else pytest.fail(str(kwargs))
    now = [1.0]
    monkeypatch.setattr('quarterdeck.service.time.monotonic', lambda: now[0])
    monkeypatch.setattr('quarterdeck.service.ship_file_stamp', lambda ship: None)
    monkeypatch.setattr('quarterdeck.service.shipmod.load', lambda folder: ship)
    monkeypatch.setattr('quarterdeck.service.service_status.inputs', lambda ship: 'unchanged')
    calls = []
    svc.publish_status = lambda: calls.append(now[0])
    svc._tick_steps = svc.status_due
    svc.tick()
    now[0] = 1.1
    svc.tick()
    assert calls == [1.0], 'a short pass must not force another full collection'
    now[0] = 1.8
    svc.status_due()
    assert calls == [1.0], 'an unchanged check is cheap'
    now[0] = 31.0
    svc.status_due()
    assert calls == [1.0, 31.0], 'periodic collection refreshes unchanged presentation'
    svc.tmux.clients_attached = lambda sessions: False
    now[0] = 62.0
    svc.tick()
    assert calls == [1.0, 31.0], 'a known no-viewer pass keeps the existing skip'
    svc.tmux.clients_attached = lambda sessions: None
    now[0] = 62.8
    svc.tick()
    assert calls[-1] == 62.8, 'unknown demand cannot suppress a due collection'


def test_reader_reuse_refreshes_each_new_view_after_external_changes(tmp_path, monkeypatch):
    runtime, store, attempts, message, attempt = inventory(tmp_path)
    scanned = []
    original = post.Attempts._all_current
    def scan(reader, **kwargs):
        scanned.append(reader)
        return original(reader, **kwargs)
    monkeypatch.setattr(post.Attempts, '_all_current', scan)
    with post.reader_scope(store=store, attempts=attempts):
        with post.reading_pass():
            assert post.Attempts(runtime).latest()[message.id] == 'queued'
            assert post.Store(runtime).for_seat('reader') == [message]
        attempts.close(attempt, 'delivered')
        added = store.new('order', 'issuer', 'reader', 'Arrived between readings')
        with post.reading_pass():
            assert post.Attempts(runtime).latest()[message.id] == 'delivered'
            assert [m.id for m in post.Store(runtime).for_seat('reader')] == [message.id, added.id]
    assert scanned == [attempts, attempts], 'reader objects are shared, each metadata view is freshly collected'
