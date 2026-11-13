"""The person's quiet time in a human session, read from the record, and its one wake per readiness.

Everything is synthetic: Claude Code row shapes built here, nonhex identities, no real record read.
"""

from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from quarterdeck import compaction, ledger, person_quiet, post, ship as shipmod, watch
from quarterdeck.harness.base import Join
from quarterdeck.seat import Launched, Seat
from quarterdeck.ship import Pool, SeatSpec, Session, Ship
from tests.compaction_shapes import AT, Native

LINE = 'qd > ship: synthetic waking line.'
READY = 'ready for manual fresh: human-owned session; choose an idle break'


def stamp(seconds):
    return (AT + timedelta(seconds=seconds)).isoformat()


@pytest.fixture
def desk(tmp_path, monkeypatch):
    def make(harness='claude', human=True, role='desk'):
        spec = SeatSpec('desk', 'office', role, harness, ('synthetic',), fresh_after_compactions=2)
        ship = Ship(tmp_path / 'ship', 'synthetic', 'operator', tmp_path, {'synthetic': Pool('synthetic', harness)},
                    {'office': Session('office', 'Synthetic office', human)}, {'desk': spec})
        tmux = SimpleNamespace(pane=lambda _: SimpleNamespace(id='%1', pid=123, dead=False))
        seat = Seat(ship, spec, tmux)
        seat.folder.mkdir(parents=True)
        native = Native(seat.home / 'synthetic-record', harness if harness in ('claude', 'codex') else 'claude')
        launch(seat)
        monkeypatch.setattr(seat, 'record', lambda: native.path)
        monkeypatch.setattr(seat, 'join', lambda: Join(native.path))
        monkeypatch.setattr(person_quiet, 'waking_lines', lambda: (LINE,))
        return SimpleNamespace(seat=seat, native=native, store=post.Store(ship.runtime))
    return make


def launch(seat, at=AT):
    Launched('%1', 123, at.isoformat(), 'synthetic', None, [seat.spec.harness], 'synthetic',
             harness=seat.spec.harness).write(seat.folder / 'launch.json')


def ready(seat, generation='synthetic-generation-a', manual=READY, **changes):
    data = compaction.new()
    data.update(generation=generation, count=2, reason='manual-only readiness', manual=manual)
    data.update(changes)
    compaction.save(seat.ship.runtime, seat.name, data)


def notice(native):
    """A background task's end, in each carrier the program writes it in."""
    body = ('<task-notification>\n<task-id>ghjkmnpqr</task-id>\n<tool-use-id>toolu_synthetic_quill</tool-use-id>\n'
            '<status>completed</status>\n<summary>Synthetic</summary>\n</task-notification>')
    native.add('queue-operation', operation='enqueue', content=body)
    native.add('user', message=dict(role='user', content=body),
               origin=dict(kind='task-notification', producer='session-task', runId='synthetic-run'))
    native.add('attachment', attachment=dict(type='queued_command', commandMode='task-notification', prompt=body,
                                             origin=dict(kind='task-notification'), timestamp=stamp(native.serial + 1)))


def turn(native, text='Synthetic person input', **fields):
    native.add('user', message=dict(role='user', content=text), **fields)
    native.finish()


def latest(native):
    """The time of the newest row written so far."""
    return AT + timedelta(seconds=native.serial)


def tick(w, now):
    return person_quiet.tick(w.seat, w.store, now=now)


def wake_orders(w):
    return [m for m in w.store.for_seat('desk') if m.kind == 'order' and m.body == person_quiet.ORDER]


# -- the reading -------------------------------------------------------------------------------------

def test_notices_and_the_waking_line_after_the_persons_input_do_not_advance_the_quiet_clock(desk):
    w = desk()
    w.native.finish()
    typed = w.native.serial + 1
    turn(w.native, origin=dict(kind='human'), promptSource='typed')
    notice(w.native)
    w.native.finish()
    turn(w.native, LINE, origin=dict(kind='human'), promptSource='typed')
    w.native.add('queue-operation', operation='enqueue', content=LINE)
    summary = w.native.add('user', message=dict(role='user', content='Synthetic summary'), isCompactSummary=True)
    assert summary
    fact = person_quiet.read(w.seat, latest(w.native))
    assert fact['at'] == stamp(typed) and fact['reason'] is None


def test_input_typed_while_the_seat_works_counts_as_the_person(desk):
    w = desk()
    w.native.finish()
    w.native.add('queue-operation', operation='enqueue', content='Synthetic typed while busy')
    queued = w.native.serial
    assert person_quiet.read(w.seat, latest(w.native))['at'] == stamp(queued)
    w.native.add('attachment', attachment=dict(type='queued_command', commandMode='prompt', prompt='Synthetic typed while busy',
                                               origin=dict(kind='human'), timestamp=stamp(w.native.serial + 1)))
    assert person_quiet.read(w.seat, latest(w.native))['at'] == stamp(w.native.serial), 'taken in later: the later time wins'


@pytest.mark.parametrize('text', [LINE + ' extra', 'Synthetic ' + LINE, LINE.upper()])
def test_only_the_exact_waking_line_is_left_out(desk, text):
    w = desk()
    w.native.finish()
    turn(w.native, text)
    assert person_quiet.read(w.seat, latest(w.native))['at'] == stamp(w.native.serial - 2)


def test_an_unrecognized_origin_counts_as_the_person(desk):
    w = desk()
    w.native.finish()
    turn(w.native, 'Synthetic remote input', origin=dict(kind='synthetic-other-origin'))
    assert person_quiet.read(w.seat, latest(w.native))['at'] == stamp(w.native.serial - 2)


@pytest.mark.parametrize('harness', ['codex', 'fake'])
def test_an_adapter_that_declares_no_notices_reads_unknown(desk, harness):
    w = desk(harness)
    fact = person_quiet.read(w.seat, latest(w.native))
    assert fact['at'] is None and fact['reason'] == f'{harness} declares no program notices'


def test_unknown_readings_say_why_and_never_read_quiet(desk, monkeypatch):
    w = desk()
    w.native.finish()
    now = latest(w.native)
    w.native.rows.append(dict(type='user', message=dict(role='user', content='Synthetic untimed'),
                              uuid='synthetic-untimed', parentUuid=w.native.previous, sessionId='synthetic-conversation'))
    w.native.flush()
    assert person_quiet.read(w.seat, now) == person_quiet._unknown(compaction.key_for(w.seat.launched()), 'an input row has no time')
    w.native.rows.pop()
    w.native.flush()
    with w.native.path.open('a') as handle:
        handle.write('{"type": "user", "unterminated\n')
    assert person_quiet.read(w.seat, now)['reason'] == 'record has unreadable rows'
    w.native.flush()
    assert person_quiet.read(w.seat, AT - timedelta(minutes=1))['reason'] == 'a recorded time is in the future'
    monkeypatch.setattr(w.seat, 'record', lambda: None)
    assert person_quiet.read(w.seat, now)['reason'] == 'no record joined'
    (w.seat.folder / 'launch.json').unlink()
    assert person_quiet.read(w.seat, now)['reason'] == 'seat not launched'


def test_a_reader_error_never_shows_record_text(desk, monkeypatch):
    w = desk()
    monkeypatch.setattr(w.seat.harness, 'person_inputs', lambda *a: (_ for _ in ()).throw(ValueError('Synthetic private words')))
    assert person_quiet.read(w.seat, latest(w.native))['reason'] == 'record could not be classified'


# -- the fact, and what watch and status show ------------------------------------------------------------

def test_status_and_watch_read_only_the_kept_fact(desk, monkeypatch):
    w = desk()
    w.native.finish()
    person_quiet.observe(w.seat, latest(w.native))
    def never(*args, **kwargs):
        raise AssertionError('the conversation was read')
    monkeypatch.setattr(w.seat, 'record', never)
    monkeypatch.setattr(w.seat, 'join', never)
    monkeypatch.setattr(w.seat.harness, 'person_inputs', never)
    monkeypatch.setattr(person_quiet, 'read', never)
    shown = person_quiet.summary(w.seat, now=AT + timedelta(minutes=23, seconds=1))
    assert shown == dict(text='person quiet for 23m', last_input_at=stamp(1), quiet_seconds=23 * 60, reason=None)
    row = SimpleNamespace(name='desk', activity='idle', door='open', order_text='-', today=ledger.Count(), harness_text='claude',
                          provider_name='Synthetic', pool='synthetic', model_text='-', effort_text='-', compaction=None, wake=None,
                          person_quiet=shown)
    full = watch.render_full(dict(seat=row, record=None, state=None, record_absent=None, record_limits=None,
                                  receipt_error=None, receipt_skips=None, screen=None, usage=[]))
    assert '  person quiet for 23m' in full
    table = watch.render_table(dict(service='ok', seats=[row], open_orders=[], usage=[]))
    assert not any('quiet' in line for line in table), 'the status table keeps one row per seat; status carries the reading in its JSON'


def test_unknown_is_shown_with_its_reason_and_a_new_launch_drops_the_old_fact(desk):
    w = desk('codex')
    person_quiet.observe(w.seat, latest(w.native))
    assert person_quiet.summary(w.seat)['text'] == "person's quiet time unknown: codex declares no program notices"
    launch(w.seat, AT + timedelta(hours=1))
    assert person_quiet.summary(w.seat)['text'] == "person's quiet time unknown: reading belongs to an earlier launch"
    (w.seat.folder / person_quiet.FACT).write_text(json.dumps(dict(version=1, launch=None, at=None, reason='Synthetic words')))
    assert person_quiet.fact(w.seat)['reason'] in ('reading belongs to an earlier launch', 'reading unreadable')


def test_an_unchanged_fact_is_not_rewritten(desk):
    w = desk()
    w.native.finish()
    person_quiet.observe(w.seat, latest(w.native))
    path = w.seat.folder / person_quiet.FACT
    before = path.stat().st_ino, path.stat().st_mtime_ns
    person_quiet.observe(w.seat, latest(w.native) + timedelta(minutes=5))
    assert (path.stat().st_ino, path.stat().st_mtime_ns) == before


def test_twenty_minutes_is_a_constant_and_not_a_ship_key():
    assert person_quiet.QUIET == timedelta(minutes=20)
    assert not any('quiet' in key for key in shipmod.SEAT_KEYS)
    assert person_quiet.ORDER == ('The person has been quiet for twenty minutes and you are ready: '
                                  'post your handoff if you have not, then run qd fresh now.')


# -- the wake --------------------------------------------------------------------------------------

def quiet_desk(desk):
    w = desk()
    w.native.finish()
    ready(w.seat)
    return w, AT + timedelta(seconds=1)


def test_a_ready_idle_desk_quiet_twenty_minutes_gets_exactly_one_order(desk):
    w, typed = quiet_desk(desk)
    first = tick(w, typed + timedelta(minutes=20))
    assert first and [m.id for m in wake_orders(w)] == [first]
    order = w.store.get(first)
    assert (order.sender, order.to, order.kind) == (post.SHIP, 'desk', 'order')
    for minutes in (21, 25, 60, 600):
        assert tick(w, typed + timedelta(minutes=minutes)) is None
    assert len(wake_orders(w)) == 1


def test_nineteen_minutes_gets_none(desk):
    w, typed = quiet_desk(desk)
    assert tick(w, typed + timedelta(minutes=19, seconds=59)) is None
    assert wake_orders(w) == [] and not (w.seat.folder / person_quiet.WAKE).exists()


def test_the_wake_waits_for_readiness(desk):
    w = desk()
    w.native.finish()
    later = AT + timedelta(hours=2)
    ready(w.seat, manual=None, reason='below compaction threshold')
    assert tick(w, later) is None
    ready(w.seat, reading_unknown=True)
    assert tick(w, later) is None
    ready(w.seat, transition=dict(source=None))
    assert tick(w, later) is None
    assert wake_orders(w) == []


def test_unknown_quiet_never_wakes(desk):
    w = desk('codex')
    ready(w.seat)
    assert tick(w, AT + timedelta(hours=5)) is None and wake_orders(w) == []


@pytest.mark.parametrize('busy', ['turn', 'queued', 'tools'])
def test_a_busy_seat_gets_none_and_busy_never_rearms(desk, busy):
    w, typed = quiet_desk(desk)
    before = w.native.rows[:]
    def make_busy():
        if busy == 'turn':  # a task's notice opens a turn without being the person
            w.native.add('user', message=dict(role='user', content='<task-notification>\n<status>completed</status>\n</task-notification>'),
                         origin=dict(kind='task-notification', producer='session-task', runId='synthetic-run'))
        elif busy == 'queued':
            w.native.rows.append(dict(type='queue-operation', operation='enqueue', content='Synthetic queued',
                                      timestamp=stamp(-1), sessionId='synthetic-conversation'))
            w.native.flush()
        else:
            w.native.add('assistant', message=dict(id='synthetic-call', model='synthetic', stop_reason='tool_use',
                         content=[dict(type='tool_use', id='toolu_synthetic_bg', name='Bash',
                                       input=dict(command='qd inbox --wait', run_in_background=True))]))
    make_busy()
    assert tick(w, typed + timedelta(minutes=30)) is None and wake_orders(w) == []
    w.native.rows[:] = before
    w.native.flush()
    first = tick(w, typed + timedelta(minutes=31))
    assert first
    make_busy()
    assert tick(w, typed + timedelta(minutes=40)) is None
    w.native.rows[:] = before
    w.native.flush()
    assert tick(w, typed + timedelta(minutes=41)) is None, 'busy never clears the readiness already woken'
    assert len(wake_orders(w)) == 1


def test_unknown_readiness_never_rearms_but_a_clear_one_or_a_new_readiness_does(desk):
    w, typed = quiet_desk(desk)
    assert tick(w, typed + timedelta(minutes=20))
    ready(w.seat, reading_unknown=True)
    assert tick(w, typed + timedelta(minutes=21)) is None
    ready(w.seat)
    assert tick(w, typed + timedelta(minutes=22)) is None and len(wake_orders(w)) == 1
    ready(w.seat, manual=None, reason='below compaction threshold')
    assert tick(w, typed + timedelta(minutes=23)) is None and not (w.seat.folder / person_quiet.WAKE).exists()
    ready(w.seat)
    assert tick(w, typed + timedelta(minutes=24)) and len(wake_orders(w)) == 2
    ready(w.seat, generation='synthetic-generation-b')
    assert tick(w, typed + timedelta(minutes=25)) and len(wake_orders(w)) == 3


def test_a_new_launch_is_a_new_readiness(desk):
    w, typed = quiet_desk(desk)
    assert tick(w, typed + timedelta(minutes=20))
    launch(w.seat, AT + timedelta(seconds=30))
    assert tick(w, typed + timedelta(minutes=21)) and len(wake_orders(w)) == 2


def test_an_interrupted_pass_adopts_its_posted_order_and_never_posts_twice(desk, monkeypatch):
    w, typed = quiet_desk(desk)
    saves = []
    real = person_quiet._save_wake
    def crash_after_post(seat, data):
        saves.append(dict(data))
        if data.get('published'):
            raise OSError('synthetic crash after the post')
        real(seat, data)
    monkeypatch.setattr(person_quiet, '_save_wake', crash_after_post)
    with pytest.raises(OSError):
        tick(w, typed + timedelta(minutes=20))
    monkeypatch.setattr(person_quiet, '_save_wake', real)
    assert len(wake_orders(w)) == 1
    adopted = tick(w, typed + timedelta(minutes=21))
    assert adopted == wake_orders(w)[0].id and len(wake_orders(w)) == 1
    kept = json.loads((w.seat.folder / person_quiet.WAKE).read_text())
    assert kept['order'] == adopted and kept['published'] is True
    assert tick(w, typed + timedelta(minutes=22)) is None and len(wake_orders(w)) == 1


def test_an_intent_without_its_post_posts_once(desk, monkeypatch):
    w, typed = quiet_desk(desk)
    def crash_before_post(*args, **kwargs):
        raise OSError('synthetic crash before the post')
    original = w.store.write
    monkeypatch.setattr(w.store, 'write', crash_before_post)
    with pytest.raises(OSError):
        tick(w, typed + timedelta(minutes=20))
    monkeypatch.setattr(w.store, 'write', original)
    named = json.loads((w.seat.folder / person_quiet.WAKE).read_text())['order']
    assert named and wake_orders(w) == []
    assert tick(w, typed + timedelta(minutes=20, seconds=30)) and len(wake_orders(w)) == 1
    assert tick(w, typed + timedelta(minutes=30)) is None and len(wake_orders(w)) == 1


def test_an_unreadable_wake_journal_posts_nothing(desk):
    w, typed = quiet_desk(desk)
    (w.seat.folder / person_quiet.WAKE).write_text('{')
    with pytest.raises(shipmod.ShipError):
        tick(w, typed + timedelta(minutes=30))
    assert wake_orders(w) == []


def test_a_seat_outside_a_human_session_is_never_read_or_woken(desk):
    w = desk(human=False)
    w.native.finish()
    ready(w.seat)
    assert tick(w, AT + timedelta(hours=1)) is None
    assert wake_orders(w) == [] and not (w.seat.folder / person_quiet.FACT).exists()


def test_an_office_seat_that_is_not_the_desk_is_read_but_never_woken(desk):
    w = desk(role='counsel')
    w.native.finish()
    ready(w.seat)
    typed = AT + timedelta(seconds=1)
    for minutes in (20, 30, 120):
        assert tick(w, typed + timedelta(minutes=minutes)) is None
    assert wake_orders(w) == [] and not (w.seat.folder / person_quiet.WAKE).exists()
    assert person_quiet.summary(w.seat, now=typed + timedelta(minutes=20))['text'] == 'person quiet for 20m'


# -- the compositions with the waking line and the idle proof ---------------------------------------

def test_the_waking_line_left_out_is_the_ships_own():
    from quarterdeck import human_wake
    assert person_quiet.waking_lines() == (human_wake.LINE,)


def watching(w, *, other=False):
    """The desk's own background inbox watch, still running in its closed turn; with another tool beside it."""
    calls = [('toolu_synthetic_watch', 'qd inbox --wait', 'bkwatchq')]
    if other:
        calls.append(('toolu_synthetic_other', 'sleep 600', 'bkothrz'))
    measured = '2.1.317'  # the version the background watch's rows were measured on
    w.native.add('assistant', version=measured, message=dict(id='synthetic-call', model='synthetic', stop_reason='tool_use', content=[
        dict(type='tool_use', id=identity, name='Bash', input=dict(command=command, run_in_background=True))
        for identity, command, _ in calls]))
    for identity, _, task in calls:
        w.native.add('user', version=measured, message=dict(role='user', content=[dict(type='tool_result', tool_use_id=identity,
                     content='synthetic handle', is_error=False)]),
                     toolUseResult=dict(stdout='', stderr='', interrupted=False, isImage=False,
                                        noOutputExpected=False, backgroundTaskId=task))
    w.native.finish()


@pytest.mark.parametrize('proved', [True, False])
def test_only_the_desks_own_proved_watch_is_let_through(desk, monkeypatch, proved):
    from quarterdeck import inbox_watch
    seen = []
    monkeypatch.setattr(inbox_watch, 'current', lambda seat, at: seen.append(seat.name) or proved)
    w = desk()
    watching(w)
    ready(w.seat)
    posted = tick(w, latest(w.native) + timedelta(hours=1))
    assert seen == ['desk']
    assert bool(posted) is proved and len(wake_orders(w)) == int(proved)


def test_a_proved_watch_beside_another_tool_still_holds(desk, monkeypatch):
    from quarterdeck import inbox_watch
    monkeypatch.setattr(inbox_watch, 'current', lambda seat, at: True)
    w = desk()
    watching(w, other=True)
    ready(w.seat)
    assert tick(w, latest(w.native) + timedelta(hours=1)) is None and wake_orders(w) == []
