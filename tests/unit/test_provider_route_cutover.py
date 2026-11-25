"""The installed transition and launch evidence for legacy resume, with scratch state only."""

from dataclasses import replace
from datetime import timedelta
import io
import json
import tarfile
from types import SimpleNamespace

import pytest

from quarterdeck import install, provenance, route_cutover
from quarterdeck.harness.resume_target import Unproved
from quarterdeck.seat import Launched, Seat
from quarterdeck.ship import Pool, ShipError
from tests.unit.own_copy_synthetic import fact, inert_copy, snapshot, transaction, vessel


OLD = dict(commit='1' * 40, ref='old', at='2026-01-01T00:00:00+00:00', proof='good',
           instance='1' * 32, package_digest='1' * 64, have_laws=True)
NEW = {**OLD, 'commit': '2' * 40, 'instance': '2' * 32, 'package_digest': '2' * 64}


def activate(ship, monkeypatch):
    from quarterdeck import seat as seatmod

    monkeypatch.setattr(seatmod, 'prepare_program_path', lambda *a, **k: None)
    monkeypatch.setattr(install, 'queue_rebrief', lambda *a, **k: (None, None))
    monkeypatch.setattr(install, 'rebrief_result', lambda *a, **k: 'rebriefed')
    data = {'format': 1, 'current': OLD,
            'pending': {'old_current': OLD, 'was_running': True, 'snapshot': None,
                        'token': 'synthetic', 'route_cutover_mode': 'enroll'}}
    fact = dict(NEW)

    def start(*a, **k):
        assert route_cutover.first(ship) is None  # a staged receipt is not a completed activation
        persisted = install._state(ship.runtime)
        monkeypatch.setattr(install, 'STARTING_ROUTE_ENROLLMENT', persisted['pending'][route_cutover.REFERENCE])
        assert route_cutover.first(ship) == install.datetime.fromisoformat(fact['at'])
        return 'synthetic activated service'

    monkeypatch.setattr(install.servicectl, 'start', start)
    assert install._activate(ship, data, fact, None)[:2] == ['synthetic activated service', 'rebriefed']
    return data, fact


def scratch(tmp_path):
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    return SimpleNamespace(code=None, folder=tmp_path, runtime=runtime)


@pytest.mark.parametrize('force', [False, True])
def test_guard_upgrade_refuses_before_stopping_or_modifying_its_predecessor(transaction, monkeypatch, force):
    tx = transaction
    tx.seed()
    package = tx.ship.runtime / 'venv/package'
    package.mkdir()
    tx.running = True
    monkeypatch.setattr(install, 'package_origin', lambda *args: (package, '1' * 64))
    monkeypatch.setattr(install, 'sync_archive', lambda repo, commit: install.SyncArchive(commit, b'', True))
    before = snapshot(tx.ship.folder)
    with pytest.raises(ShipError) as refused:
        install.upgrade(tx.ship, force=force)
    assert f'qd --ship {tx.ship.folder} sync {tx.commit} {route_cutover.ENROLL}' in str(refused.value)
    assert tx.running and all(call[0] == 'git' for call in tx.calls)
    assert snapshot(tx.ship.folder) == before


@pytest.mark.parametrize(('evidence', 'cause'), [
    ('trusted/retired/record-launches.jsonl', 'retained seat history'),
    ('trusted/retired/launch-price.json', 'retained seat history'),
    ('seats/removed-seat/launch.json', 'retained seat history'),
    ('evidence/seat-move/older-seat', 'retained seat history'),
    ('trusted', 'absence of launch history could not be proved'),
])
def test_virgin_opt_out_requires_known_absence_even_for_retired_or_removed_seats(tmp_path, evidence, cause):
    ship = scratch(tmp_path)
    path = ship.runtime / evidence
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('synthetic evidence')
    with pytest.raises(ShipError, match=cause):
        route_cutover.require_virgin(ship)


def test_once_enrolled_before_service_and_untouched_by_later_copy_state(tmp_path, monkeypatch):
    ship = scratch(tmp_path)
    data, fact = activate(ship, monkeypatch)
    cutoff = route_cutover.first(ship)
    witness = ship.runtime / route_cutover.FILE
    original = witness.read_bytes()
    assert cutoff == install.datetime.fromisoformat(fact['at'])
    for current in (OLD, dict(NEW, at=(cutoff + timedelta(days=1)).isoformat())):
        data['current'] = current
        install._save(ship.runtime, data)
        assert route_cutover.first(ship) == cutoff and witness.read_bytes() == original
    with pytest.raises(ShipError, match='could not be proved once'):
        route_cutover.begin(ship, data, fact, 'enroll')
    # Losing either companion confers no legacy exception and blocks a replay.
    witness.unlink()
    assert route_cutover.first(ship) is None
    with pytest.raises(ShipError, match='incomplete or inconsistent'):
        route_cutover.begin(ship, data, fact, 'enroll')
    witness.write_bytes(original)
    data.pop(route_cutover.REFERENCE)
    install._save(ship.runtime, data)
    assert route_cutover.first(ship) is None
    with pytest.raises(ShipError, match='incomplete or inconsistent'):
        route_cutover.begin(ship, data, fact, 'enroll')


def test_unfinished_enrollment_receipt_remains_partial_after_the_operation_clears(tmp_path):
    ship = scratch(tmp_path)
    state = dict(format=1, current=OLD)
    install._save(ship.runtime, state)
    route_cutover.begin(ship, state, NEW, 'enroll')
    before = (ship.runtime / route_cutover.FILE).read_bytes()
    assert route_cutover.first(ship) is None
    assert 'incomplete' in route_cutover.doctor(ship, state)
    for mode in (None, 'enroll', 'virgin'):
        with pytest.raises(ShipError, match='enrollment is incomplete'):
            route_cutover.check_transition(ship, state, NEW['commit'], True, mode=mode)
    assert (ship.runtime / route_cutover.FILE).read_bytes() == before


@pytest.mark.parametrize('case', ['legacy', 'same-time', 'after-rollback', 'checkout', 'missing-cutover',
                                  'mismatched-cutover', 'zai', 'lost-side', 'mismatched-side'])
def test_legacy_exception_needs_a_pre_cutover_bound_launch(tmp_path, monkeypatch, case):
    ship = scratch(tmp_path)
    activate(ship, monkeypatch)
    cutoff = route_cutover.first(ship)
    trusted = ship.runtime / 'trusted/worker'
    path = trusted / 'launch.json'
    record = trusted / 'synthetic-record'
    pool = Pool('paid', 'synthetic') if case != 'zai' else Pool('paid', 'zai', endpoint='https://synthetic.invalid')
    at = cutoff - timedelta(minutes=2)
    if case == 'same-time':
        at = cutoff
    elif case == 'after-rollback':
        at = cutoff + timedelta(minutes=1)
    route = provenance.provider_route(pool) if case in ('lost-side', 'mismatched-side') else None
    launch = Launched('%1', 123, at.isoformat(), pool.name, None, ['fake'], 'synthetic', 'fake',
                      start='100', provider_route=route)
    launch.write(path)
    if case == 'checkout':
        ship.code = 'checkout'
    if case == 'missing-cutover':
        (ship.runtime / route_cutover.FILE).unlink()
    if case == 'mismatched-cutover':
        (ship.runtime / route_cutover.FILE).write_text('{}')
    if route is not None:
        side = provenance.launch_route_path(trusted, launch)
        if case == 'lost-side':
            side.unlink()
        else:
            evidence = json.loads(side.read_text())
            evidence['identity']['start'] = 'different-process-start'
            side.write_text(json.dumps(evidence))
    seat = SimpleNamespace(ship=ship, trusted=trusted, folder=trusted, name='worker', session=SimpleNamespace(human=False))
    found = Seat.launched(seat)
    assert 'provider_route' not in json.loads(path.read_text())
    provenance.remember_launch(trusted, record, found)
    if case == 'legacy':
        Seat._require_record_route(seat, record, pool, found)
        Seat._require_record_route(seat, record, pool, None)
    else:
        with pytest.raises(Unproved, match='provider route'):
            Seat._require_record_route(seat, record, pool, found)
        with pytest.raises(Unproved, match='provider route'):
            Seat._require_record_route(seat, record, pool, None)


def test_unchanged_launch_identity_keeps_route_across_old_and_new_rewrites(tmp_path):
    path = tmp_path / 'launch.json'
    pool = Pool('paid', 'synthetic')
    route = provenance.provider_route(pool)
    launch = Launched('%1', 123, OLD['at'], pool.name, None, ['fake'], 'synthetic', 'fake',
                      start='100', provider_route=route)
    launch.write(path)
    seat = SimpleNamespace(trusted=tmp_path, folder=tmp_path)
    assert Seat.launched(seat) == launch
    edited = replace(launch, notice='synthetic launch note')
    edited.write(path)
    assert Seat.launched(seat) == edited
    replace(edited, provider_route=None, notice='older writer').write(path)
    assert Seat.launched(seat).provider_route == route
    replace(edited, provider_route=None, pid=456).write(path)
    assert Seat.launched(seat).provider_route is None


def archive_of(*members):
    content = io.BytesIO()
    with tarfile.open(fileobj=content, mode='w') as archive:
        for name, kind in members:
            info = tarfile.TarInfo(name)
            if kind == 'file':
                info.size = len(b'synthetic')
                archive.addfile(info, io.BytesIO(b'synthetic'))
            else:
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
    return content.getvalue()


@pytest.mark.parametrize(('members', 'guarded'), [
    ((('src/quarterdeck/route_cutover.py', 'file'),), True),
    ((('src/quarterdeck/seat.py', 'file'),), False),
    ((('src/quarterdeck/route_cutover.py', 'directory'),), None),
])
def test_target_capability_comes_from_the_archive_it_unpacks(tmp_path, monkeypatch, members, guarded):
    content = archive_of(*members)
    calls = []

    def git(repo, *args):
        calls.append(args)
        return content

    monkeypatch.setattr(install, 'git', git)
    if guarded is None:
        with pytest.raises(ShipError, match='unreadable'):
            install.sync_archive(tmp_path, NEW['commit'])
        return
    archive = install.sync_archive(tmp_path, NEW['commit'])
    assert (archive.commit, archive.content, archive.guarded) == (NEW['commit'], content, guarded)
    assert calls == [('archive', '--format=tar', NEW['commit'])]  # one read of the pinned commit, no listing
    install._extract_archive(archive.content, tmp_path / 'staging')
    assert (tmp_path / 'staging' / members[0][0]).exists()


@pytest.mark.parametrize('older', ['none', 'unguarded', 'guarded', 'guarded-unrecorded', 'unreadable'])
def test_enrollment_reads_an_older_copy_through_its_own_interpreter(tmp_path, monkeypatch, older):
    ship = scratch(tmp_path)
    live, prev = ship.runtime / 'venv', ship.runtime / 'venv.prev'
    inert_copy(live, 'synthetic predecessor')
    packages = {live: live / 'package'}
    if older != 'none':
        inert_copy(prev, 'synthetic older copy')
        packages[prev] = prev / 'package'
    for root, package in packages.items():
        package.mkdir()
        if root == prev and older.startswith('guarded'):
            (package / 'route_cutover.py').write_text('synthetic capability')

    def origin(root, have_laws):
        if root == prev and older == 'unreadable':
            raise ShipError('synthetic unreadable copy')
        return packages[root], '9' * 64

    monkeypatch.setattr(install, 'package_origin', origin)
    recorded = fact('3' * 40) if older != 'guarded-unrecorded' else None
    state = dict(format=1, current=fact(), previous_copy=recorded)
    if older in ('none', 'unguarded'):
        assert route_cutover.check_transition(ship, state, NEW['commit'], True, mode='enroll') is None
        with pytest.raises(ShipError, match='requires enrollment'):
            route_cutover.check_transition(ship, state, NEW['commit'], True)
        return
    match = 'boundary is unknown' if older == 'unreadable' else 'incomplete or inconsistent'
    with pytest.raises(ShipError, match=match):
        route_cutover.check_transition(ship, state, NEW['commit'], True, mode='enroll')
