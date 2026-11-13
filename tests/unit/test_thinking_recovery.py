"""Real-reader and Door recovery: one continuation, durable interruption, safe restoration."""

from dataclasses import replace
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from quarterdeck import endpoint, pending, post, record_owner, recovery, watch
from quarterdeck.door import Door
from quarterdeck.harness.opencode import OpenCodeHarness
from quarterdeck.ledger import Ledger
from quarterdeck.seat import Launched, Seat
from quarterdeck.service import Service
from quarterdeck.ship import Pool, SeatSpec, Session, Ship, ShipError
from tests.unit.test_opencode_record import AT, MS, database, message, part
from tests.unit.test_thinking_generation import append_auto_cap, append_compaction, capped


class Native:
    """Owned fake transport; native SQLite rows still pass through the actual reader/receipt path."""
    def __init__(self, seat, record):
        self.seat, self.record = seat, record
        self.selected, self.box, self.working = 'default', '', False
        self.calls, self.inputs = [], []
        self.auto_complete, self.failed_recovery, self.fault = True, False, None
        self.live = SimpleNamespace(id='%1', pid=12345, width=120, dead=False, in_mode=False)

    def pane(self, ident):
        return self.live if self.live is not None and ident == self.live.id else None

    def capture(self, *args, **kwargs):
        suffix = '' if self.selected == 'default' else ' · qd-recovery-off'
        return '\n'.join(['  ┃', *['  ┃  ' + line for line in self.box.split('\n')], '  ┃',
            '  ┃  Build auto · qwen3.8-27b synthetic' + suffix, '  ╹' + '▀' * 115,
            'esc interrupt' if self.working else '/synthetic']) + '\n'

    def paste(self, ident, text, **kwargs):
        self.calls.append(('paste', text))
        self.box = text

    def keys(self, ident, *keys):
        self.calls.append(('keys', keys))
        if keys == ('C-t',):
            restoring = self.selected != 'default'
            if self.fault == ('restore-before' if restoring else 'off-before'):
                raise OSError('synthetic interrupted control')
            self.selected = 'default' if restoring else 'qd-recovery-off'
            path = self.seat.home / 'state/opencode/model.json'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({'variant': {'synthetic/qwen3.8-27b': self.selected}}))
            if self.fault == ('restore-after' if restoring else 'off-after'):
                raise OSError('synthetic interrupted control after native change')
        elif keys == ('Enter',):
            if self.fault == 'submit-before':
                raise OSError('synthetic submit refused before native acceptance')
            self.submit()
            if self.fault == 'submit-after':
                raise OSError('synthetic submit interrupted after native acceptance')

    def submit(self):
        assert not self.inputs, 'a second continuation must never be submitted'
        self.inputs.append(self.box)
        text, self.box = self.box, ''
        self.working = not self.auto_complete
        con = database_connection(self.record)
        message(con, 'recovery-input', 'root', 10000, {'role': 'user', 'agent': 'build',
            'time': {'created': MS + 10000}, 'model': {'modelID': 'qwen3.8-27b', 'providerID': 'synthetic',
                                                        'variant': 'qd-recovery-off'}})
        part(con, 'recovery-input-text', 'recovery-input', 'root', 10000, {'type': 'text', 'text': text})
        data = {'role': 'assistant', 'parentID': 'recovery-input', 'agent': 'build',
            'modelID': 'qwen3.8-27b', 'providerID': 'synthetic', 'variant': 'qd-recovery-off',
            'time': {'created': MS + 10010}, 'tokens': {'total': 25, 'input': 20, 'output': 5, 'reasoning': 0}}
        if self.auto_complete:
            data['time']['completed'] = MS + 10020
            data['finish'] = 'length' if self.failed_recovery else 'stop'
        message(con, 'recovery-answer', 'root', 10010, data)
        if not self.failed_recovery:
            part(con, 'recovery-text', 'recovery-answer', 'root', 10015, {'type': 'text', 'text': 'Synthetic final answer'})
        con.commit()
        con.close()

    def kill_window(self, ident, *, expected_pid=None):
        self.calls.append(('down', ident))
        self.live = None


def database_connection(path):
    import sqlite3
    return sqlite3.connect(path)


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(endpoint, 'models', lambda *args, **kw: [endpoint.Served('qwen3.8-27b', 98304)])
    monkeypatch.setattr(post, 'now', lambda: AT + timedelta(seconds=1))
    pool = Pool('synthetic', 'local', endpoint='http://example.invalid/v1')
    spec = SeatSpec('worker', 'crew', 'worker', 'opencode', ('synthetic',), model='qwen3.8-27b',
        settings={'provider': {'synthetic': {'models': {'qwen3.8-27b': {
            'options': {'chat_template_kwargs': {'enable_thinking': True, 'preserve_thinking': True}}}}}}})
    ship = Ship(tmp_path, 'synthetic', 'operator', tmp_path, {'synthetic': pool},
                {'crew': Session('crew', 'Crew')}, {'worker': spec})
    seat = Seat(ship, spec, None)
    seat.harness.build_home(seat.home, seat.spec, pool, None)
    launch = seat.harness.launch(seat.spec, pool, seat.home, tmp_path / 'brief', None, None)
    Launched('%1', 12345, AT.isoformat(), pool.name, None, list(launch.argv), '1.19.5', 'opencode',
             recovery=launch.recovery).write(seat.folder / 'launch.json')
    record = seat.home / 'data-001/opencode/opencode.db'
    con = database(record)
    capped(con)
    con.close()
    (seat.folder / 'record').write_text(str(record))
    record_owner.register(seat.folder, record, 'opencode')
    native = Native(seat, record)
    seat.tmux = native
    store, attempts = post.Store(ship.runtime), post.Attempts(ship.runtime)
    order = post.Message('ord-20300101-0000-captain-abcd', 'order', 'captain', 'worker', AT,
                         'Synthetic accepted work')
    store.write(order)
    store.write(post.Message('ack-20300101-0000-worker-abcd', 'ack', 'worker', 'captain',
                            AT + timedelta(milliseconds=15), 'Started', re=order.id))
    events = []
    service = SimpleNamespace(store=store, attempts=attempts, ship=ship,
        resolved_seats=lambda: [seat], log=lambda event, **fields: events.append((event, fields)))
    return SimpleNamespace(seat=seat, native=native, record=record, service=service, order=order, events=events)


def journal(world):
    proofs, errors = pending.journals(world.seat)
    assert not errors and len(proofs) == 1
    return proofs[0]


def test_one_continuation_retains_original_tools_receipts_order_and_accounting(world):
    recovery.run(world.service)
    proof = journal(world)
    assert proof.data['phase'] == 'delivered' and proof.data['recovery']['mode'] == 'done'
    assert proof.data['recovery']['recovery_input'] == 'recovery-input'
    assert len(world.native.inputs) == 1 and world.native.selected == 'default'
    assert [c for c in world.native.calls if c[0] == 'keys'] == [
        ('keys', ('C-t',)), ('keys', ('Enter',)), ('keys', ('C-t',))]
    assert world.service.store.is_open(world.order), 'a ship continuation report cannot close the worker\'s order'
    state, turns = world.seat.harness.read(world.record)
    assert len(turns) == len(state.completed_turns) == 2
    assert turns[0].input == 'Synthetic accepted work' and turns[0].tools[0].name == 'bash'
    assert turns[1].input == proof.data['payload'] and turns[1].reply == 'Synthetic final answer'
    book = Ledger(world.seat.ship.runtime / 'ledger.sqlite')
    book.ingest('worker', 'synthetic', world.record, turns, world.service.store)
    book.ingest('worker', 'synthetic', world.record, turns, world.service.store)
    assert book.db.execute('SELECT COUNT(*) FROM turns').fetchone() == (2,)
    book.close()
    recovery.run(world.service)  # new service duty invocation is not a new allowance
    assert len(world.native.inputs) == 1 and journal(world).data['recovery']['mode'] == 'done'


def test_native_trimmed_envelope_uses_the_unchanged_receipt_rule_for_its_identity(world):
    original = world.native.submit
    def trimmed():
        world.native.box = world.native.box.rstrip('\n')
        original()
    world.native.submit = trimmed
    recovery.run(world.service)
    proof = journal(world)
    assert world.native.inputs == [proof.data['payload'].rstrip('\n')]
    assert proof.data['phase'] == 'delivered' and proof.data['recovery']['mode'] == 'done'
    assert proof.data['recovery']['recovery_input'] == 'recovery-input'


def test_shared_receipt_comparison_does_not_accept_partial_or_edited_input():
    from quarterdeck.input_receipt import same_text
    assert same_text('payload\n', 'payload')
    assert not same_text('pay load', 'payload')
    assert not same_text('payload truncated', 'payload')


@pytest.mark.parametrize('stage', ['before-post', 'after-post'])
def test_publication_interruption_keeps_a_visible_once_reservation_without_keys(world, monkeypatch, stage):
    if stage == 'before-post':
        original = world.service.store.write
        monkeypatch.setattr(world.service.store, 'write', lambda notice: (_ for _ in ()).throw(RuntimeError('crash before post')))
    else:
        original = recovery._advance
        monkeypatch.setattr(recovery, '_advance', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('crash after post')))
    with pytest.raises(RuntimeError):
        recovery.run(world.service)
    proof = journal(world)
    assert proof.data['recovery']['mode'] == 'claimed' and not world.native.calls
    assert 'thinking recovery mode unresolved' in Door(world.seat).reading().reason
    if stage == 'before-post':
        assert 'post missing' in Door(world.seat).reading().reason
        monkeypatch.setattr(world.service.store, 'write', original)
    else:
        monkeypatch.setattr(recovery, '_advance', original)
    recovery.run(world.service)
    assert journal(world).data['recovery']['mode'] == 'held' and not world.native.calls
    assert world.service.store.is_open(world.order)


@pytest.mark.parametrize('fault', ['off-before', 'off-after', 'submit-before', 'submit-after', 'restore-before', 'restore-after'])
def test_interrupted_control_or_submit_is_never_replayed(world, fault):
    world.native.fault = fault
    recovery.run(world.service)
    first = list(world.native.calls)
    world.native.fault = None
    recovery.run(world.service)
    proof = journal(world)
    if fault in ('submit-after', 'restore-after'):
        assert proof.data['recovery']['mode'] == 'done'
        assert len(world.native.inputs) == 1
    else:
        assert recovery.held(world.seat)
    assert sum(c == ('keys', ('Enter',)) for c in world.native.calls) <= 1
    if fault.startswith('off') or fault.startswith('restore') or fault == 'submit-before':
        assert world.native.calls == first, 'an uncertain control/paste/submit must never be repeated'


def test_receipt_before_restore_holds_late_work_fresh_failover_controls_and_rebrief(world, monkeypatch):
    original = recovery._restore
    monkeypatch.setattr(recovery, '_restore', lambda *a: (_ for _ in ()).throw(RuntimeError('receipt before restore')))
    with pytest.raises(RuntimeError):
        recovery.run(world.service)
    assert journal(world).data['phase'] == 'delivered'
    world.service.store.write(post.Message('rep-20300101-0000-worker-cdef', 'report', 'worker', 'captain',
        AT + timedelta(milliseconds=10015), 'Completed', re=world.order.id))
    world.seat.spec = replace(world.seat.spec, fresh='after-order')
    monkeypatch.setattr(world.seat, 'fresh', lambda: pytest.fail('after-order fresh must remain held'))
    Service.fresh_after_order(world.service)  # otherwise the recorded closing turn would qualify
    late = world.service.store.new('order', 'captain', 'worker', 'Later accepted work must wait')
    assert Door(world.seat).deliver(post.typed([late])).result == 'withheld'
    assert Service.control(world.service, world.seat, 'effort', 'high').startswith('effort held:')
    for action in (lambda: world.seat.up(), world.seat.restart, lambda: Seat.fresh(world.seat),
                   lambda: world.seat.failover_to('synthetic')):
        with pytest.raises(ShipError, match='thinking recovery mode unresolved'):
            action()
    assert Service.relaunch(world.service, world.seat, False) is None
    world.seat.spec = replace(world.seat.spec, pools=('synthetic', 'other'))
    Service.failover(world.service)  # must hold before even querying a destination/ledger
    assert len(world.native.inputs) == 1
    # A rebrief uses ordinary Door delivery; it cannot type a first/new input in off mode.
    assert Door(world.seat).deliver('New brief notification').result == 'withheld'
    monkeypatch.setattr(recovery, '_restore', original)
    recovery.run(world.service)
    assert journal(world).data['recovery']['mode'] == 'done'


def test_failed_recovery_keeps_both_turns_and_never_loops(world):
    world.native.failed_recovery = True
    recovery.run(world.service)
    assert journal(world).data['recovery']['mode'] == 'done'
    assert len(world.seat.harness.read(world.record)[1]) == 2
    recovery.run(world.service)
    assert len(world.native.inputs) == 1


def test_native_compaction_after_recovery_cannot_claim_another_allowance(world):
    recovery.run(world.service)
    con = database_connection(world.record)
    append_compaction(con)
    append_auto_cap(con)
    con.close()
    found = world.seat.harness.state(world.record)
    assert found.capped is not None and found.capped.work == 'recovery-input'
    recovery.run(world.service)
    assert len(world.native.inputs) == 1 and len(pending.journals(world.seat)[0]) == 1


def test_running_recovery_is_a_quiet_hold_but_unknown_execution_stays_honest(world):
    world.native.auto_complete = False
    recovery.run(world.service)
    assert journal(world).data['recovery']['recovery_input'] == 'recovery-input'
    reading = Door(world.seat).reading()
    assert not reading.open and reading.waiting and reading.clears != 'needs a human'
    assert world.native.selected == 'qd-recovery-off'
    assert [c for c in world.native.calls if c == ('keys', ('C-t',))] == [('keys', ('C-t',))]
    con = database_connection(world.record)
    part(con, 'unknown', 'recovery-answer', 'root', 10016, {'type': 'future_tool_call', 'name': 'bash'})
    con.commit()
    con.close()
    recovery.run(world.service)
    assert Door(world.seat).reading().clears == 'needs a human'
    assert len(world.native.inputs) == 1 and world.native.selected == 'qd-recovery-off'


@pytest.mark.parametrize('route', ['status', 'service'])
def test_redraw_alarm_paths_preserve_quiet_and_unknown_recovery_holds(world, monkeypatch, route):
    world.native.auto_complete = False
    recovery.run(world.service)
    seat, svc = world.seat, world.service
    svc.told, svc.alarmed = {}, {}
    svc.alarm_recipients = lambda seat, reason: Service.alarm_recipients(svc, seat, reason)
    svc.ship.seats['desk'] = SeatSpec('desk', 'crew', 'desk', 'opencode', ('synthetic',))
    monkeypatch.setattr(watch, 'seats', lambda *_: [seat])
    capture = world.native.capture
    captures = []
    def redrawing(*args, **kwargs):
        captures.append(1)
        return 'partial unknown redraw' if len(captures) == 1 else capture(*args, **kwargs)
    def alarm():
        captures.clear()
        if route == 'status':
            return seat.name in watch.facts(svc.ship, seat.tmux, walls=[], login=[]).needs_human
        Service.needs_human(svc, seat)
        return bool([m for m in svc.store.for_seat('desk') if m.sender == 'ship'])
    monkeypatch.setattr(world.native, 'capture', redrawing)
    assert not alarm()
    assert len(captures) == 1, 'a quiet recovery hold does not become an unknown-screen alarm'
    assert Door(seat).reading().waiting and not Door(seat).reading().open
    assert Door(seat).deliver('Late work must remain held').result == 'withheld'
    con = database_connection(world.record)
    part(con, 'unknown', 'recovery-answer', 'root', 10016, {'type': 'future_tool_call', 'name': 'bash'})
    con.commit()
    con.close()
    recovery.run(svc)
    calls = list(world.native.calls)
    assert alarm()
    assert len(captures) == 1, 'unknown recovery evidence is immediate even during a changed redraw'
    reading, reason = Door(seat).human_obstruction()
    assert not reading.open and reading.clears == 'needs a human'
    assert reason and 'mode unresolved' in reason
    assert Door(seat).deliver('Still no new work').result == 'withheld'
    assert world.native.calls == calls and len(world.native.inputs) == 1


def forbid_human_content_and_input(world, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('human mode alarms/refusals use only owned journal metadata, never conversation or input')
    for obj, name in ((world.seat, 'record'), (world.seat.harness, 'state'),
                      (world.seat.harness, 'inputs'), (world.seat.harness, 'screen'),
                      (world.native, 'capture'), (world.native, 'paste'), (world.native, 'keys')):
        monkeypatch.setattr(obj, name, forbidden)


def from_status(seat):
    from quarterdeck import human_wake
    return human_wake.status(seat)


def wake_pass_reads_only(world, guard):
    """Under the waking law, only the wake pass of an up, unarmed, unheld human seat with a waiting message
    reads its record (state, and the prior-receipt inputs read, by the captain's ruling) and its screen. Nothing
    types into it, and status, alarms and refusals stay metadata-only. Returns the names the wake pass read."""
    from quarterdeck import human_wake
    inside, reads = [False], []

    def forbidden(*args, **kwargs):
        pytest.fail('nothing types into a human seat here')

    def allowed(name, real):
        def read(*args, **kwargs):
            if not inside[0]:
                pytest.fail(f'{name}: human mode alarms/refusals use only owned journal metadata, never conversation')
            reads.append(name)
            return real(*args, **kwargs)
        return read
    for obj, name in ((world.seat, 'record'), (world.seat.harness, 'state'), (world.seat.harness, 'inputs'),
                      (world.seat.harness, 'screen'), (world.native, 'capture')):
        guard.setattr(obj, name, allowed(name, getattr(obj, name)))
    for obj, name in ((world.native, 'paste'), (world.native, 'keys')):
        guard.setattr(obj, name, forbidden)
    real_run = human_wake.run

    def run(seat):
        inside[0] = True
        try:
            return real_run(seat)
        finally:
            inside[0] = False
    guard.setattr(human_wake, 'run', run)
    return reads


def human_ownership_transition(world, monkeypatch, *, unknown):
    seat, svc = world.seat, world.service
    svc.told, svc.alarmed = {}, {}
    svc.alarm_recipients = lambda seat, reason: Service.alarm_recipients(svc, seat, reason)
    svc.needs_human = lambda seat: Service.needs_human(svc, seat)
    svc.ship.sessions['office'] = Session('office', 'Office', human=True)
    svc.ship.seats['desk'] = SeatSpec('desk', 'office', 'desk', 'opencode', ('synthetic',))
    monkeypatch.setattr(watch, 'seats', lambda *_: [seat])
    original = svc.ship.sessions['crew']
    svc.ship.sessions['crew'] = replace(original, human=True)
    world.native.box = "a person's half-written draft"  # the wake pass reads, finds the box taken, and holds
    with monkeypatch.context() as guard:
        reads = wake_pass_reads_only(world, guard)
        facts = watch.facts(svc.ship, seat.tmux, walls=[], login=[])
        assert not facts.needs_human and facts.unarmed == [seat.name]
        assert Door(seat).reading().reason == "a human's session"
        Service.deliveries(svc)
        assert not svc.store.for_seat('desk'), 'ordinary human/unarmed behavior is unchanged'
        assert 'state' in reads and 'capture' in reads, 'only the wake pass read the state and the screen'
        assert from_status(seat) == 'human wake box not empty; untouched' and not world.native.calls
    world.native.box = ''
    svc.ship.sessions['crew'] = original
    world.native.auto_complete = False
    recovery.run(svc)
    if unknown:
        con = database_connection(world.record)
        part(con, 'unknown', 'recovery-answer', 'root', 10016, {'type': 'future_tool_call', 'name': 'bash'})
        con.commit()
        con.close()
        recovery.run(svc)
    proof = journal(world)
    svc.ship.sessions['crew'] = replace(original, human=True)
    assert seat.session.human and not seat.spec.human_session, 'actual Session ownership is authoritative'
    before = proof.path.read_bytes()
    calls = list(world.native.calls)
    forbid_human_content_and_input(world, monkeypatch)
    return proof, before, calls


def test_human_ownership_exposes_a_running_mode_hold_without_conversation_reads(world, monkeypatch):
    proof, before, calls = human_ownership_transition(world, monkeypatch, unknown=False)
    seat, svc = world.seat, world.service
    facts = watch.facts(svc.ship, seat.tmux, walls=[], login=[])
    assert facts.needs_human == [seat.name] and facts.unarmed == [seat.name]
    assert 'mode unresolved' in '\n'.join(facts.pending)
    reading = Door(seat).reading()
    assert not reading.open and reading.clears == 'needs a human' and not reading.waiting
    recovery.run(svc)
    assert proof.path.read_bytes() == before and world.native.calls == calls
    assert world.native.selected == 'qd-recovery-off' and len(world.native.inputs) == 1


def test_human_ownership_reports_unknown_mode_and_keeps_every_new_input_held(world, monkeypatch):
    proof, before, calls = human_ownership_transition(world, monkeypatch, unknown=True)
    seat, svc = world.seat, world.service
    svc.store.new('order', 'captain', seat.name, 'Late new work')
    svc.store.new('report', 'ship', seat.name, 'New rebrief notification')
    attempts = [a.path for a in svc.attempts.all()]
    Service.deliveries(svc)
    Service.deliveries(svc)
    reports = svc.store.for_seat('desk')
    assert len(reports) == 1 and 'mode unresolved' in reports[0].body
    assert [a.path for a in svc.attempts.all()] == attempts
    recovery.run(svc)
    assert Service.control(svc, seat, 'effort', 'high').startswith('effort held:')
    assert Service.relaunch(svc, seat, False) is None
    Service.fresh_after_order(svc)
    Service.failover(svc)
    for action in (seat.up, seat.restart, seat.fresh, lambda: seat.failover_to('synthetic')):
        with pytest.raises(ShipError, match='mode unresolved'):
            action()
    assert proof.path.read_bytes() == before and world.native.calls == calls
    assert world.native.selected == 'qd-recovery-off' and len(world.native.inputs) == 1


@pytest.mark.parametrize('change', ['native-option', 'native-selection', 'declaration'])
def test_intervening_setting_is_never_overwritten_on_restore(world, monkeypatch, change):
    original = recovery._restore
    monkeypatch.setattr(recovery, '_restore', lambda *a: (_ for _ in ()).throw(RuntimeError('receipt before restore')))
    with pytest.raises(RuntimeError):
        recovery.run(world.service)
    if change == 'native-option':
        path = world.seat.home / 'config/opencode/opencode.json'
        config = json.loads(path.read_text())
        config['provider']['synthetic']['models']['qwen3.8-27b']['options']['seed'] = 7
        path.write_text(json.dumps(config))
    elif change == 'native-selection':
        path = world.seat.home / 'state/opencode/model.json'
        path.write_text(json.dumps({'variant': {'synthetic/qwen3.8-27b': 'custom'}}))
        world.native.selected = 'custom'
    else:
        world.seat.spec = replace(world.seat.spec, settings={})
    calls = list(world.native.calls)
    monkeypatch.setattr(recovery, '_restore', original)
    recovery.run(world.service)
    assert recovery.held(world.seat) and world.native.calls == calls
    assert Door(world.seat).reading().clears == 'needs a human'


def test_changed_completed_tool_evidence_after_paste_refuses_submit_and_keeps_the_box(world):
    original = world.native.paste
    def changed(ident, text, **kwargs):
        original(ident, text, **kwargs)
        con = database_connection(world.record)
        con.execute("UPDATE part SET data=json_set(data,'$.state.output','changed synthetic result') WHERE id='earlier-tool'")
        con.commit()
        con.close()
    world.native.paste = changed
    recovery.run(world.service)
    assert not world.native.inputs and world.native.box
    assert sum(c == ('keys', ('Enter',)) for c in world.native.calls) == 0
    assert journal(world).data['phase'] == 'pasting' and recovery.held(world.seat)


def test_changed_launch_is_not_adopted_and_identity_checked_down_is_still_available(world):
    world.native.fault = 'off-after'
    recovery.run(world.service)
    proof = journal(world)
    launch = world.seat.launched()
    replace(launch, at=(AT + timedelta(seconds=2)).isoformat()).write(world.seat.folder / 'launch.json')
    world.native.fault = None
    recovery.run(world.service)
    assert len(world.native.calls) == 1 and recovery.held(world.seat)
    with pytest.raises(ShipError):
        world.seat.restart()
    assert 'down' in world.seat.down()
    assert world.native.calls[-1] == ('down', '%1')
    assert journal(world).data['recovery']['source'] == proof.data['recovery']['source']
    assert journal(world).data['recovery']['evidence'] == launch.recovery


@pytest.mark.parametrize('fault', ['human', 'draft', 'queue', 'config', 'missing-launch-proof', 'wrong-version', 'native-custom'])
def test_unproven_original_state_never_claims_or_types(world, fault):
    seat = world.seat
    if fault == 'human':
        seat.ship.sessions['crew'] = Session('crew', 'Crew', human=True)
    elif fault == 'draft':
        world.native.box = 'Foreign draft'
    elif fault == 'queue':
        original = world.native.capture
        world.native.capture = lambda *a, **k: '  ┃  QUEUED\n' + original(*a, **k)
    elif fault == 'config':
        p = seat.home / 'config/opencode/opencode.json'
        p.write_text(p.read_text() + '\n')
    elif fault in ('missing-launch-proof', 'wrong-version'):
        launch = seat.launched()
        replace(launch, **({'recovery': None} if fault == 'missing-launch-proof' else {'version': '1.19.6'})).write(seat.folder / 'launch.json')
    else:
        world.native.selected = 'custom'
    recovery.run(world.service)
    assert not world.native.calls and not pending.journals(seat)[0]


@pytest.mark.parametrize('field,value', [('format', 2), ('mode', 'future'), ('client', 'f' * 64), ('recovery_input', 12)])
def test_malformed_extension_is_a_visible_hold_not_an_absent_reservation(world, field, value):
    recovery.run(world.service)
    proof = journal(world)
    proof.save(recovery={**proof.data['recovery'], field: value})
    calls = list(world.native.calls)
    assert pending.journals(world.seat)[1]
    assert 'journal unreadable' in Door(world.seat).reading().reason
    recovery.run(world.service)
    assert world.native.calls == calls
