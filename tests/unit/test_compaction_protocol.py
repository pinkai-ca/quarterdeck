"""Explicit readiness, immutable intent recovery, and incoming admission at a fresh boundary."""

from dataclasses import replace
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
from threading import Event, Thread

import pytest

from quarterdeck import compaction, post, record_owner, watch
from quarterdeck.harness import get
from quarterdeck.harness.base import Join, Screen
from quarterdeck.seat import Launched, Seat
from quarterdeck.service import Service
from quarterdeck.ship import Pool, SeatSpec, Session, Ship, ShipError
from tests.compaction_shapes import AT, Native
from .resting_attempts import UNSETTLED, save_attempt, settle


@pytest.fixture(params=['claude', 'codex'])
def world(tmp_path, monkeypatch, request):
    program = request.param
    spec = SeatSpec('worker', 'crew', 'worker', program, ('synthetic',), fresh_after_compactions=2)
    ship = Ship(tmp_path/'ship', 'synthetic', 'operator', tmp_path,
                {'synthetic': Pool('synthetic', program)}, {'crew': Session('crew', 'Synthetic')}, {'worker': spec})
    pane = SimpleNamespace(id='%1', pid=123, dead=False)
    tmux = SimpleNamespace(pane=lambda _: pane, capture=lambda *a, **k: 'synthetic screen')
    svc, clock = Service(ship, tmux), [AT]
    monkeypatch.setattr(post, 'now', lambda: clock[0])
    seat = svc.seat('worker')
    seat.folder.mkdir(parents=True)
    seat.trusted.mkdir(parents=True, exist_ok=True)
    source = [Native(seat.home/'synthetic-record', program)]
    monkeypatch.setattr(post, 'now', lambda: AT + timedelta(seconds=source[0].serial + 0.5))
    Launched('%1', 123, AT.isoformat(), 'synthetic', None, [program], 'synthetic', harness=program).write(seat.trusted/'launch.json')
    def pin():
        (seat.trusted/'record').write_text(str(source[0].path))
        record_owner.register(seat.trusted, source[0].path, program)
    pin()
    monkeypatch.setattr(seat, 'record', lambda: source[0].path)
    monkeypatch.setattr(seat, 'join', lambda: Join(source[0].path))
    monkeypatch.setattr(seat.harness, 'screen', lambda _: Screen('synthetic', '', working=seat.harness.state(source[0].path).turn_open))
    calls, carried = [], []
    def down():
        calls.append('down')
        return 'synthetic down'
    def up(resume=True, *, observation=None, target=None):
        first, messages = seat.first_message()
        carried.append(messages)
        calls.append('resume' if resume else 'fresh')
        if not resume:
            source[0] = Native(seat.home/f'synthetic-record-{len(calls)}', program, f'synthetic-successor-{len(calls)}')
        Launched('%1', 123, (AT + timedelta(microseconds=len(calls))).isoformat(), 'synthetic', source[0].conversation if resume else None,
                 [program], 'synthetic', harness=program).write(seat.trusted/'launch.json')
        pin()
        return 'synthetic launch'
    monkeypatch.setattr(seat, '_down', down)
    monkeypatch.setattr(seat, '_up', up)
    yield SimpleNamespace(svc=svc, seat=seat, source=source, calls=calls, carried=carried, clock=clock, program=program)
    svc.ledger.close()


def threshold(w):
    w.source[0].compact()
    compaction.tick(w.seat)
    assert compaction.load(w.svc.runtime, w.seat.name)['request'] is None
    w.source[0].compact()
    compaction.tick(w.seat)
    data = compaction.load(w.svc.runtime, w.seat.name)
    return post.Message.from_json(data['request'])


def handoff(w, request):
    return w.svc.store.new('order', w.seat.name, w.seat.name, 'Synthetic durable handoff', re=request.id)


def ready(w):
    request = threshold(w)
    hand = handoff(w, request)
    w.source[0].call()
    act = compaction.prepare_ready(w.seat, request.id, hand.id)
    compaction.arm(w.seat, **act)
    return request, hand, act


def test_native_first_second_events_and_deduplicated_request_across_service_reads(world):
    w = world
    request = threshold(w)
    for _ in range(3):
        compaction.tick(w.seat)
    assert w.calls == []
    assert [m for m in w.svc.store.for_seat(w.seat.name) if m.sender == post.SHIP] == [request]
    assert compaction.load(w.svc.runtime, w.seat.name)['count'] == 2


def test_report_without_control_never_arms_then_exact_completion_freshens_once(world):
    w = world
    request = threshold(w)
    hand = handoff(w, request)
    assert hand not in w.svc.attempts.undelivered(w.svc.store, w.seat.name)
    w.source[0].call()
    act = compaction.prepare_ready(w.seat, request.id, hand.id)
    assert compaction.prepare_ready(w.seat, request.id, hand.id) == act
    compaction.tick(w.seat)
    assert not w.calls and compaction.load(w.svc.runtime, w.seat.name)['ready'] is None
    assert not w.svc.store.is_open(request) and w.svc.store.is_open(hand)
    compaction.arm(w.seat, **act)
    compaction.tick(w.seat)
    assert not w.calls
    w.source[0].result()
    compaction.tick(w.seat)
    assert not w.calls  # a tool result is not turn completion
    w.source[0].finish()
    compaction.tick(w.seat)
    assert w.calls == ['down', 'fresh'] and hand in w.carried[-1]
    for _ in range(3):
        compaction.tick(w.seat)
    assert w.calls == ['down', 'fresh']
    assert compaction.load(w.svc.runtime, w.seat.name)['count'] == 0
    assert w.svc.store.is_open(hand)


@pytest.mark.parametrize('world', ['claude'], indirect=True)
@pytest.mark.parametrize('races', [1, 2])
def test_tick_retries_a_prefix_proof_after_a_native_append(world, monkeypatch, races):
    w = world
    native = w.source[0]
    native.compact()
    compaction.tick(w.seat)
    prior = compaction.load(w.svc.runtime, w.seat.name)
    assert prior['count'] == 1 and prior['request'] is None

    original = compaction.current
    counts = []

    def read_then_append(seat):
        observation = original(seat)
        reading = observation[1]
        counts.append(reading.count)
        if len(counts) <= races:
            serial = native.serial + len(counts)
            row = dict(type='system', subtype='compact_boundary', compactMetadata={'trigger': 'auto'},
                       uuid=f'synthetic-append-{serial}', sessionId=native.conversation, isSidechain=False,
                       timestamp=(AT + timedelta(seconds=serial)).isoformat(), parentUuid=None,
                       logicalParentUuid=native.previous if len(counts) == 1 else f'synthetic-append-{serial - 1}')
            with native.path.open('a') as stream:
                stream.write(json.dumps(row) + '\n')
        return observation

    monkeypatch.setattr(compaction, 'current', read_then_append)
    compaction.tick(w.seat)
    assert counts == [1, 2]
    data = compaction.load(w.svc.runtime, w.seat.name)
    assert data['error'] is None
    if races == 2:
        assert data['reading_unknown'] and data['request'] is None
        assert data['count'] == prior['count'] and data['digest'] == prior['digest']
        assert 'count unknown' in compaction.status(w.seat)
        compaction.tick(w.seat)
        assert counts == [1, 2, 3]
        data = compaction.load(w.svc.runtime, w.seat.name)
    assert data['count'] == 1 + races and not data['reading_unknown']
    raw = native.path.read_bytes()
    assert (data['extent'], data['digest']) == (len(raw), sha256(raw).hexdigest())
    assert data['request'] is not None and w.calls == []


def test_ordinary_report_prose_cannot_arm_or_close_reserved_handoff(world):
    w = world
    request = threshold(w)
    hand = handoff(w, request)
    with pytest.raises(ShipError, match='stays open'):
        w.svc.store.new('report', w.seat.name, w.seat.name, 'Synthetic early close', re=hand.id)
    w.svc.store.new('report', w.seat.name, post.SHIP, 'Synthetic prose says ready at a natural break', re=request.id)
    w.source[0].finish()
    compaction.tick(w.seat)
    assert not w.calls and w.svc.store.is_open(hand) and not w.svc.store.is_open(request)
    assert compaction.load(w.svc.runtime, w.seat.name)['ready'] is None


def test_explicit_attestation_flag_required_before_publishing_report(world, monkeypatch):
    from quarterdeck import cli
    w = world
    request = threshold(w)
    hand = handoff(w, request)
    monkeypatch.setenv('QD_SEAT', w.seat.name)
    monkeypatch.setattr(cli, 'load_ship', lambda _: w.seat.ship)
    monkeypatch.setattr(cli, 'seat_of', lambda *_: w.seat)
    args = SimpleNamespace(seat=w.seat.name, cancel=False, at_break=False, request=request.id, handoff=hand.id)
    with pytest.raises(ShipError, match='explicit --at-break'):
        cli.cmd_fresh_ready(args)
    assert w.svc.store.is_open(request) and w.svc.store.is_open(hand)
    assert compaction.load(w.svc.runtime, w.seat.name)['attestation'] is None


@pytest.fixture
def readiness_cli(world, monkeypatch):
    from quarterdeck import cli

    monkeypatch.setenv('QD_SEAT', world.seat.name)
    monkeypatch.setattr(cli, 'load_ship', lambda _: world.seat.ship)
    monkeypatch.setattr(cli, 'seat_of', lambda *_: world.seat)
    monkeypatch.setattr(cli, 'handoff', lambda *_: None)

    def invoke(*options):
        return cli.main(['--ship', str(world.seat.ship.folder), 'fresh-ready', *options])

    return invoke


def test_manual_readiness_cli_records_an_open_handoff_without_a_request(world, readiness_cli, monkeypatch, capsys):
    w = world
    w.seat.spec = replace(w.seat.spec, fresh_after_compactions=None)
    hand = w.svc.store.new('order', w.seat.name, w.seat.name, 'Synthetic manual handoff')
    assert readiness_cli('--handoff', hand.short, '--at-break') == 0
    assert f'ready for manual fresh: attested with {hand.short}' in capsys.readouterr().out
    compaction.tick(w.seat)
    data = compaction.load(w.svc.runtime, w.seat.name)
    assert data['manual_attestation']['launch'] == compaction.launch_key(w.seat)
    assert data['manual_attestation']['handoff'] == hand.id
    assert data['request'] is None and data['ready'] is None and data['transition'] is None
    assert f'attested with {hand.short}' in compaction.status(w.seat, alerts_only=True)
    assert w.calls == [] and w.svc.store.is_open(hand)

    monkeypatch.delenv('QD_SEAT')
    assert readiness_cli(w.seat.name, '--cancel') == 0
    assert 'cancelled' in capsys.readouterr().out
    assert 'manual_attestation' not in compaction.load(w.svc.runtime, w.seat.name)
    assert compaction.status(w.seat, alerts_only=True) is None
    assert w.calls == [] and w.svc.store.is_open(hand)


def test_manual_readiness_requires_the_handoff_to_stay_open(world, readiness_cli, capsys):
    w = world
    hand = w.svc.store.new('order', w.seat.name, w.seat.name, 'Synthetic manual handoff')
    assert readiness_cli('--handoff', hand.id) == 2
    assert 'explicit --at-break' in capsys.readouterr().err
    w.svc.store.new('report', w.seat.name, post.SHIP, 'Synthetic handoff closed', re=hand.id)
    assert readiness_cli('--handoff', hand.id, '--at-break') == 2
    assert 'open self-addressed handoff' in capsys.readouterr().err
    assert compaction.load(w.svc.runtime, w.seat.name) is None


def test_request_readiness_cli_still_arms_the_automatic_control(world, readiness_cli, monkeypatch, capsys):
    from quarterdeck import service

    w = world
    request = threshold(w)
    hand = handoff(w, request)
    w.source[0].call()
    monkeypatch.setattr(service, 'alive', lambda _: True)
    monkeypatch.setattr(w.svc, 'seat', lambda _: w.seat)
    monkeypatch.setattr(service, 'request', lambda _, kind, **act: w.svc.answer(dict(kind=kind, **act)))
    assert readiness_cli('--request', request.short, '--handoff', hand.short, '--at-break') == 0
    assert 'waiting for exact turn completion' in capsys.readouterr().out
    data = compaction.load(w.svc.runtime, w.seat.name)
    assert data['ready']['handoff'] == hand.id and 'manual_attestation' not in data
    assert w.svc.store.is_open(hand) and w.calls == []


@pytest.mark.parametrize('change', ['input', 'compaction'])
def test_new_input_or_compaction_invalidates_and_releases_reservation(world, change):
    w = world
    _, hand, _ = ready(w)
    w.source[0].result()
    if change == 'input':
        w.source[0].begin()
    else:
        w.source[0].compact()
    compaction.tick(w.seat)
    data = compaction.load(w.svc.runtime, w.seat.name)
    assert data['ready'] is None and data['reserved'] is None and hand.id in data['used']
    assert not w.calls
    assert hand in w.svc.attempts.undelivered(w.svc.store, w.seat.name)


def test_a_later_tool_in_the_attested_turn_keeps_readiness_and_the_gate_waits_for_the_turn(world):
    """The attestation binds the turn, not its tools: the gate alone reads the tool state, after the turn ends."""
    w = world
    _, hand, _ = ready(w)
    bound = compaction.load(w.svc.runtime, w.seat.name)['ready']
    assert not {'frontier', 'call'} & bound.keys(), 'the binding names no tool'
    w.source[0].result()
    w.source[0].call()
    compaction.tick(w.seat)
    data = compaction.load(w.svc.runtime, w.seat.name)
    assert data['ready'] == bound and data['reserved'] == hand.id and not w.calls
    assert 'waiting for exact recorded turn completion' in compaction.status(w.seat)
    w.source[0].result()
    w.source[0].finish()
    compaction.tick(w.seat)
    assert w.calls == ['down', 'fresh'] and hand in w.carried[-1]


def test_incoming_after_ready_disarms_before_publication_and_late_control_is_stale(world):
    w = world
    _, hand, act = ready(w)
    message = w.svc.store.new('order', 'operator', w.seat.name, 'Synthetic new work')
    data = compaction.load(w.svc.runtime, w.seat.name)
    assert data['ready'] is None and data['reserved'] is None
    with pytest.raises(ShipError):
        compaction.arm(w.seat, **act)
    assert w.svc.store.is_open(hand) and w.svc.store.is_open(message)


def test_resume_cancel_disable_preserve_posts_and_count(world):
    w = world
    request, hand, _ = ready(w)
    generation = compaction.load(w.svc.runtime, w.seat.name)['generation']
    w.seat.restart()
    compaction.tick(w.seat)
    data = compaction.load(w.svc.runtime, w.seat.name)
    assert data['generation'] == generation and data['count'] == 2
    assert data['ready'] is None and data['reserved'] is None
    assert hand in w.carried[-1] and w.svc.store.is_open(hand)
    assert post.Message.from_json(data['request']).id == request.id
    w.seat.spec = replace(w.seat.spec, fresh_after_compactions=0)
    compaction.tick(w.seat)
    assert compaction.load(w.svc.runtime, w.seat.name)['disabled']
    assert w.svc.store.is_open(hand)


def test_human_notice_and_readiness_never_automatically_relaunch(world):
    w = world
    w.seat.ship.sessions[w.seat.spec.session] = replace(w.seat.session, human=True)
    request, hand, _ = ready(w)
    w.source[0].result()
    w.source[0].finish()
    for _ in range(3):
        compaction.tick(w.seat)
    assert not w.calls and w.svc.store.is_open(hand)
    assert request in w.svc.attempts.undelivered(w.svc.store, w.seat.name)
    assert 'manual' in compaction.status(w.seat)


def test_history_rewrite_is_unknown_and_never_reset_to_zero(world):
    w = world
    threshold(w)
    w.source[0].rows.pop()
    w.source[0].flush()
    compaction.tick(w.seat)
    assert 'unknown' in compaction.status(w.seat)
    assert not w.calls


def test_transition_crash_cannot_replay_fresh(world, monkeypatch):
    w = world
    ready(w)
    w.source[0].result()
    w.source[0].finish()
    def crash():
        w.calls.append('possibly-down')
        raise OSError('synthetic crash')
    monkeypatch.setattr(w.seat, '_down', crash)
    compaction.tick(w.seat)
    compaction.tick(w.seat)
    assert w.calls == ['possibly-down']
    assert compaction.load(w.svc.runtime, w.seat.name)['transition']


@pytest.mark.parametrize('kind', ['request', 'handoff', 'report'])
def test_fixed_intent_recovers_publication_without_duplicates(world, monkeypatch, kind):
    w = world
    original, failures = compaction.publish, []
    def publish(runtime, intent, **checked):  # the seat's name and journal the real publish checks the intent by
        message = post.Message.from_json(intent)
        target = ('request' if message.sender == post.SHIP else
                  'report' if message.kind == 'report' else 'handoff')
        if target == kind and not failures:
            failures.append(message.id)
            raise OSError('synthetic publication crash')
        return original(runtime, intent, **checked)
    if kind != 'handoff':
        monkeypatch.setattr(compaction, 'publish', publish)
    request = threshold(w)
    if kind == 'request':
        compaction.tick(w.seat)
    if kind == 'handoff':
        original_write = post.Store._write
        def fail_write(store, message):
            if message.kind == 'order' and message.sender == message.to and not failures:
                failures.append(message.id)
                raise OSError('synthetic handoff publication crash')
            return original_write(store, message)
        monkeypatch.setattr(post.Store, '_write', fail_write)
        with pytest.raises(OSError):
            handoff(w, request)
        compaction.tick(w.seat)
        hand = w.svc.store.get(failures[0])
    else:
        hand = handoff(w, request)
    w.source[0].call()
    if kind == 'report':
        with pytest.raises(OSError):
            compaction.prepare_ready(w.seat, request.id, hand.id)
        compaction.tick(w.seat)
    act = compaction.prepare_ready(w.seat, request.id, hand.id)
    assert compaction.prepare_ready(w.seat, request.id, hand.id) == act
    compaction.arm(w.seat, **act)
    compaction.arm(w.seat, **act)
    assert len([m for m in w.svc.store.all() if m.id == failures[0]]) == 1
    assert len([m for m in w.svc.store.all() if m.kind == 'report']) == 1
    assert len([m for m in w.svc.store.all() if m.sender == post.SHIP]) == 1


def test_post_after_transition_claim_is_admitted_during_slow_start_and_carried(world, monkeypatch):
    w = world
    _, hand, _ = ready(w)
    w.source[0].result()
    w.source[0].finish()
    entered, release = Event(), Event()
    original = w.seat._down
    errors = []
    def down():
        entered.set()
        assert release.wait(5), 'test admission/start barrier timed out'
        return original()
    monkeypatch.setattr(w.seat, '_down', down)
    def run():
        try:
            compaction.tick(w.seat)
        except BaseException as error:
            errors.append(error)
    thread = Thread(target=run)
    thread.start()
    try:
        assert entered.wait(5)
        with pytest.raises(ShipError, match='stays open'):
            w.svc.store.new('report', w.seat.name, w.seat.name, 'Synthetic premature close', re=hand.id)
        # Would block if admission were held through retirement/startup.
        message = w.svc.store.new('order', 'operator', w.seat.name, 'Synthetic post after claim')
        report = w.svc.store.new('report', 'operator', w.seat.name, 'Synthetic unread non-open post after claim')
        assert w.svc.attempts.undelivered(w.svc.store, w.seat.name) == []
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
    assert w.calls == ['down', 'fresh'] and hand in w.carried[-1] and message in w.carried[-1]
    assert not w.svc.store.is_open(report) and report in w.carried[-1]
    compaction.tick(w.seat)
    assert w.calls == ['down', 'fresh']


def test_post_winning_before_final_claim_prevents_fresh(world, monkeypatch):
    from contextlib import contextmanager
    w = world
    _, hand, _ = ready(w)
    w.source[0].result()
    w.source[0].finish()
    original = w.seat._lifecycle
    @contextmanager
    def claim(*, automatic=False):
        w.svc.store.new('order', 'operator', w.seat.name, 'Synthetic arrival wins')
        with original(automatic=automatic):
            yield
    monkeypatch.setattr(w.seat, '_lifecycle', claim)
    compaction.tick(w.seat)
    assert w.calls == [] and w.svc.store.is_open(hand)
    assert compaction.load(w.svc.runtime, w.seat.name)['ready'] is None


@pytest.mark.parametrize('suffix', ['json', 'claimed'])
def test_existing_lifecycle_request_blocks_automatic_claim(world, suffix):
    w = world
    ready(w)
    w.source[0].result()
    w.source[0].finish()
    folder = w.svc.runtime/'requests'
    folder.mkdir()
    path = folder/f'synthetic-explicit-relaunch.{suffix}'
    path.write_text(json.dumps(dict(kind='relaunch', seat=w.seat.name, resume=True)))
    compaction.tick(w.seat)
    assert not w.calls and path.exists(), 'explicit pending or possibly executed lifecycle intent wins'
    assert 'lifecycle request' in compaction.status(w.seat)


@pytest.mark.parametrize('phase', ['before-claim', 'after-claim'])
def test_alternate_recipient_cannot_close_reserved_handoff(world, monkeypatch, phase):
    w = world
    _, hand, _ = ready(w)
    refused = []
    def try_close():
        try:
            w.svc.store.new('report', w.seat.name, 'operator', 'Synthetic alternate-recipient close', re=hand.id)
        except ShipError:
            refused.append(True)
    if phase == 'before-claim':
        try_close()
    else:
        original = w.seat._down
        def down():
            try_close()
            return original()
        monkeypatch.setattr(w.seat, '_down', down)
    w.source[0].result()
    w.source[0].finish()
    compaction.tick(w.seat)
    assert refused == [True] and w.svc.store.is_open(hand), f'calls={w.calls}, carried={w.carried}'
    assert w.calls == ['down', 'fresh'] and hand in w.carried[-1]


@pytest.mark.parametrize('world', ['codex'], indirect=True)
@pytest.mark.parametrize('trigger', ['threshold', 'handoff', 'ordinary', 'ordinary-self'])
def test_compaction_after_order_excludes_protocol_closures_only(world, monkeypatch, trigger):
    w = world
    w.seat.spec = replace(w.seat.spec, fresh='after-order')
    monkeypatch.setattr(w.svc, 'resolved_seats', lambda: [w.seat])
    request = threshold(w)
    if trigger == 'threshold':
        opaque_call(w.source[0])
        opaque_result(w.source[0])
    w.svc.store.new('report', w.seat.name, post.SHIP, 'Synthetic request status report', re=request.id)
    if trigger == 'handoff':
        hand = handoff(w, request)
        w.source[0].finish()
        w.seat.fresh()  # explicit successor; the generation advances before its handoff report
        for row in w.source[0].rows:
            row['timestamp'] = (datetime.fromisoformat(row['timestamp']) + timedelta(hours=1)).isoformat()
        w.source[0].flush()
        monkeypatch.setattr(post, 'now', lambda: AT + timedelta(hours=1, seconds=w.source[0].serial + 0.5))
        compaction.tick(w.seat)
        assert compaction.load(w.svc.runtime, w.seat.name)['count'] == 0
        w.svc.store.new('report', w.seat.name, 'operator', 'Synthetic successor handoff closure', re=hand.id)
        assert not w.svc.store.is_open(hand)
    elif trigger.startswith('ordinary'):
        order = w.svc.store.new('order', w.seat.name if trigger == 'ordinary-self' else 'operator',
                                w.seat.name, 'Synthetic ordinary order')
        w.svc.store.new('report', w.seat.name, 'operator', 'Synthetic ordinary closure', re=order.id)
    previous_rows = len(w.source[0].rows)
    w.source[0].finish()
    if trigger == 'handoff':
        for row in w.source[0].rows[previous_rows:]:
            row['timestamp'] = (datetime.fromisoformat(row['timestamp']) + timedelta(hours=1)).isoformat()
        w.source[0].flush()
    compaction.tick(w.seat)
    before = list(w.calls)
    w.svc.fresh_after_order()
    assert w.calls[len(before):] == (['down', 'fresh'] if trigger.startswith('ordinary') else [])


@pytest.mark.parametrize('world', ['claude'], indirect=True)
def test_real_control_writer_releases_admission_before_waiting_for_reply(world, monkeypatch):
    from contextlib import contextmanager
    from quarterdeck import durable, servicectl
    w = world
    ready(w)
    w.source[0].result()
    w.source[0].finish()
    monkeypatch.setattr(w.svc, 'seat', lambda _: w.seat)
    monkeypatch.setattr(servicectl, 'alive', lambda _: True)
    monkeypatch.setattr(servicectl, 'ANSWER_SECONDS', 2)
    original_lock, original_write = post.admission, durable.replace_file
    entering, published = Event(), Event()
    replies, errors = [], []
    @contextmanager
    def observed_lock(runtime, name):
        entering.set()
        with original_lock(runtime, name):
            yield
    def observed_write(path, text):
        original_write(path, text)
        if path.parent.name == 'requests' and path.suffix == '.json':
            published.set()
    monkeypatch.setattr(post, 'admission', observed_lock)
    monkeypatch.setattr(durable, 'replace_file', observed_write)
    def ask():
        try:
            replies.append(servicectl.request(w.svc.runtime, 'relaunch', seat=w.seat.name, resume=True))
        except BaseException as error:
            errors.append(error)
    thread = Thread(target=ask)
    with original_lock(w.svc.runtime, w.seat.name):
        thread.start()
        assert entering.wait(2) and not published.is_set()
    try:
        assert published.wait(2)
        w.svc.requests()  # ordinary lifecycle takes admission again to disarm readiness
    finally:
        thread.join(3)
    assert not thread.is_alive() and not errors
    assert replies == ['synthetic down\nsynthetic launch'] and w.calls == ['down', 'resume']


@pytest.mark.parametrize('guard', ['queue-error', 'draft', 'blind', 'untimed', 'incoming'])
def test_uncertain_queue_composer_receipt_or_completion_holds_fresh(world, monkeypatch, guard):
    w = world
    if guard == 'blind':
        message = w.svc.store.new('answer', 'operator', w.seat.name, 'Synthetic old input')
        attempt = w.svc.attempts.open(message, w.seat.name, '%1', 123, AT.isoformat())
        w.svc.attempts.close(attempt, 'blind', 'synthetic unknown receipt')
    elif guard == 'incoming':
        w.svc.store.new('order', 'operator', w.seat.name, 'Synthetic unread incoming work')
    ready(w)
    w.source[0].result()
    w.source[0].finish()
    if guard == 'queue-error':
        if w.program == 'claude':
            w.source[0].add('queue-operation', operation='popAll')
        else:
            original = w.seat.harness.compactions
            def unresolved(path):
                reading = original(path)
                return replace(reading, state=replace(reading.state, queue_error='synthetic unresolved'))
            monkeypatch.setattr(w.seat.harness, 'compactions', unresolved)
    elif guard == 'draft':
        monkeypatch.setattr(w.seat.harness, 'screen', lambda _: Screen('synthetic', 'Synthetic draft'))
    elif guard == 'untimed':
        w.source[0].rows[-1]['timestamp'] = None
        w.source[0].flush()
    compaction.tick(w.seat)
    assert w.calls == []


def test_outside_cancel_works_when_down_and_never_closes_posts(world, monkeypatch):
    from quarterdeck import cli
    w = world
    _, hand, _ = ready(w)
    (w.seat.trusted/'launch.json').unlink()
    monkeypatch.delenv('QD_SEAT', raising=False)
    monkeypatch.setattr(cli, 'load_ship', lambda _: w.seat.ship)
    monkeypatch.setattr(cli, 'seat_of', lambda *_: w.seat)
    args = SimpleNamespace(seat=w.seat.name, cancel=True)
    assert 'cancelled' in cli.cmd_fresh_ready(args)
    data = compaction.load(w.svc.runtime, w.seat.name)
    assert data['ready'] is None and data['reserved'] is None and w.svc.store.is_open(hand)


@pytest.mark.parametrize('value', [None, 0])
def test_disabling_an_armed_revision_releases_without_closing(world, value):
    w = world
    _, hand, _ = ready(w)
    w.seat.spec = replace(w.seat.spec, fresh_after_compactions=value)
    compaction.tick(w.seat)
    data = compaction.load(w.svc.runtime, w.seat.name)
    assert data['disabled'] and data['ready'] is None and data['reserved'] is None
    assert w.svc.store.is_open(hand) and hand in w.svc.attempts.undelivered(w.svc.store, w.seat.name)
    assert w.calls == []


def test_proved_explicit_fresh_adopts_successor_without_a_second_launch(world):
    w = world
    _, hand, _ = ready(w)
    w.source[0].result()
    w.source[0].finish()
    w.seat.fresh()
    for _ in range(3):
        compaction.tick(w.seat)
    data = compaction.load(w.svc.runtime, w.seat.name)
    assert data['count'] == 0 and data['request'] is None and data['ready'] is None
    assert w.calls == ['down', 'fresh'] and hand in w.carried[-1] and w.svc.store.is_open(hand)


def test_other_native_origin_without_new_launch_is_unknown(world):
    w = world
    request = threshold(w)
    other = Native(w.seat.home/'synthetic-other-record', w.program, 'synthetic-other-origin')
    record_owner.register(w.seat.trusted, other.path, w.program)
    w.source[0] = other
    compaction.tick(w.seat)
    assert 'count unknown' in compaction.status(w.seat) and not w.calls
    assert [m for m in w.svc.store.for_seat(w.seat.name) if m.sender == post.SHIP] == [request]


@pytest.mark.parametrize('raw', ['{broken', '{"version":99}', '{"version":1,"generation":"synthetic"}'])
def test_unknown_journal_retained_and_never_defaulted_to_zero(world, raw):
    w = world
    path = compaction.path(w.svc.runtime, w.seat.name)
    path.write_text(raw)
    compaction.tick(w.seat)
    assert path.read_text() == raw and not w.calls
    assert 'held' in compaction.status(w.seat)


def direct_call(native, call='synthetic-direct-attestation'):
    native.add('response_item', payload=dict(type='function_call', name='exec_command', call_id=call,
               status='completed', arguments=json.dumps(dict(cmd='qd fresh-ready synthetic', yield_time_ms=10000))))
    return call


def direct_result(native, call, *, live=False):
    outcome = 'Process running with session ID 17' if live else 'Process exited with code 0'
    native.add('response_item', payload=dict(type='function_call_output', call_id=call,
               output=[dict(type='input_text', text=f'Wall time: 0.1000 seconds\n{outcome}\nOutput:\nSynthetic result')]))


def opaque_call(native):
    native.add('response_item', payload=dict(type='custom_tool_call', name='exec', call_id='synthetic-opaque',
               status='completed', input="text('Synthetic script')"))


def opaque_result(native):
    native.add('response_item', payload=dict(type='custom_tool_call_output', call_id='synthetic-opaque',
               output=[dict(type='input_text', text='Script completed\nSynthetic result')]))


@pytest.mark.parametrize('world', ['codex'], indirect=True)
@pytest.mark.parametrize('where', ['older', 'attestation'])
def test_managed_opaque_shapes_attest_then_hold_at_the_gate_with_explicit_fallback(world, where, monkeypatch):
    """An unknown tool state no longer stops the attestation or the arming. The gate reads it once, from the shared
    idle proof; with no screen to settle it, the seat is ready for manual fresh, and nothing starts by itself."""
    from quarterdeck import cli
    w = world
    native = w.source[0]
    if where == 'older':
        opaque_call(native)
        opaque_result(native)
        native.finish()
        native.begin()
    request = threshold(w)
    hand = handoff(w, request)
    if where == 'older':
        assert 'ready for manual fresh' not in compaction.status(w.seat), 'the protocol reads no tool state of its own'
        assert not any(message.kind == 'report' for message in w.svc.store.all())
        assert 'Leave that handoff open' in request.body and '--cancel' in request.body
    if where == 'attestation':
        opaque_call(native)
    else:
        direct_call(native)
    assert w.seat.harness.state(native.path).pending_tools is None, 'the record reads the tool state unknown'
    act = compaction.prepare_ready(w.seat, request.id, hand.id)
    compaction.arm(w.seat, **act)
    if where == 'attestation':
        opaque_result(native)
    else:
        direct_result(native, 'synthetic-direct-attestation')
    native.finish()
    compaction.tick(w.seat)
    status = compaction.status(w.seat)
    assert 'compactions 2/2: ready for manual fresh: execution unknown' in status and not w.calls
    assert compaction.load(w.svc.runtime, w.seat.name)['ready'] is not None, 'readiness stands; nothing is dropped'
    assert w.seat.harness.state(native.path).pending_tools is None
    print(f'synthetic managed {where}: {status}; automatic launches=0')
    # Explicit cancel and fresh do not pretend that opaque work was proved absent.
    compaction.disarm(w.seat, 'synthetic outside cancellation')
    monkeypatch.setattr(cli, 'seat_of', lambda *_: w.seat)
    cli.relaunch(w.seat.ship, w.seat.name, resume=False)
    assert w.calls == ['down', 'fresh'] and hand in w.carried[-1] and w.svc.store.is_open(hand)
    print('synthetic explicit cancel/fresh: open handoff carried, one explicit launch')


@pytest.mark.parametrize('world', ['codex'], indirect=True)
@pytest.mark.parametrize('result', ['terminal', 'yielded', 'wrong-turn'])
def test_direct_process_requires_native_lifetime_and_exact_completion(world, result):
    w = world
    native = w.source[0]
    request = threshold(w)
    hand = handoff(w, request)
    call = direct_call(native)
    act = compaction.prepare_ready(w.seat, request.id, hand.id)
    compaction.arm(w.seat, **act)
    direct_result(native, call, live=result == 'yielded')
    native.finish()
    if result == 'wrong-turn':
        native.rows[-1]['payload']['turn_id'] = 'synthetic-other-turn'
        native.flush()
    compaction.tick(w.seat)
    assert w.calls == (['down', 'fresh'] if result == 'terminal' else [])
    print(f'synthetic direct {result}: automatic launches={w.calls.count("fresh")}')


@pytest.mark.parametrize(('lost_reply', 'linger'), [
    pytest.param(False, 0, id='ordinary'),
    pytest.param(True, 0, id='lost-reply'),
    pytest.param(False, 20, id='after-down-bound'),
    pytest.param(False, 61, id='after-relaunch-bound'),
])
def test_measured_fake_program_uses_real_lifecycle_and_carries_open_handoff(world, monkeypatch, tmp_path, lost_reply, linger):
    import subprocess
    import sys
    from quarterdeck import cli, credential, durable, jail_end, relaunch_intent, seat as seatmod, service, shutdown
    from quarterdeck.harness.base import Launch

    w = world
    request = threshold(w)
    delivery = w.svc.attempts.open(request, w.seat.name, '%1', 123, AT.isoformat())
    w.svc.attempts.close(delivery, 'queued', 'synthetic original threshold delivery')
    w.source[0].begin(post.typed([request]))
    w.svc.reconcile()
    assert w.svc.attempts.latest()[request.id] == 'delivered'
    hand = handoff(w, request)
    if w.program == 'codex':
        call = direct_call(w.source[0])
    else:
        w.source[0].call()
    monkeypatch.setenv('QD_SEAT', w.seat.name)
    monkeypatch.setattr(cli, 'load_ship', lambda _: w.seat.ship)
    monkeypatch.setattr(cli, 'seat_of', lambda *_: w.seat)
    monkeypatch.setattr(w.svc, 'seat', lambda _: w.seat)
    monkeypatch.setattr(service, 'alive', lambda _: True)
    def control(runtime, kind, **data):
        folder = runtime/'requests'
        folder.mkdir(exist_ok=True)
        path = folder/'synthetic-fresh-ready.json'
        durable.replace_file(path, json.dumps(dict(kind=kind, **data))+'\n')
        w.svc.requests()
        return path.with_suffix('.done').read_text()
    monkeypatch.setattr(service, 'request', control)
    args = SimpleNamespace(seat=w.seat.name, cancel=False, at_break=True, request=request.id, handoff=hand.id)
    assert 'waiting for exact turn completion' in cli.cmd_fresh_ready(args)

    # Restore the real Seat lifecycle/join. Replace only credentials, adapter launch command
    # and the synthetic pane transport. The successor process writes its own native record.
    live = [w.seat.pane()]
    launches, retired, first_messages, late = [], [], [], []
    up_attempts = []
    old = replace(w.seat.launched(), start='synthetic-start')
    old.write(w.seat.trusted/'launch.json')
    tenths = 0

    def sleep(seconds):
        nonlocal tenths
        tenths += round(seconds * 10)

    def old_alive(pid, start):
        assert (pid, start) == (old.pid, old.start)
        return tenths < linger * 10

    fake_time = SimpleNamespace(monotonic=lambda: tenths / 10, sleep=sleep)
    monkeypatch.setattr(seatmod, 'process_alive', old_alive)
    monkeypatch.setattr(jail_end, 'snapshot', lambda launch: {})
    monkeypatch.setattr(w.seat, 'record', lambda: Seat.record(w.seat))
    monkeypatch.setattr(w.seat, 'join', lambda: Seat.join(w.seat))

    def down():
        with monkeypatch.context() as clock:
            clock.setattr(seatmod, 'time', fake_time)
            return Seat._down(w.seat)

    monkeypatch.setattr(w.seat, '_down', down)
    def up(resume=True, *, observation=None, target=None):
        up_attempts.append(resume)
        proof = Seat._up(w.seat, resume)
        if lost_reply:
            raise OSError('synthetic crash after durable successor launch')
        return proof
    monkeypatch.setattr(w.seat, '_up', up)
    monkeypatch.setattr(credential, 'reseed', lambda *_, **bounds: None)
    monkeypatch.setattr(w.seat.harness, 'build_home', lambda *_: None)
    monkeypatch.setattr(w.seat.harness, 'version', lambda: 'synthetic')
    monkeypatch.setattr(w.seat.tmux, 'pane', lambda key: live[0] if live[0] is not None and live[0].id == key else None)
    def kill(key, expected_pid):
        assert (key, expected_pid) == (live[0].id, live[0].pid)
        intent = relaunch_intent.read(w.svc.runtime, f'{w.seat.name}.json', {w.seat.name})
        assert intent['kind'] == 'compaction' and intent['resume'] is False
        retired.append((key, expected_pid))
        live[0] = None
    monkeypatch.setattr(w.seat.tmux, 'kill_window', kill, raising=False)
    fake = tmp_path/'synthetic-program.py'
    fake.write_text('''from datetime import datetime, timezone
import hashlib, json, sys
from pathlib import Path
template, target, prompt, brief = map(Path, sys.argv[1:])
text = prompt.read_text()
rows = [json.loads(line) for line in template.read_text().splitlines()]
for row in rows:
    row['timestamp'] = datetime.now(timezone.utc).isoformat()
    if row['type'] == 'user':
        row['message']['content'] = text
    if row['type'] == 'response_item' and row['payload'].get('role') == 'user':
        row['payload']['content'] = [dict(type='input_text', text=text)]
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(''.join(json.dumps(row)+'\\n' for row in rows))
print(json.dumps(dict(first_sha256=hashlib.sha256(text.encode()).hexdigest(), brief=bool(brief.read_text()))), flush=True)
sys.stdin.read(1)
''')
    template = Native(tmp_path/'prepared-native.jsonl', w.program, 'synthetic-fresh-successor')
    target = (w.seat.home/'projects/synthetic/synthetic-successor.jsonl' if w.program == 'claude'
              else w.seat.home/'sessions/2030/01/01/rollout-synthetic-successor.jsonl')
    def launch(spec, pool, home, brief, resume, first):
        assert resume is None and first is not None and post.typed([hand]) in first
        first_messages.append(first)
        prompt = tmp_path/'synthetic-first.txt'
        prompt.write_text(first)
        return Launch((sys.executable, str(fake), str(template.path), str(target), str(prompt), str(brief)), {})
    monkeypatch.setattr(w.seat.harness, 'launch', launch)
    def open_window(argv, env, path):
        assert not old_alive(old.pid, old.start)
        late.append(w.svc.store.new('answer', 'operator', w.seat.name, 'Synthetic arrival after the launch snapshot'))
        assert post.typed(late) not in first_messages[-1]
        assert w.svc.attempts.undelivered(w.svc.store, w.seat.name) == []
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        launches.append(process)
        proof = json.loads(process.stdout.readline())
        import hashlib
        assert proof['brief'] and proof['first_sha256'] == hashlib.sha256(first_messages[-1].encode()).hexdigest()
        live[0] = SimpleNamespace(id='%2', pid=process.pid, dead=process.poll() is not None)
        return live[0]
    monkeypatch.setattr(w.seat, '_open_window', open_window)
    try:
        compaction.tick(w.seat)
        assert not launches and not retired
        if w.program == 'codex':
            direct_result(w.source[0], call)
        else:
            w.source[0].result()
        w.source[0].finish()
        with shutdown.Stop(w.svc.runtime).installed():
            compaction.tick(w.seat)
        assert not relaunch_intent.path(w.svc.runtime, w.seat.name).exists()
        if linger > seatmod.RELAUNCH_END_SECONDS:
            assert tenths == seatmod.RELAUNCH_END_SECONDS * 10
            assert not up_attempts and not launches
            assert not w.seat.is_up and w.seat.launched() == old
            assert old_alive(old.pid, old.start)
            assert f'{w.seat.name}: its program is still ending; its relaunch was refused' in watch.facts(w.seat.ship, w.seat.tmux).line()
            tenths = linger * 10
            assert w.seat.finish_retirement()
            w.svc.relaunch_intents()
            compaction.tick(w.seat)
            assert not launches and not w.seat.is_up and w.seat.launched() is None
            assert w.seat.retiring() is None and w.svc.store.is_open(hand)
            line = watch.facts(w.seat.ship, w.seat.tmux).line()
            assert f'{w.seat.name}: its relaunch was refused while its program was ending; qd up {w.seat.name}' in line
            return
        assert tenths == linger * 10
        assert up_attempts == [False]
        assert len(launches) == len(retired) == 1 and launches[0].poll() is None
        assert w.seat.record() == target
        assert w.svc.attempts.latest()[hand.id] == 'launch' and w.svc.store.is_open(hand)
        restarted = Service(w.seat.ship, w.seat.tmux)
        try:
            monkeypatch.setattr(restarted, 'resolved_seats', lambda: [w.seat])
            for _ in range(3):
                restarted.compactions()
            assert compaction.load(w.svc.runtime, w.seat.name)['count'] == 0
            assert len(launches) == 1 and restarted.attempts.undelivered(restarted.store, w.seat.name) == late
        finally:
            restarted.ledger.close()
        assert len([m for m in w.svc.store.all() if m.sender == post.SHIP]) == 1
        assert len([m for m in w.svc.store.all() if m.kind == 'report']) == 1
        print(f'{w.program} fake launch (lost_reply={lost_reply}): request=1 attestation=1 fresh_process=1 open_handoff_launch_receipt=1 successor_count=0 late_successor_unread=1')
    finally:
        for process in launches:
            process.communicate(timeout=5)


# -- the screen, read second at the gate ------------------------------------------------------------------------

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures' / 'claude' / '2.1.317'


def unknown_record(w, monkeypatch, shown):
    """A Claude seat on the measured 2.1.317 whose record cannot say whether a tool is open once its turn ends,
    the protocol's own reading included, and whose screen is the named measured capture."""
    from quarterdeck.harness.types import ActiveTool
    harness = w.seat.harness
    state, compactions = harness.state, harness.compactions
    screen = type(harness).screen.__get__(harness)  # the adapter's own reading, under the world's stand-in

    def unknown(found):
        if found is None or found.turn_open is not False:
            return found
        return replace(found, pending_tools=None, active_tools=(ActiveTool('Bash', AT),))

    monkeypatch.setattr(harness, 'state', lambda record: unknown(state(record)))
    monkeypatch.setattr(harness, 'compactions', lambda record: (lambda reading: replace(
        reading, state=unknown(reading.state)) if reading.state is not None else reading)(compactions(record)))
    monkeypatch.setattr(harness, 'screen', lambda _: screen((FIXTURES / shown[0]).read_text()))
    Launched('%1', 123, AT.isoformat(), 'synthetic', None, ['claude'], '2.1.317', harness='claude').write(
        w.seat.trusted / 'launch.json')


@pytest.mark.parametrize('world', ['claude'], indirect=True)
def test_compacted_twice_record_unknown_and_screen_at_rest_goes_fresh_by_itself(world, monkeypatch):
    from quarterdeck import idle
    w = world
    unknown_record(w, monkeypatch, ['rest-nothing-running.txt'])
    _, hand, _ = ready(w)
    w.source[0].result()
    w.source[0].finish()
    assert w.seat.harness.state(w.source[0].path).pending_tools is None, 'the record alone cannot say'
    compaction.tick(w.seat)
    assert w.calls == ['down', 'fresh'] and hand in w.carried[-1] and w.svc.store.is_open(hand)
    assert compaction.load(w.svc.runtime, w.seat.name)['transition']['witness'] == idle.RECORD_AND_SCREEN


@pytest.mark.parametrize('world', ['claude'], indirect=True)
def test_compacted_twice_record_unknown_and_screen_showing_work_stays_held_with_the_reason(world, monkeypatch):
    w = world
    shown = ['rest-one-shell-one-monitor.txt']
    unknown_record(w, monkeypatch, shown)
    _, hand, _ = ready(w)
    w.source[0].result()
    w.source[0].finish()
    for _ in range(2):
        compaction.tick(w.seat)
    data = compaction.load(w.svc.runtime, w.seat.name)
    assert not w.calls and data['ready'] is not None and data['reserved'] == hand.id
    assert data['manual'] is None, 'the screen settled the tool state: work, not unknown'
    assert compaction.status(w.seat) == ('compactions 2/2: tools pending: its screen shows 1 shell and 1 monitor '
                                         'in the background; automatic fresh held')
    shown[0] = 'rest-nothing-running.txt'  # the work ends; the same readiness goes at the next gate
    compaction.tick(w.seat)
    assert w.calls == ['down', 'fresh'] and hand in w.carried[-1]


@pytest.mark.parametrize('phase', UNSETTLED)
def test_ready_compaction_waits_for_an_intentless_resting_move(world, phase):
    w = world
    ready(w)
    w.source[0].result()
    w.source[0].finish()
    data = compaction.load(w.svc.runtime, w.seat.name)
    record, reading, _ = compaction.current(w.seat)
    assert compaction.obstruction(w.seat, data, record, reading) is None
    _, attempt = save_attempt(w.seat, phase)
    assert 'qd up worker' in compaction.obstruction(w.seat, data, record, reading)
    compaction.tick(w.seat)
    assert not w.calls and compaction.load(w.svc.runtime, w.seat.name)['ready'] == data['ready']
    settle(w.seat, attempt)
    compaction.tick(w.seat)
    assert w.calls == ['down', 'fresh']
