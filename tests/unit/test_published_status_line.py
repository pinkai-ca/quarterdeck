"""Running services reuse publications; missing/down readings collect current waits."""
import json
from types import SimpleNamespace

import pytest

from quarterdeck import cli, output, presentation, watch
from .test_status_presentation import live


def no_collection(*args, **kwargs):
    raise AssertionError('a text status job must not collect live post/seat facts')


@pytest.mark.parametrize('change', ['old', 'different-owner', 'changed-code'])
def test_unverified_publications_keep_alerts_with_explicit_staleness(live, monkeypatch, change):
    ship, svc, lock = live
    data = json.loads((ship.runtime / presentation.NAME).read_text())
    data['line'] = 'service ok · 2 blind · worker needs a human'
    if change == 'old':
        monkeypatch.setattr(presentation.time, 'time_ns', lambda: data['written'] + 46_000_000_000)
        monkeypatch.setattr(presentation.time, 'monotonic_ns', lambda: data['monotonic'] + 46_000_000_000)
    elif change == 'different-owner':
        data['process_start'] = 'different'
    else:
        data['framework'] = ['different']
    path = ship.runtime / presentation.NAME
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    monkeypatch.setattr(watch, 'facts', no_collection)
    line = presentation.command_line(['--ship', str(ship.folder), 'status', '--line'])
    assert 'status stale:' in line and line.endswith('2 blind · worker needs a human')
    assert line.startswith('service ok')
    if change == 'old':
        assert 'published 46s ago' in line
    assert path.read_bytes() == before, 'reading must not renew the publication'


@pytest.mark.parametrize('publication', [None, '{corrupt', '{}', '[]', 'x' * 65537])
def test_no_usable_publication_collects_current_alerts(live, monkeypatch, publication):
    ship, svc, lock = live
    path = ship.runtime / presentation.NAME
    if publication is None:
        path.unlink()
    else:
        path.write_text(publication)
    facts = watch.Facts('ok', 3, 0, 1, needs_human=['waiting-worker'])
    calls = []
    def collect(*args, **kwargs):
        calls.append(True)
        return facts
    monkeypatch.setattr(watch, 'facts', collect)
    line = presentation.command_line(['--ship', str(ship.folder), 'status', '--line'])
    assert line == facts.line() and calls == [True]
    assert 'waiting-worker needs a human' in line


def test_slow_collection_is_published_with_its_actual_start_time(live, monkeypatch):
    ship, svc, lock = live
    wall, mono = presentation.time.time_ns(), presentation.time.monotonic_ns()
    clock = [0]
    monkeypatch.setattr(presentation.time, 'time_ns', lambda: wall + clock[0])
    monkeypatch.setattr(presentation.time, 'monotonic_ns', lambda: mono + clock[0])
    def slow(*args, **kwargs):
        clock[0] = 16_000_000_000
        return watch.Facts('ok', 3, 0, 1, needs_human=['worker'])
    monkeypatch.setattr(watch, 'facts', slow)
    assert presentation.publish(ship, svc.tmux, svc.status_framework, svc.store, svc.attempts)
    data = json.loads((ship.runtime / presentation.NAME).read_text())
    assert data['wall'] == wall and data['monotonic'] == mono
    monkeypatch.setattr(watch, 'facts', no_collection)
    line = presentation.published_line(ship.folder)
    assert data['written'] == wall + 16_000_000_000
    assert presentation.cached_line(ship.folder) is None
    assert line == data['line'] and 'worker needs a human' in line


def test_parser_path_uses_publication_but_json_collects_current_facts(live, monkeypatch, capsys):
    ship, svc, lock = live
    monkeypatch.setattr(cli, 'load_ship', lambda args: ship)
    monkeypatch.setattr(watch, 'facts', no_collection)
    result = cli.cmd_status(SimpleNamespace(line=True, json=False))
    output.emit(result)
    assert capsys.readouterr().out.strip() == presentation.cached_line(ship.folder)
    with pytest.raises(AssertionError, match='must not collect'):
        cli.cmd_status(SimpleNamespace(line=True, json=True))


def test_down_service_collects_named_waits_instead_of_keeping_old_alerts(live, monkeypatch):
    ship, svc, lock = live
    lock.close()
    facts = watch.Facts('down', 1, 2, 0, needs_human=['waiting-worker'], unarmed=['operator'])
    calls = []
    def collect(*args, **kwargs):
        calls.append(True)
        return facts
    monkeypatch.setattr(watch, 'facts', collect)
    assert presentation.published_line(ship.folder) is None
    line = presentation.command_line(['--ship', str(ship.folder), 'status', '--line'])
    assert line == facts.line() and calls == [True]
    assert line.startswith('SERVICE DOWN')
    assert 'waiting-worker needs a human' in line and 'operator: no watch armed' in line


def test_valid_publication_never_loads_store_even_when_heartbeat_is_late(live, monkeypatch):
    import os
    from quarterdeck import post, servicectl
    ship, svc, lock = live
    data = json.loads((ship.runtime / presentation.NAME).read_text())
    monkeypatch.setattr(watch, 'facts', no_collection)
    monkeypatch.setattr(post.Store, '_load_current', no_collection)
    assert presentation.command_line(['--ship', str(ship.folder), 'status', '--line']) == data['line']
    monkeypatch.setattr(servicectl, 'read_heartbeat', lambda runtime: (os.getpid(), 20.))
    line = presentation.command_line(['--ship', str(ship.folder), 'status', '--line'])
    assert line.startswith('service busy 20s · status stale:')
    assert line.endswith(data['line'].partition(' · ')[2])


@pytest.mark.parametrize('field,value', [('wall', 'unknown'), ('monotonic', None),
    ('written', 'unknown'), ('pid', -1), ('process_start', None), ('format', True), ('line', 'service ok\nforged')])
def test_malformed_publication_metadata_uses_the_independent_collector(live, monkeypatch, field, value):
    ship, svc, lock = live
    path = ship.runtime / presentation.NAME
    data = json.loads(path.read_text())
    data[field] = value
    if field == 'written':
        # The unchanged three-second cache does not consume the new field.
        data['wall'] -= 20_000_000_000
        data['monotonic'] -= 20_000_000_000
    path.write_text(json.dumps(data))
    facts = watch.Facts('ok', 1, 0, 0, needs_human=['waiting-worker'])
    calls = []
    def collect(*args, **kwargs):
        calls.append(True)
        return facts
    monkeypatch.setattr(watch, 'facts', collect)
    assert presentation.command_line(['--ship', str(ship.folder), 'status', '--line']) == facts.line()
    assert calls == [True]


@pytest.mark.parametrize('collection_age,written_age,plain', [
    (20, 2, True), (50, 40, True), (50, 40.000000001, False), (50, 30, True)])
def test_publication_recency_uses_write_time_instead_of_collection_start(
        live, monkeypatch, collection_age, written_age, plain):
    from quarterdeck import post
    ship, svc, lock = live
    now, mono = presentation.time.time_ns(), presentation.time.monotonic_ns()
    path = ship.runtime / presentation.NAME
    data = json.loads(path.read_text())
    data.update(wall=now - collection_age * 1_000_000_000,
                monotonic=mono - collection_age * 1_000_000_000,
                written=now - round(written_age * 1_000_000_000))
    path.write_text(json.dumps(data))
    monkeypatch.setattr(presentation.time, 'time_ns', lambda: now)
    monkeypatch.setattr(presentation.time, 'monotonic_ns', lambda: mono)
    monkeypatch.setattr(watch, 'facts', no_collection)
    monkeypatch.setattr(post.Store, '_load_current', no_collection)
    assert presentation.cached_line(ship.folder) is None, 'collection-age cache stays unchanged'
    before = path.read_bytes()
    line = presentation.command_line(['--ship', str(ship.folder), 'status', '--line'])
    if plain:
        assert line == data['line'] and 'status stale' not in line
    else:
        assert f'status stale: published {int(written_age)}s ago' in line
        assert line.endswith(data['line'].partition(' · ')[2])
    assert path.read_bytes() == before


@pytest.mark.parametrize('path_kind', ['bootstrap', 'parser'])
@pytest.mark.parametrize('busy', [False, True])
@pytest.mark.parametrize('refresh', [30, 45.5])
@pytest.mark.parametrize('past_bound_ns', [0, 1, 1_000_000_000])
def test_an_expired_publication_collects_a_newly_waiting_seat(
        drydock, tmux, monkeypatch, path_kind, busy, refresh, past_bound_ns):
    import os
    from quarterdeck import post, servicectl, ship as shipmod
    from quarterdeck.tmux import Tmux
    file = drydock / 'ship.toml'
    file.write_text(file.read_text().replace('[ship]', f'[ship]\nstatus_refresh={refresh}'))
    ship = shipmod.load(drydock)
    bound_ns = round(3 * refresh * 1_000_000_000)
    age_ns = bound_ns + past_bound_ns
    ship.runtime.mkdir(parents=True)
    monkeypatch.setattr(Tmux, 'for_ship', classmethod(lambda cls, current: tmux))
    with servicectl.acquire_lock(ship.runtime):
        servicectl.write_heartbeat(ship.runtime)
        servicectl.write_pass(ship.runtime, 'pass')  # a running service's main loop stamps its pass beside the beat
        store, attempts = post.Store(ship.runtime), post.Attempts(ship.runtime)
        assert presentation.publish(ship, tmux, presentation.framework_stamp(), store, attempts)
        file = ship.runtime / presentation.NAME
        data = json.loads(file.read_text())
        assert 'desk: no watch armed' not in data['line']
        now, mono = presentation.time.time_ns(), presentation.time.monotonic_ns()
        data.update(wall=now - age_ns - 20_000_000_000, monotonic=mono - age_ns - 20_000_000_000,
                    written=now - age_ns)
        file.write_text(json.dumps(data))
        store.new('question', 'operator', 'desk', 'Synthetic input newly waiting for the desk.')
        monkeypatch.setattr(presentation.time, 'time_ns', lambda: now)
        monkeypatch.setattr(presentation.time, 'monotonic_ns', lambda: mono)
        if busy:
            monkeypatch.setattr(servicectl, 'read_heartbeat', lambda runtime: (os.getpid(), 20.))
        collect, calls = watch.facts, []
        def observed(*args, **kwargs):
            calls.append(True)
            return collect(*args, **kwargs)
        monkeypatch.setattr(watch, 'facts', observed)
        if path_kind == 'bootstrap':
            line = presentation.command_line(['--ship', str(ship.folder), 'status', '--line'])
        else:
            monkeypatch.setattr(cli, 'load_ship', lambda args: ship)
            result = cli.cmd_status(SimpleNamespace(line=True, json=False))
            line = result.render(result.data)
        if past_bound_ns > 0:
            assert calls == [True]
            assert f'publication {age_ns // 1_000_000_000}s old, collected here' in line
            assert 'desk: no watch armed' in line and '1 waiting' in line
            assert 'status stale' not in line
        else:
            assert not calls
            assert f'status stale: published {age_ns // 1_000_000_000}s ago' in line
            assert 'desk: no watch armed' not in line
        assert line.startswith('service busy 20s' if busy else 'service ok')
        assert file.read_text() == json.dumps(data), 'a reading never renews the service publication'


@pytest.mark.parametrize('written', [None, 'future'])
def test_an_unknown_write_age_collects_instead_of_reusing_without_a_bound(live, monkeypatch, written):
    ship, svc, lock = live
    file = ship.runtime / presentation.NAME
    data = json.loads(file.read_text())
    now, mono = presentation.time.time_ns(), presentation.time.monotonic_ns()
    data.update(wall=now - 20_000_000_000, monotonic=mono - 20_000_000_000)
    if written is None:
        data.pop('written')
    else:
        data['written'] = now + 1_000_000_000
    file.write_text(json.dumps(data))
    facts = watch.Facts('ok', 1, 0, 0, needs_human=['new-worker'])
    calls = []
    def collect(*args, **kwargs):
        calls.append(True)
        return facts
    monkeypatch.setattr(watch, 'facts', collect)
    assert presentation.command_line(['--ship', str(ship.folder), 'status', '--line']) == facts.line()
    assert calls == [True]


@pytest.mark.parametrize('refresh', [5, 45.5, 300])
@pytest.mark.parametrize('past_bound_ns', [0, 1])
@pytest.mark.parametrize('path_kind', ['bootstrap', 'parser', 'direct'])
def test_plain_age_uses_the_ship_refresh_plus_the_collection_allowance(
        live, monkeypatch, refresh, past_bound_ns, path_kind):
    from quarterdeck import servicectl, ship as shipmod
    ship, svc, lock = live
    ship.file.write_text(ship.file.read_text().replace('[ship]', f'[ship]\nstatus_refresh={refresh}'))
    ship = shipmod.load(ship.folder)
    file = ship.runtime / presentation.NAME
    data = json.loads(file.read_text())
    now, mono = presentation.time.time_ns(), presentation.time.monotonic_ns()
    age = round((refresh + servicectl.STALE_SECONDS) * 1_000_000_000) + past_bound_ns
    data.update(wall=now - age - 20_000_000_000, monotonic=mono - age - 20_000_000_000,
                written=now - age, ship=presentation.fingerprint(ship.file))
    file.write_text(json.dumps(data))
    monkeypatch.setattr(presentation.time, 'time_ns', lambda: now)
    monkeypatch.setattr(presentation.time, 'monotonic_ns', lambda: mono)
    # At the minimum interval both bounds are fifteen seconds; past it the
    # independent collector must win rather than briefly reusing stale text.
    expired = age > round(3 * refresh * 1_000_000_000)
    calls = []
    def collect(*args, **kwargs):
        calls.append(True)
        return watch.Facts('ok', 1, 0, 0, needs_human=['new-worker'])
    monkeypatch.setattr(watch, 'facts', collect if expired else no_collection)
    if path_kind == 'bootstrap':
        line = presentation.command_line(['--ship', str(ship.folder), 'status', '--line'])
    elif path_kind == 'parser':
        monkeypatch.setattr(cli, 'load_ship', lambda args: ship)
        result = cli.cmd_status(SimpleNamespace(line=True, json=False))
        line = result.render(result.data)
    else:
        line = presentation.published_line(ship.folder)
    if expired:
        if path_kind == 'direct':
            assert line is None and not calls
        else:
            assert calls == [True] and 'new-worker needs a human' in line
            assert 'publication 15s old, collected here' in line
    elif past_bound_ns:
        assert f'status stale: published {age // 1_000_000_000}s ago' in line
    else:
        assert line == data['line']


@pytest.mark.parametrize('age_seconds', [90, 91])
def test_an_unreadable_ship_uses_default_bounds_for_the_direct_reader(live, monkeypatch, age_seconds):
    from quarterdeck import ship as shipmod
    ship, svc, lock = live
    file = ship.runtime / presentation.NAME
    data = json.loads(file.read_text())
    now, mono = presentation.time.time_ns(), presentation.time.monotonic_ns()
    data.update(wall=now - 120_000_000_000, monotonic=mono - 120_000_000_000,
                written=now - age_seconds * 1_000_000_000)
    file.write_text(json.dumps(data))
    monkeypatch.setattr(presentation.time, 'time_ns', lambda: now)
    monkeypatch.setattr(presentation.time, 'monotonic_ns', lambda: mono)
    def unreadable(folder):
        raise shipmod.ShipError('synthetic unreadable declaration')
    monkeypatch.setattr(shipmod, 'load', unreadable)
    line, age = presentation.published_reading(ship.folder)
    if age_seconds == 90:
        assert 'status stale: published 90s ago' in line and age is None
    else:
        assert line is None and age == 91
