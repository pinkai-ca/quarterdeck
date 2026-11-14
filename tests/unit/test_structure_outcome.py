"""Ordinary ended-turn outcomes using the fake program and invented pi records."""

from contextlib import nullcontext
from dataclasses import replace
from datetime import timedelta
from itertools import count
import json
from types import SimpleNamespace

import pytest

from quarterdeck import cascade, group_post, post, record_owner, structure_outcome as outcome
from quarterdeck.harness.fake import FakeHarness
from quarterdeck.harness.pi_thinking import Client
from quarterdeck.harness.simulator import Program
from quarterdeck.harness.types import Inputs, Tokens, Turn
from quarterdeck.seat import Launched
from quarterdeck.ship import Cascade, Group, Pool, SeatSpec, Session, Ship
from quarterdeck.tmux import Pane


@pytest.fixture
def structures(tmp_path, monkeypatch):
    serial = count()
    monkeypatch.setattr(post.Store, '_new_id', lambda self, kind, sender, at: f'{kind[:3]}-synthetic-z{next(serial)}')
    monkeypatch.setenv('QD_FAKE_STEPS', '0')
    monkeypatch.setenv('QD_FAKE_ACCEPT_SECONDS', '0')
    monkeypatch.setattr(Program, 'say', lambda *args: None)
    monkeypatch.setattr(Program, 'draw', lambda *args: None)
    names = ('alpha', 'beta', 'gamma', 'lead', 'conductor')
    specs = {name: SeatSpec(name, 'crew', 'ring', 'fake', ('fake',), effort='low') for name in names}
    ship = Ship(tmp_path, 'synthetic', 'operator', tmp_path, {'fake': Pool('fake', 'synthetic')},
                {'crew': Session('crew', 'Synthetic')}, specs,
                groups={'reviewers': Group('reviewers', ('alpha',), 'lead')},
                cascade=Cascade(('alpha', 'beta', 'gamma'), 'conductor', ('low',)))
    svc = SimpleNamespace(ship=ship, runtime=ship.runtime, store=post.Store(ship.runtime),
                          structure_passes=outcome.Passes())
    seats = {}
    svc.seat = seats.__getitem__

    def take(order, reason='stop', text=None):
        name = order.to
        home = ship.runtime / 'seats' / name / 'home'
        home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv('QD_FAKE_HOME', str(home))
        monkeypatch.setenv('QD_FAKE_END_REASON', reason)
        program = Program(SimpleNamespace(shape='pi', model='synthetic-model', effort='low',
                                          resume='session-test', brief=None, first=None))
        launch = Launched('%synthetic', 12345, (order.at - timedelta(seconds=1)).isoformat(), 'fake', None,
                          ['fake'], 'fake', 'fake')
        launch.write(home.parent / 'launch.json')
        (home.parent / 'record').write_text(str(program.record.path))
        record_owner.register(home.parent, program.record.path, 'fake')
        locks = ship.runtime / 'locks/lifecycle'
        locks.mkdir(parents=True, exist_ok=True)
        (locks / f'{name}.lock').touch()
        pane = Pane(launch.pane, 'Synthetic', 'Synthetic', launch.pid, 100, 40, False, False, None)
        seat = SimpleNamespace(name=name, ship=ship, spec=specs[name], pool=ship.pools['fake'],
                               home=home, folder=home.parent, session=ship.sessions['crew'], harness=FakeHarness(),
                               _lifecycle=lambda **kwargs: nullcontext(), current_launch=launch, current_pane=pane,
                               shown='────────────────\n> \n────────────────\nfake pi · model synthetic-model · effort low · idle')
        seat.launched = lambda: seat.current_launch
        seat.pane = lambda: seat.current_pane
        seat.record = lambda: program.record.path
        seat.tmux = SimpleNamespace(capture=lambda *args, **kwargs: seat.shown)
        seats[name] = seat
        program.turn(post.typed([order]) if text is None else text)
        return seat, program

    def make(kind):
        if kind == 'forum':
            root, copies = group_post.submit(ship, svc.store, 'reviewers', 'operator', 'Review a fictional garden.')
            order = copies[0]
            advance = lambda: group_post.advance(ship, svc.store, outcomes=lambda n, m: outcome.observe(svc, n, m))
            finished = lambda: bool(json_read(ship.runtime / 'post/groups' / f'{root.id}.json')['notice'])
        else:
            root = svc.store.new('question', 'operator', 'conductor', 'Improve a fictional garden.')
            cascade.start(svc, root.id, 'conductor')
            path = cascade.folder(ship.runtime, root.id)
            runner = cascade.Runner(ship, svc.store, lambda *args: None,
                                    lambda n, e: dict(requested=e, actual=e, detail='Synthetic effort.'),
                                    outcomes=lambda n, m: outcome.observe(svc, n, m))
            runner.advance(path)
            order = svc.store.get(cascade.load(path)['steps'][-1]['order'])
            advance = lambda: runner.advance(path)
            finished = lambda: cascade.load(path)['status'] == 'stopped'
        return SimpleNamespace(kind=kind, root=root, order=order, advance=advance, finished=finished,
                               service=svc, ship=ship, take=take, seats=seats)
    return make


def json_read(path):
    import json
    return json.loads(path.read_text())


def notices(case):
    who = 'lead' if case.kind == 'forum' else 'conductor'
    return [m for m in case.service.store.for_seat(who) if m.sender == post.SHIP]


def pass_once(case):
    case.service.structure_passes.begin()
    case.advance()
    case.service.structure_passes.finish()


def file_report(case, order=None):
    order = order or case.order
    body = '## Cascade note\nSynthetic improvement.\n## Answer\nLast FILED whole garden answer.'
    return case.service.store.new('report', order.to, order.sender, body, re=order.id)


@pytest.mark.parametrize('kind', ['forum', 'cascade'])
@pytest.mark.parametrize('reason', ['stop', 'length'])
def test_fake_end_has_one_pass_grace_then_exactly_one_named_outcome(structures, kind, reason):
    case = structures(kind)
    seat, program = case.take(case.order, reason)
    state = seat.harness.state(program.record.path)
    assert state.completed_turns[-1].end_reason == reason
    assert state.completed_turns[-1].inputs[0].text == post.typed([case.order])
    pass_once(case)
    assert not case.finished() and notices(case) == []
    assert outcome.waits(case.ship, [seat]) == ['waiting on alpha: turn ended, no report']
    # Reading status, including many times, never consumes another service pass.
    for _ in range(4):
        assert outcome.waits(case.ship, [seat]) == ['waiting on alpha: turn ended, no report']
    pass_once(case)
    assert case.finished()
    final = notices(case)
    expected = outcome.LENGTH if reason == 'length' else outcome.NORMAL
    assert len(final) == 1 and f'alpha: {expected}' in final[0].body
    assert case.service.store.is_open(case.order) and case.service.store.is_open(case.root)
    assert outcome.waits(case.ship, [seat]) == []
    pass_once(case)
    late = file_report(case)
    pass_once(case)
    assert notices(case) == final and late.id not in final[0].body
    if kind == 'cascade':
        data = cascade.load(cascade.folder(case.ship.runtime, case.root.id))
        assert len(data['steps']) == 1 and data['last'] is None
        assert 'No ring answer completed.' in final[0].body
        assert not [m for m in case.service.store.all() if m.kind == 'order' and m.to in ('beta', 'gamma')]


@pytest.mark.parametrize('kind', ['forum', 'cascade'])
def test_report_admitted_during_grace_wins(structures, kind):
    case = structures(kind)
    seat, _ = case.take(case.order, 'length')
    pass_once(case)
    filed = file_report(case)
    assert outcome.waits(case.ship, [seat]) == []
    pass_once(case)
    if kind == 'forum':
        assert len(notices(case)) == 1 and 'all filed' in notices(case)[0].body
        assert filed.id in notices(case)[0].body
    else:
        data = cascade.load(cascade.folder(case.ship.runtime, case.root.id))
        assert data['status'] == 'running' and data['last'] == filed.id
        assert len(data['steps']) == 2 and notices(case) == []
    assert not outcome.waits(case.ship, [seat])


@pytest.mark.parametrize('kind', ['forum', 'cascade'])
def test_same_pass_reentry_and_service_restart_cannot_age_grace(structures, kind):
    case = structures(kind)
    case.take(case.order)
    tracker = case.service.structure_passes
    tracker.begin()
    case.advance()
    case.advance()
    assert not case.finished()
    # An incomplete pass and all old candidates disappear on service restart.
    case.service.structure_passes = outcome.Passes()
    pass_once(case)
    assert not case.finished()
    pass_once(case)
    assert case.finished() and len(notices(case)) == 1


@pytest.mark.parametrize('kind', ['forum', 'cascade'])
@pytest.mark.parametrize('change', ['working', 'tools', 'queue', 'queue-error', 'screen-queue', 'composer',
                                   'no-completion', 'wrong-input', 'partial', 'reader-unknown', 'new-launch',
                                   'no-pane', 'changed-owner', 'missing-footer'])
def test_activity_or_unknown_evidence_resets_candidate(structures, monkeypatch, kind, change):
    case = structures(kind)
    seat, program = case.take(case.order)
    pass_once(case)
    read = seat.harness.state
    changes = {'working': dict(turn_open=True), 'tools': dict(pending_tools=None),
               'queue': dict(queued=('synthetic queued input',)), 'queue-error': dict(queue_error='synthetic queue unknown'),
               'no-completion': dict(completed_turns=())}
    if change in changes:
        monkeypatch.setattr(seat.harness, 'state', lambda p: replace(read(p), **changes[change]))
    elif change == 'screen-queue':
        seat.shown += ' · queued 1'
    elif change == 'composer':
        seat.shown = seat.shown.replace('> \n', '> unfinished draft\n')
    elif change == 'wrong-input':
        program.turn('An unrelated synthetic turn.')
    elif change == 'partial':
        with program.record.path.open('a') as stream:
            stream.write('{"type":')
    elif change == 'no-pane':
        seat.current_pane = None
    elif change == 'changed-owner':
        seat.current_pane = replace(seat.current_pane, pid=seat.current_pane.pid + 1)
    elif change == 'missing-footer':
        seat.shown = '\n'.join(seat.shown.splitlines()[:-1])
    elif change == 'reader-unknown':
        (seat.folder / record_owner.REGISTRY).write_text(
            json.dumps(dict(record=str(program.record.path), harness=None)) + '\n')
    else:
        seat.current_launch = replace(seat.current_launch, at=(post.now() + timedelta(seconds=1)).isoformat())
    assert all('turn ended, no report' not in line for line in outcome.waits(case.ship, [seat]))
    pass_once(case)
    assert not case.finished() and not notices(case)
    assert any('unknown' in line for line in outcome.waits(case.ship, [seat]))
    assert case.order.id not in case.service.structure_passes.candidates


@pytest.mark.parametrize('kind', ['forum', 'cascade'])
def test_renewed_work_needs_its_own_complete_grace(structures, kind):
    case = structures(kind)
    seat, program = case.take(case.order)
    pass_once(case)
    program.turn(post.typed([case.order]))
    pass_once(case)
    assert not case.finished()
    pass_once(case)
    assert case.finished()


def test_cascade_stop_keeps_last_filed_answer_and_notes(structures):
    case = structures('cascade')
    case.take(case.order)
    filed = file_report(case)
    pass_once(case)
    path = cascade.folder(case.ship.runtime, case.root.id)
    order = case.service.store.get(cascade.load(path)['steps'][-1]['order'])
    case.order = order
    case.take(order, 'length')
    notes = (path / 'responses.md').read_bytes()
    pass_once(case)
    pass_once(case)
    data = cascade.load(path)
    assert data['last'] == filed.id and data['status'] == 'stopped'
    assert len(data['steps']) == 2 and (path / 'responses.md').read_bytes() == notes
    assert notices(case)[0].body.endswith(filed.body)
    assert f'beta: {outcome.LENGTH}' in notices(case)[0].body


def test_old_turn_defaults_do_not_invent_steered_membership_or_reason():
    now = post.now()
    turn = Turn(0, now, now + timedelta(seconds=1), 'Other input', '', 'synthetic', Tokens())
    assert turn.inputs is None and turn.end_reason is None
    inputs = Inputs()
    from quarterdeck.harness.types import Input
    inputs.append(Input('Exact synthetic order', now))
    assert not outcome._associated(turn, 'Exact synthetic order', now, inputs)
    explicit = replace(turn, inputs=tuple(inputs), end_reason='stop')
    assert outcome._associated(explicit, 'Exact synthetic order', now, inputs)


def reserve_continuation(case, seat, original):
    """A real pending journal/attempt, with an invented admitted native client."""
    from dataclasses import asdict
    from quarterdeck import pending
    evidence = dict(format=1, configuration={'synthetic.txt': '0' * 64}, environment={},
                    settings=dict(original='low', model=original.model, provider=original.provider, output=original.written))
    client = Client(seat.home, evidence)
    message = case.service.store.new('report', 'ship', seat.name, client.continuation, re=case.root.id)
    payload = post.typed([message])
    launched = seat.launched()
    attempt = post.Attempts(case.ship.runtime).open(message, seat.name, launched.pane, launched.pid, launched.at)
    proof = pending.Proof.begin(seat, attempt, payload)
    proof.save(phase='submitted', recovery=dict(format=1, mode='submitted', source=asdict(original),
               client=client.signature, evidence=evidence, post=message.id, recovery_input=None, error=None))
    assert pending.journals(seat)[1] == []
    return proof, client, payload


@pytest.mark.parametrize('kind', ['forum', 'cascade'])
def test_reserved_continuation_gets_its_own_grace_without_reusing_original_cap(structures, monkeypatch, kind):
    from quarterdeck import recovery
    case = structures(kind)
    seat, program = case.take(case.order, 'length')
    original = seat.harness.state(program.record.path).capped
    assert original is not None
    pass_once(case)  # structures observe first; ordinary recovery has this pass's opportunity
    proof, client, payload = reserve_continuation(case, seat, original)
    monkeypatch.setattr(recovery, '_client', lambda s: client)
    pass_once(case)
    assert not case.finished()
    assert case.order.id not in case.service.structure_passes.candidates
    assert any('thinking recovery unresolved' in line or 'tools or queue unknown' in line
               for line in outcome.waits(case.ship, [seat]))
    program.record.end_reason = 'stop'
    program.turn(payload)
    assert proof.confirmed(seat) == 'delivered'
    proof.finish('delivered')
    meta = proof.data['recovery']
    meta.update(mode='done', recovery_input=seat.harness.state(program.record.path).work_id)
    proof.save(recovery=meta)
    pass_once(case)
    assert not case.finished()  # final continuation, rather than the consumed original cap, gets grace
    pass_once(case)
    assert case.finished() and outcome.NORMAL in notices(case)[0].body
    assert outcome.LENGTH not in notices(case)[0].body
    assert meta['source']['work'] == original.work  # no source rewrite or new allowance


@pytest.mark.parametrize('kind', ['forum', 'cascade'])
def test_continuation_with_unknown_lineage_never_becomes_original_no_report_outcome(structures, monkeypatch, kind):
    from quarterdeck import recovery
    case = structures(kind)
    seat, program = case.take(case.order, 'length')
    original = seat.harness.state(program.record.path).capped
    proof, client, payload = reserve_continuation(case, seat, original)
    monkeypatch.setattr(recovery, '_client', lambda s: client)
    program.record.end_reason = 'stop'
    program.turn(payload)
    assert proof.confirmed(seat) == 'delivered'
    proof.finish('delivered')
    proof.save(recovery={**proof.data['recovery'], 'mode': 'done', 'recovery_input': 'missing-native-input-zzzz'})
    pass_once(case)
    pass_once(case)
    assert not case.finished() and notices(case) == []
    assert any('thinking recovery unresolved' in line for line in outcome.waits(case.ship, [seat]))


@pytest.mark.parametrize('kind', ['forum', 'cascade'])
def test_supported_cap_still_awaiting_reservation_is_visible_without_settling(structures, monkeypatch, kind):
    from quarterdeck import recovery
    case = structures(kind)
    seat, program = case.take(case.order, 'length')
    cap = seat.harness.state(program.record.path).capped
    evidence = dict(format=1, configuration={'synthetic.txt': '0' * 64}, environment={},
                    settings=dict(original='low', model=cap.model, provider=cap.provider, output=cap.written))
    seat.current_launch = replace(seat.current_launch, recovery=evidence)
    client = Client(seat.home, evidence)
    monkeypatch.setattr(recovery, '_client', lambda s: client)
    pass_once(case)
    assert outcome.waits(case.ship, [seat]) == ['waiting on alpha: turn ended, no report']
    pass_once(case)
    pass_once(case)
    assert not case.finished() and notices(case) == []
    assert any('thinking recovery unresolved' in line for line in outcome.waits(case.ship, [seat]))


def test_aborted_pi_end_and_unsupported_input_diagnostics_remain_unknown(structures, monkeypatch):
    case = structures('forum')
    seat, program = case.take(case.order)
    read = seat.harness.state
    monkeypatch.setattr(seat.harness, 'inputs', lambda p: list(FakeHarness().inputs(p)))
    pass_once(case)
    pass_once(case)
    assert not case.finished()
    monkeypatch.setattr(seat.harness, 'inputs', FakeHarness().inputs)
    state = read(program.record.path)
    changed = replace(state, completed_turns=(replace(state.completed_turns[-1], end_reason='aborted'),))
    monkeypatch.setattr(seat.harness, 'state', lambda p: changed)
    pass_once(case)
    pass_once(case)
    assert not case.finished()


def test_status_invalidates_stale_side_state_and_preserves_overflow_category(structures):
    from quarterdeck import headlines, watch
    case = structures('forum')
    seat, program = case.take(case.order)
    pass_once(case)
    path = outcome._path(case.ship.runtime, case.order.id)
    before = path.read_bytes()
    seat.shown = seat.shown.replace('> \n', '> new draft\n')
    assert outcome.waits(case.ship, [seat]) == ['waiting on alpha: turn outcome unknown (record proof unknown)']
    assert path.read_bytes() == before  # status never writes observations or ages grace
    facts = watch.Facts('ok', 0, 0, 0, structure_waiting=outcome.waits(case.ship, [seat]))
    assert 'turn outcome unknown' in facts.line()
    assert '1 structure waits' in headlines.status_line(facts, ['Synthetic alert ' * 30])
    path.write_text('{')
    assert outcome.waits(case.ship, [seat]) == ['structure wait evidence unreadable']
