"""The service's tick is a bounded [ship] key: read from the ship file alone, applied after each pass (law: ship-file)."""

from __future__ import annotations

import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import drill, service as servicemod, servicectl, ship as shipmod
from quarterdeck.ship import ShipError
from tests.conftest import ROOT, TEST_TICK

HEAD = '[ship]\nname = "synthetic"\noperator = "operator"\n'


def load(tmp_path: Path, line: str = '') -> shipmod.Ship:
    folder = tmp_path / 'synthetic'
    folder.mkdir(exist_ok=True)
    (folder / 'ship.toml').write_text(HEAD + line + '\n')
    return shipmod.load(folder)


def test_an_absent_tick_is_the_default_one_second(tmp_path):
    assert load(tmp_path).tick == 1.0 == shipmod.TICK


@pytest.mark.parametrize('written,expected', [('0.05', 0.05), ('5.0', 5.0), ('5', 5.0), ('1', 1.0), ('0.1', 0.1),
                                              ('2.5', 2.5)])
def test_a_tick_within_the_bounds_is_kept_as_seconds(tmp_path, written, expected):
    tick = load(tmp_path, f'tick = {written}').tick
    assert tick == expected and type(tick) is float


@pytest.mark.parametrize('written', ['0.0499', '5.0001', '0', '-1', '6', 'nan', 'inf', '-inf', '1e400',
                                     'true', 'false', '"1.0"', '[1.0]', '{ seconds = 1.0 }',
                                     '1' + '0' * 400])
def test_a_tick_outside_the_bounds_or_not_a_finite_number_is_refused_by_name(tmp_path, written):
    with pytest.raises(ShipError, match=r'ship\.tick must be a number of seconds from 0\.05 to 5; got '):
        load(tmp_path, f'tick = {written}')


@pytest.mark.parametrize('value', [10 ** 400, -10 ** 400, True, None, math.nan, float('inf'), '0.5', b'1'])
def test_the_bound_check_never_converts_or_accepts_what_is_not_a_finite_number(value):
    with pytest.raises(ShipError, match=r'ship\.tick'):
        shipmod._tick(value)


def test_the_longest_tick_keeps_a_running_service_inside_its_stale_age():
    """A heartbeat is written before every duty, and at most one tick passes between passes."""
    assert shipmod.TICK_BOUNDS == (0.05, 5.0)
    assert shipmod.TICK_BOUNDS[1] * 2 <= servicectl.STALE_SECONDS


def test_the_service_has_no_tick_of_its_own_and_no_environment_reaches_it(monkeypatch, tmp_path):
    assert not hasattr(servicemod, 'TICK'), 'the ship file is the only source'
    for name in ('QD_TICK', 'QD_SERVICE_TICK', 'TICK'):
        monkeypatch.setenv(name, '0.05')
    assert load(tmp_path).tick == 1.0


class Stop(Exception):
    pass


def scheduled(monkeypatch, ship, passes, reload=None):
    """Service.run with its passes stood in: returns how long it waited after each pass, by a fake clock."""
    clock = SimpleNamespace(now=0.0)
    starts = []
    acknowledgements = []
    service = servicemod.Service.__new__(servicemod.Service)
    service.ship = ship
    service.runtime = ship.runtime
    service.copy_identity = None
    service.copy_start_token = None
    service.next_status = 0.0
    service.log = lambda *args, **kwargs: None
    service.status_due = lambda: None

    def tick():
        starts.append(clock.now)
        if len(starts) > passes:
            raise Stop
        if reload is not None:
            reload(service, len(starts))

    def sleep(seconds):
        clock.now += seconds

    def acquire_lock(runtime):
        runtime.mkdir(parents=True, exist_ok=True)
        return servicectl.acquire_lock(runtime)

    service.tick = tick
    monkeypatch.setattr(servicemod, 'acquire_lock', acquire_lock)
    monkeypatch.setattr(servicemod.servicectl, 'acknowledge', lambda *args: acknowledgements.append(args))
    monkeypatch.setattr(servicemod.signal, 'signal', lambda *args: None)
    monkeypatch.setattr(servicemod.time, 'monotonic', lambda: clock.now)
    monkeypatch.setattr(servicemod.time, 'sleep', sleep)
    try:
        with pytest.raises(Stop):
            service.run()
    finally:
        service.lock.close()
    return [round(b - a, 9) for a, b in zip(starts, starts[1:])]


def test_the_service_waits_its_ships_tick_after_each_pass(monkeypatch, tmp_path):
    ship = load(tmp_path, 'tick = 0.25')
    assert scheduled(monkeypatch, ship, 4) == [0.25, 0.25, 0.25, 0.25]


def test_a_new_tick_applies_from_the_wait_after_the_pass_that_read_it(monkeypatch, tmp_path):
    slow, quick = load(tmp_path), load(tmp_path, 'tick = 0.05')

    def reload(service, index):
        service.ship = quick if index >= 3 else slow  # the third pass reads the edited file
    assert scheduled(monkeypatch, slow, 4, reload) == [1.0, 1.0, 0.05, 0.05]


def test_a_ship_file_that_stops_loading_keeps_the_last_valid_tick(monkeypatch, tmp_path):
    ship = load(tmp_path, 'tick = 0.2')
    service = servicemod.Service.__new__(servicemod.Service)
    service.ship = ship
    logged = []
    service.log = lambda kind, **fields: logged.append((kind, fields.get('step'), fields.get('error')))
    service.heartbeat = lambda: None
    service.status_due = lambda: None
    from quarterdeck.structure_outcome import Passes
    service.structure_passes = Passes()
    for name in ('reconcile', 'account', 'groups', 'cascades', 'inboxes', 'recoveries', 'refusals', 'deliveries',
                 'follow', 'requests', 'drain_requests', 'compactions', 'fresh_after_order', 'rebrief', 'credentials', 'failover'):
        setattr(service, name, lambda: None)
    ship.file.write_text(HEAD + 'tick = 9\n')
    service.tick()
    assert service.ship is ship and service.ship.tick == 0.2
    assert logged and logged[0][:2] == ('error', 'load') and 'ship.tick' in logged[0][2]
    ship.file.write_text(HEAD + 'tick = 0.5\n')
    service.tick()
    assert service.ship.tick == 0.5


def test_test_ships_and_the_drill_copy_declare_a_short_tick_while_the_tracked_ships_keep_the_default(drydock):
    assert TEST_TICK == drill.DRILL_TICK == 0.1
    assert shipmod.load(drydock).tick == TEST_TICK, 'the conftest drydock copy'
    for tracked in ('drydock', 'default'):
        assert shipmod.load(ROOT / 'ships' / tracked).tick == 1.0, f'ships/{tracked} keeps the default'
