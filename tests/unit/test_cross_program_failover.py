"""Real lifecycle/record adapters with synthetic processes, walls and conversations."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
import json

import pytest

from quarterdeck import cli, credential, pending, post, quota, record_owner, seat as seatmod, service, standing, watch
from quarterdeck.harness.claude import ClaudeHarness
from quarterdeck.harness.codex import CodexHarness
from quarterdeck.harness.limits import Usage, Window
from quarterdeck.harness.synthetic_record import ClaudeShape, CodexShape
from quarterdeck.harness.types import Screen, State
from quarterdeck.seat import Launched, Seat
from quarterdeck.service import Service
from quarterdeck.ship import Pool, SeatSpec, Session, Ship, ShipError
from quarterdeck.tmux import Pane
from .test_pool_choices import declaration

CHOICES = '["first", {pool="second",harness="claude",model="synthetic-target"}, {pool="third",harness="claude",model="synthetic-third"}]'


def usage(svc, pool, used):
    at = post.now()
    svc.ledger.record_usage(pool, Usage((Window('5h', used, at + timedelta(hours=1)),), at))


@pytest.fixture
def running(tmp_path, monkeypatch, request):
    ship = declaration(tmp_path, CHOICES)
    if getattr(request, 'param', False):
        ship.sessions['crew'] = replace(ship.sessions['crew'], human=True)
    live = {'pane': Pane('%1', 'crew', 'worker', 101, 80, 24, False, False, None)}
    tmux = SimpleNamespace(pane=lambda ident: live['pane'] if live['pane'] and live['pane'].id == ident else None,
                           kill_window=lambda ident, *, expected_pid=None: live.update(pane=None), capture=lambda *a, **k: '')
    seat = Seat(ship, ship.seat('worker'), tmux)
    seat.home.mkdir(parents=True)
    source = CodexShape(seat.home, tmp_path, 'synthetic-source', 'synthetic-source')
    source.begin('old synthetic input', 'synthetic-source', 'high')
    source.end('old synthetic reply')
    Launched('%1', 101, (post.now()-timedelta(minutes=1)).isoformat(), 'first', None,
             ['codex'], 'synthetic', 'codex').write(seat.folder/'launch.json')
    (seat.folder/'record').write_text(str(source.path))
    seat.record()
    launches, created, reseeds = [], [], []
    original_reseed = credential.reseed

    def reseed(ship, seats, **bounds):
        reseeds.extend((s.pool.name, s.harness.name, s.launched().pool) for s in seats if s.launched())
        return original_reseed(ship, seats, **bounds)

    def open_window(argv, env, path):
        launches.append(argv)
        if '--resume' in argv:
            record = created[-1]
        else:
            shape = CodexShape if seat.harness.name == 'codex' else ClaudeShape
            record = shape(seat.home, tmp_path, f'synthetic-target-{len(launches)}', seat.spec.model)
            created.append(record)
        record.begin('new synthetic input', seat.spec.model, seat.spec.effort or 'none')
        record.end('new synthetic reply')
        live['pane'] = Pane(f'%{len(launches)+1}', 'crew', 'worker', 101+len(launches), 80, 24, False, False, None)
        return live['pane']

    monkeypatch.setattr(credential, 'reseed', reseed)
    monkeypatch.setattr(seat, '_open_window', open_window)
    monkeypatch.setattr(seat, '_survives', lambda *a: None)
    monkeypatch.setattr(seatmod.shutil, 'which', lambda *a, **k: '/usr/bin/sh')
    for adapter in (CodexHarness, ClaudeHarness):
        monkeypatch.setattr(adapter, 'version', lambda self: 'synthetic')
        monkeypatch.setattr(adapter, 'screen', lambda *a: Screen('', ''))
        monkeypatch.setattr(adapter, 'usage', lambda *a: pytest.fail('test queried a real provider'))
    monkeypatch.setattr(service, 'seats', lambda *a: [seat])
    monkeypatch.setattr(service.machine, 'processes', lambda: {})
    monkeypatch.setattr(quota, 'refresh', lambda *a: None)
    monkeypatch.setattr(quota, 'refresh_declared', lambda *a: None)
    svc = Service(ship, tmux)
    usage(svc, 'first', 1.0)
    usage(svc, 'second', 0.0)
    usage(svc, 'third', 0.0)
    yield SimpleNamespace(seat=seat, svc=svc, source=source, launches=launches, created=created, reseeds=reseeds)
    svc.ledger.close()


def test_actual_cross_program_move_is_fresh_carries_context_and_keeps_readers(running):
    r = running
    message = r.svc.store.new('order', 'issuer', 'worker', 'complete synthetic open work')
    attempt = r.svc.attempts.open(message, 'worker', '%1', 101, r.seat.launched().at)
    r.svc.attempts.close(attempt, 'blind', 'old submission unconfirmed')
    before = r.source.path.read_bytes()
    r.svc.account()
    r.svc.failover()
    assert r.seat.pool.name == 'second' and r.seat.harness.name == 'claude'
    assert r.seat.launched().harness == 'claude' and r.seat.launched().resume is None
    assert '--resume' not in r.launches[0]
    assert message.id in r.launches[0][-1] and f'qd thread {message.id}' in r.launches[0][-1]
    assert message.body not in r.launches[0][-1]
    assert r.seat.spec.model == 'synthetic-target' and r.seat.spec.settings == {} and r.seat.spec.effort is None
    assert r.reseeds[0] == ('first', 'codex', 'first')
    assert r.svc.attempts.latest()[message.id] == 'launch'
    assert r.svc.attempts.for_message(message.id)[0].result == 'blind', 'old submission was never confirmed or erased'
    assert r.source.path.read_bytes() == before and r.seat.record() == r.created[0].path
    assert {item.path: item.reader.name if item.reader else None for item in record_owner.records(r.seat)} == {
        r.source.path: 'codex', r.created[0].path: 'claude'}
    r.svc.account()
    assert set(r.svc.ledger.db.execute('SELECT pool FROM turns')) == {('first',), ('second',)}
    assert watch.reading(r.seat).pool == 'second' and watch.reading(r.seat).harness == 'claude'
    assert 'worker on claude/second' in watch.facts(r.seat.ship, r.seat.tmux, walls=[], login=[]).line()
    restored = Seat(r.seat.ship, r.seat.ship.seat('worker'), r.seat.tmux)
    assert restored.pool.name == 'second' and restored.harness.name == 'claude'
    assert '# Program: Claude Code' in standing.brief(r.seat.ship, r.seat.ship.seat('worker')).text
    r.svc.rebrief()
    assert '# Program: Claude Code' in (r.seat.ship.runtime/'brief/worker.md').read_text()
    launched = r.seat.launched()
    usage(r.svc, 'first', 0.0)
    for _ in range(3):
        r.svc.failover()
    assert r.seat.launched() == launched and len(r.launches) == 1
    usage(r.svc, 'second', 1.0)
    r.svc.failover()
    assert r.seat.pool.name == 'third' and r.seat.launched().resume is not None
    assert '--resume' in r.launches[-1] and r.seat.record() == r.created[0].path


def test_all_candidate_walls_hold_without_relaunch(running):
    r = running
    for pool in ('second', 'third'):
        usage(r.svc, pool, 1.0)
    before = r.seat.launched()
    for _ in range(3):
        r.svc.failover()
    assert r.seat.launched() == before and not r.launches
    line = watch.facts(r.seat.ship, r.seat.tmux, login=[]).line()
    assert 'worker held at limit' in line
    assert all(pool not in line for pool in ('first', 'second', 'third'))


@pytest.mark.parametrize('later_known', [False, True])
def test_unknown_destination_is_skipped_without_inventing_capacity(running, later_known):
    r = running
    r.svc.ledger.record_usage('second', Usage((), post.now(), 'synthetic unavailable'))
    if not later_known:
        r.svc.ledger.record_usage('third', Usage((), post.now(), 'synthetic unavailable'))
    r.svc.failover()
    if later_known:
        assert r.seat.pool.name == 'third' and len(r.launches) == 1
    else:
        assert r.seat.pool.name == 'first' and not r.launches
        assert not r.svc.ledger.usage_known('second') and not r.svc.ledger.usage_known('third')
        assert any(not row.get('current') for row in watch.headline_readings(r.seat.ship) if row['pool'] == 'second')


def test_unknown_or_missing_saved_selection_does_not_choose_the_first_program(running):
    r = running
    r.svc.failover()
    launched = r.seat.launched()
    replace(launched, pool='first').write(r.seat.folder/'launch.json')
    with pytest.raises(ShipError, match='saved launch and selected program disagree'):
        Seat(r.seat.ship, r.seat.ship.seat('worker'), r.seat.tmux)
    launched.write(r.seat.folder/'launch.json')
    (r.seat.folder/'pool').write_text('unknown-choice\n')
    with pytest.raises(ShipError, match='no declared choice'):
        Seat(r.seat.ship, r.seat.ship.seat('worker'), r.seat.tmux)
    (r.seat.folder/'pool').unlink()
    restored = Seat(r.seat.ship, r.seat.ship.seat('worker'), r.seat.tmux)
    assert restored.harness.name == 'claude', 'saved launch is evidence, not a primary-program guess'
    (r.seat.folder/'launch.json').unlink()
    with pytest.raises(ShipError, match='saved pool missing'):
        Seat(r.seat.ship, r.seat.ship.seat('worker'), r.seat.tmux)


def test_unconfirmed_closed_submission_settles_absence_on_its_original_identity(running):
    r = running
    message = r.svc.store.new('order', 'issuer', 'worker', 'unconfirmed synthetic transport')
    attempt = r.svc.attempts.open(message, 'worker', '%1', 101, r.seat.launched().at)
    proof = pending.Proof.begin(r.seat, attempt, post.typed([message]))
    proof.save(phase='submitted', shown=post.typed([message]))
    r.svc.attempts.close(attempt, 'pending')
    r.svc.store.new('report', 'worker', 'issuer', 'closed by the worker', re=message.id)
    original = dict(proof.data)
    r.svc.failover()
    r.svc.reconcile()
    settled = json.loads(proof.path.read_text())
    assert settled['phase'] == 'not-in-closed-record' and not proof.same_launch(r.seat)
    assert 'not in its closed launch record' in settled['resolution']
    assert {key: settled[key] for key in original if key != 'phase'} == {
        key: value for key, value in original.items() if key != 'phase'}
    attempts = r.svc.attempts.for_message(message.id)
    assert len(attempts) == 1 and attempts[0].path == attempt.path
    assert attempts[0].result == 'not-in-closed-record'
    assert (attempts[0].at, attempts[0].pane, attempts[0].pid, attempts[0].launched_at) == (
        attempt.at, attempt.pane, attempt.pid, attempt.launched_at)
    before = (proof.path.read_bytes(), proof.path.stat().st_mtime_ns,
              attempt.path.read_bytes(), attempt.path.stat().st_mtime_ns)
    r.svc.reconcile()
    assert before == (proof.path.read_bytes(), proof.path.stat().st_mtime_ns,
                      attempt.path.read_bytes(), attempt.path.stat().st_mtime_ns)
    assert r.svc.attempts.for_message(message.id) == attempts
    assert r.svc.attempts.latest()[message.id] == 'not-in-closed-record'
    assert '--first' not in r.launches[0], 'a closed uncertain submission was not resent'
    assert not r.svc.store.is_open(message) and not pending.detached(r.seat)
    facts = watch.facts(r.seat.ship, r.seat.tmux, walls=[], login=[])
    assert 'worker' not in facts.needs_human and not facts.pending


def test_closed_submission_with_skipped_old_record_stays_held_on_its_original_identity(running):
    r = running
    message = r.svc.store.new('order', 'issuer', 'worker', 'unconfirmed synthetic transport')
    attempt = r.svc.attempts.open(message, 'worker', '%1', 101, r.seat.launched().at)
    proof = pending.Proof.begin(r.seat, attempt, post.typed([message]))
    proof.save(phase='submitted', shown=post.typed([message]))
    r.svc.attempts.close(attempt, 'pending')
    r.svc.store.new('report', 'worker', 'issuer', 'closed by the worker', re=message.id)
    original = proof.path.read_bytes()
    attempts = r.svc.attempts.for_message(message.id)
    r.svc.failover()
    # The same closed launch is now unreadable as complete input evidence.
    with r.source.path.open('a') as stream:
        stream.write(json.dumps(dict(type='unknown-input-kind', timestamp=post.now().isoformat())) + '\n')
    r.svc.reconcile()
    r.svc.reconcile()
    assert proof.path.read_bytes() == original and not proof.same_launch(r.seat)
    assert r.svc.attempts.for_message(message.id) == attempts and len(attempts) == 1
    assert r.svc.attempts.latest()[message.id] == 'pending'
    assert '--first' not in r.launches[0], 'a closed uncertain submission was not resent'
    assert not r.svc.store.is_open(message) and pending.detached(r.seat)
    facts = watch.facts(r.seat.ship, r.seat.tmux, walls=[], login=[])
    assert 'worker' in facts.needs_human and facts.pending


def test_failed_destination_launch_keeps_its_selected_program_without_a_retry_loop(running, monkeypatch):
    r = running
    with monkeypatch.context() as patch:
        def fail(*args):
            raise OSError('synthetic launch failure')
        patch.setattr(r.seat, '_open_window', fail)
        with pytest.raises(OSError):
            r.svc.failover()
    restored = Seat(r.seat.ship, r.seat.ship.seat('worker'), r.seat.tmux)
    assert restored.pool.name == 'second' and restored.harness.name == 'claude'
    for _ in range(3):
        r.svc.failover()
    assert not r.launches and restored.launched() is None
    r.seat.up(resume=True)
    assert r.seat.launched().resume is None and r.seat.launched().harness == 'claude'


@pytest.mark.parametrize('evidence', ['terminal', 'working', 'unknown', 'queued', 'unreadable', 'missing'])
def test_legacy_pool_list_also_requires_record_proof_at_the_move_boundary(tmp_path, monkeypatch, evidence):
    spec = SeatSpec('worker', 'crew', 'worker', 'fake', ('one', 'two'))
    ship = Ship(tmp_path, 'synthetic', 'operator', tmp_path,
                {name: Pool(name, 'fake') for name in spec.pools}, {'crew': Session('crew', 'crew')}, {'worker': spec})
    pane = Pane('%1', 'crew', 'worker', 101, 80, 24, False, False, None)
    tmux = SimpleNamespace(pane=lambda ident: pane, capture=lambda *a, **k: '')
    seat = Seat(ship, spec, tmux)
    seat.home.mkdir(parents=True)
    shape = CodexShape(seat.home, tmp_path, 'synthetic-terminal', 'synthetic-model')
    shape.begin('synthetic input', 'synthetic-model', 'high')
    shape.end('synthetic reply')
    record = shape.path
    Launched('%1', 101, post.now().isoformat(), 'one', None, ['fake'], 'synthetic', 'fake').write(seat.folder/'launch.json')
    (seat.folder/'record').write_text(str(record))
    record_owner.register(seat.folder, record, 'fake')
    calls = []

    def move(target=None):
        calls.append(target)
        if target:
            seat.move_to(target)
        return 'synthetic movement'

    monkeypatch.setattr(seat, 'restart', move)
    monkeypatch.setattr(seat, 'failover_to', move, raising=False)
    monkeypatch.setattr(seat.harness, 'screen', lambda *a: Screen('', ''))
    if evidence == 'missing':
        record.unlink()
    elif evidence == 'unreadable':
        def unreadable(path):
            raise OSError('synthetic unreadable record')
        monkeypatch.setattr(seat.harness, 'state', unreadable)
    else:
        state = replace(seat.harness.state(record),
                        turn_open=True if evidence == 'working' else None if evidence == 'unknown' else False,
                        queued=('queued input',) if evidence == 'queued' else ())
        monkeypatch.setattr(seat.harness, 'state', lambda path: state)
    monkeypatch.setattr(service, 'seats', lambda *a: [seat])
    svc = Service(ship, tmux)
    try:
        usage(svc, 'one', 1.0)
        svc.failover()
        assert bool(calls) == (evidence == 'terminal')
        assert seat.pool.name == ('two' if evidence == 'terminal' else 'one')
    finally:
        svc.ledger.close()


def test_shared_state_join_checks_a_siblings_selected_program(tmp_path, monkeypatch):
    from quarterdeck.harness.antigravity import AntigravityHarness
    from .test_harness_switch import setup_seat

    seat, _, _ = setup_seat(tmp_path, monkeypatch)
    (seat.folder/'record').unlink()
    neighbor = replace(seat.ship.seats['neighbor'], pools=('codex', 'google'), pool_choices={
        'codex': dict(harness='codex', model='synthetic', effort=None, settings={}),
        'google': dict(harness='antigravity', model='synthetic', effort=None, settings={})})
    seat.ship.seats['neighbor'] = neighbor
    sibling = Seat(seat.ship, neighbor, seat.tmux)
    sibling.move_to('google')
    replace(seat.launched(), pane='%2', pid=45678).write(sibling.folder/'launch.json')
    seat.tmux.pane = lambda ident: Pane(ident, 'crew', 'synthetic', 45678 if ident == '%2' else 12345,
                                      80, 24, False, False, None)
    def no_scan(*args):
        pytest.fail('ambiguous shared record candidates were enumerated')
    monkeypatch.setattr(AntigravityHarness, '_appeared', no_scan)
    assert seat.join().record is None and seat.join().reason.startswith('ambiguous:')


@pytest.mark.parametrize('change', ['model=other-model', 'effort=low', 'settings.synthetic=1', 'settings.up=false', 'synthetic=1', 'harness=codex', 'adopt'])
def test_inline_program_controls_cannot_edit_the_previous_program_defaults(running, monkeypatch, change):
    r = running
    r.svc.failover()
    before = r.seat.ship.file.read_bytes()
    applied = []
    monkeypatch.setattr(cli, 'load_ship', lambda args: r.seat.ship)
    monkeypatch.setattr(cli, 'seat_of', lambda *args: r.seat)
    monkeypatch.setattr(cli, 'apply_control', lambda *args: applied.append(args))
    monkeypatch.setattr(watch, 'running_settings', lambda *args: pytest.fail('refusal must precede program readings'))
    args = SimpleNamespace(seat='worker', adopt=change == 'adopt', changes=[] if change == 'adopt' else ['up=false', change])
    with pytest.raises(ShipError, match='inline pool choices'):
        cli.cmd_set(args)
    assert r.seat.ship.file.read_bytes() == before and not applied


@pytest.mark.parametrize('name', ['model', 'effort', 'harness', 'synthetic'])
def test_old_queued_program_control_is_refused_after_switch_to_inline_choice(running, monkeypatch, name):
    r = running
    r.svc.failover()
    before = r.seat.ship.file.read_bytes()
    monkeypatch.setattr(r.seat.harness, 'control', lambda *args: pytest.fail('refusal must precede adapter controls'))
    monkeypatch.setattr(r.svc, 'seat', lambda *args: r.seat)
    answer = r.svc.answer(dict(kind='control', seat='worker', name=name, value='other-program-value'))
    assert 'inline pool choices' in answer and 'not applied' in answer
    assert 'ship.toml' in answer
    assert r.seat.ship.file.read_bytes() == before


def test_common_seat_controls_remain_available_with_inline_choices(running, monkeypatch):
    r = running
    r.svc.failover()
    monkeypatch.setattr(cli, 'load_ship', lambda args: r.seat.ship)
    monkeypatch.setattr(cli, 'seat_of', lambda *args: r.seat)
    proofs = cli.cmd_set(SimpleNamespace(seat='worker', adopt=False, changes=['up=false']))
    assert proofs[-1] == '  up written'
    assert not service.shipmod.load(r.seat.ship.folder).seat('worker').up
    assert r.svc.control(r.seat, 'pool', 'third') == 'pool written; qd restart worker applies it'
