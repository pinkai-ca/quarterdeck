"""Ordinary complete status publication, cold fallbacks and headless demand periods."""

import json
from types import SimpleNamespace
import time

import pytest

from quarterdeck import cli, output, post, presentation, service, servicectl, ship as shipmod, status_snapshot, watch


@pytest.fixture
def published(tmp_path, monkeypatch):
    (tmp_path / 'ship.toml').write_text('[ship]\nname="synthetic"\ncode="checkout"\n')
    ship = shipmod.load(tmp_path)
    ship.runtime.mkdir()
    lock = servicectl.acquire_lock(ship.runtime)
    servicectl.write_heartbeat(ship.runtime)
    servicectl.write_pass(ship.runtime, 'pass')  # a running service's main loop stamps its pass beside the beat
    clock = [time.time_ns(), time.monotonic_ns()]
    monkeypatch.setattr(status_snapshot, 'time', SimpleNamespace(time_ns=lambda: clock[0], monotonic_ns=lambda: clock[1]))
    reading = watch.status_reading(ship, SimpleNamespace())
    status_snapshot.publish(ship, reading, wall=clock[0], monotonic=clock[1],
                            framework=presentation.framework_stamp(), stamp=presentation.fingerprint(ship.file))
    assert status_snapshot.path(ship.runtime).exists()
    monkeypatch.setattr(cli, 'load_ship', lambda args: ship)
    monkeypatch.setattr(cli.Tmux, 'for_ship', lambda ship: SimpleNamespace())
    try:
        yield ship, lock, clock, reading
    finally:
        lock.close()


def status():
    return cli.cmd_status(SimpleNamespace(json=True, line=False)).data


def test_fresh_json_status_preserves_every_feed_field(published, monkeypatch):
    ship, _, _, cold = published
    before = status_snapshot.path(ship.runtime).read_bytes()
    def unused(*args, **kwargs):
        raise AssertionError('a fresh service reading must not collect cold facts')
    monkeypatch.setattr(watch, 'status_reading', unused)
    result = status()
    label = result.pop('reading')
    assert result == output.value(cold)
    assert label['mode'] == 'served' and label['label'] == 'served: service reading at ' + label['at']
    assert status_snapshot.path(ship.runtime).read_bytes() == before
    assert status_snapshot.requested(ship)


def test_fresh_json_status_accepts_a_publication_without_the_waiting_seat(published):
    ship, _, _, _ = published
    path = status_snapshot.path(ship.runtime)
    publication = json.loads(path.read_text())
    del publication['reading']['facts']['oldest_waiting_seat']
    path.write_text(json.dumps(publication))
    result = status()
    assert result['reading']['mode'] == 'served'
    assert 'oldest_waiting_seat' not in result['facts']


@pytest.mark.parametrize('change', ['missing', 'stale', 'partial', 'incomplete', 'unreadable', 'down'])
def test_unavailable_service_reading_falls_back_cold(published, monkeypatch, change):
    ship, lock, clock, original = published
    path = status_snapshot.path(ship.runtime)
    if change == 'missing':
        path.unlink()
    elif change == 'stale':
        clock[:] = [value + status_snapshot.MAX_AGE_NS for value in clock]
    elif change == 'partial':
        path.write_text('{')
    elif change == 'incomplete':
        path.write_text(json.dumps(dict(format=1)))
    elif change == 'unreadable':
        read = status_snapshot._read
        def unavailable(runtime, name, *args):
            if name == status_snapshot.NAME:
                raise OSError('ordinary unavailable reading')
            return read(runtime, name, *args)
        monkeypatch.setattr(status_snapshot, '_read', unavailable)
    else:
        lock.close()
    calls = []
    cold = watch.status_reading
    def collect(*args, **kwargs):
        calls.append(True)
        return cold(*args, **kwargs)
    monkeypatch.setattr(watch, 'status_reading', collect)
    result = status()
    label = result.pop('reading')
    assert calls == [True] and label['mode'] == 'cold'
    assert label['label'] == 'cold: independent reading at ' + label['at']
    assert result == output.value(original if change != 'down' else cold(ship, SimpleNamespace()))


def test_failed_demand_write_keeps_cold_reader_even_with_a_fresh_publication(published, monkeypatch):
    _, _, _, original = published
    def unavailable(*args, **kwargs):
        raise PermissionError('ordinary unwritable destination')
    monkeypatch.setattr(status_snapshot, '_write', unavailable)
    result = status()
    assert result.pop('reading')['mode'] == 'cold'
    assert result == output.value(original)


def test_demand_never_renews_the_readings_time(published):
    ship, _, clock, _ = published
    before = status_snapshot.path(ship.runtime).read_bytes()
    clock[:] = [value + 30_000_000_000 for value in clock]
    assert status_snapshot.request(ship)
    assert status_snapshot.served(ship)['reading']['mode'] == 'served'
    clock[:] = [value + 30_000_000_000 for value in clock]
    assert status_snapshot.requested(ship)
    assert status_snapshot.served(ship) is None
    assert status_snapshot.path(ship.runtime).read_bytes() == before


def test_headless_periodic_collection_begins_on_demand_and_stops_after_five_quiet_minutes(published, monkeypatch):
    ship, _, epoch, _ = published
    now = [1000.0]
    actor = service.Service(ship, SimpleNamespace(clients_attached=lambda sessions: False))
    monkeypatch.setattr(service, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    observed = []
    monkeypatch.setattr(actor, 'heartbeat', lambda: None)
    monkeypatch.setattr(actor, 'publish_status', lambda: observed.append(actor.status_request[0]) or True)
    monkeypatch.setattr(service.service_status, 'inputs', lambda ship: ())
    try:
        actor.status_due()
        assert observed == []
        assert status_snapshot.request(ship)
        now[0] += 1
        actor.status_due()
        assert observed == ['full']
        now[0] += ship.status_refresh - 1
        actor.status_due()
        assert observed == ['full']
        now[0] += 1
        actor.status_due()
        assert observed == ['full', 'full']
        epoch[:] = [value + status_snapshot.DEMAND_NS for value in epoch]
        now[0] += 300
        actor.status_due()
        assert observed == ['full', 'full']
        assert status_snapshot.request(ship)
        now[0] += 1
        actor.status_due()
        assert observed == ['full', 'full', 'full']
    finally:
        actor.ledger.close()


def test_whole_service_publication_uses_its_warm_store_and_both_indexes(tmp_path, monkeypatch):
    (tmp_path / 'ship.toml').write_text('[ship]\nname="synthetic"\ncode="checkout"\n')
    ship = shipmod.load(tmp_path)
    ship.runtime.mkdir()
    lock = servicectl.acquire_lock(ship.runtime)
    actor = service.Service(ship, SimpleNamespace())
    message = actor.store.new('order', 'issuer', 'recipient', 'Ordinary status work')
    loads = []
    load = post.Store._load_current
    def counted(store):
        loads.append(store)
        return load(store)
    monkeypatch.setattr(post.Store, '_load_current', counted)
    try:
        actor.first_beat()
        assert actor.publish_status()
        assert loads == [actor.store]
        served = status_snapshot.served(ship)
        assert served is not None
        assert served['open_orders'] == [output.value(message)]
        served.pop('reading')
        assert served == output.value(watch.status_reading(ship, SimpleNamespace()))
    finally:
        actor.ledger.close()
        lock.close()


def test_status_publication_and_demand_do_not_change_accounting_certificate(published, monkeypatch):
    from quarterdeck import accounting_gate, trusted_files
    ship, _, _, _ = published
    trusted_files.root(ship.runtime).mkdir(exist_ok=True)
    status_snapshot.invalidate(ship.runtime)
    status_snapshot.path(ship.runtime).parent.rmdir()
    monkeypatch.setattr(presentation, 'time', status_snapshot.time)
    actor = service.Service(ship, SimpleNamespace())
    native_time = time.time_ns
    monkeypatch.setattr(accounting_gate, 'time', SimpleNamespace(
        time_ns=lambda: native_time() + 10 * post.POST_TIMESTAMP_TICK_NS,
        monotonic=time.monotonic))
    gate = accounting_gate.Gate()
    try:
        actor.first_beat()
        gate.begin(actor, ())
        gate.finish(actor, actor.ledger.db.total_changes, full=True)
        assert gate.completed is not None and not gate.allowed(actor)
        before = accounting_gate.capture(actor, ())
        assert status_snapshot.path(ship.runtime).parent == ship.runtime / 'status'
        for _ in range(2):
            assert actor.publish_status()
            assert accounting_gate.capture(actor, ()) == before
            assert not gate.allowed(actor)
        assert status_snapshot.request(ship)
        assert status_snapshot.served(ship) is not None
        assert accounting_gate.capture(actor, ()) == before
        assert not gate.allowed(actor)
    finally:
        actor.ledger.close()
