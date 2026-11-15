"""The screen as the idle proof's second witness, where the record cannot say whether a tool is open.

The record is read first and its reading is unchanged. Only where it shows the turn ended, nothing queued and its
last input complete, and still cannot say whether a tool is open, the screen is read: the composer at rest, and the
program's own bottom line in a form measured on the launched version. Nothing named there is rest; one shell is
rest only where it is the seat's registered watch alone; anything else named is work, held under its name; a screen
that does not say settles nothing, and the hold says what it lacked (law: record-first).

Synthetic states and synthetic screens of the measured shapes only. The watch's own proof from the process table
has its tests in test_inbox_watch_alone.py; here it is a given.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from quarterdeck import door, idle, inbox_watch, move_hold, person_quiet, quota, recovery, watch
from quarterdeck.harness.claude import ClaudeHarness
from quarterdeck.harness.types import ActiveTool, State, Tokens, Turn
from quarterdeck.service import Service

AT = datetime(2032, 4, 5, 6, 7, tzinfo=timezone.utc)
VERSION = '2.1.317'
RULE = '─' * 100
MODE = '  ⏵⏵ bypass permissions on'
NOTHING = MODE + ' (shift+tab to cycle)'
ONE_SHELL = MODE + ' · 1 shell'
UNKNOWN = idle.Hold('tools-unknown', 'tool state unknown')


def text(bottom, *below, box='❯\xa0', sign=None):
    lines = ['', '● synthetic reply fern', '', *([sign, ''] if sign else []), RULE, box, RULE,
             '  Seat Fern  role · Model 0.0 · effort · context 5%', bottom, *below]
    return '\n'.join(lines) + '\n'


def unread(**changes):
    """A record that shows the turn ended and its input complete, and cannot say whether a tool is open."""
    turn = Turn(0, AT, AT + timedelta(seconds=5), 'synthetic input', 'synthetic reply', None, Tokens())
    state = State(turn_open=False, pending_tools=None, last_input_at=AT, completed_turns=(turn,),
                  active_tools=(ActiveTool('Bash', AT),))
    return replace(state, **changes)


class Case:
    def __init__(self, tmp_path, monkeypatch, state, shown, *, alone=False, version=VERSION, human=False):
        self.state, self.shown, self.alone, self.reads, self.moved = state, shown, alone, 0, []
        self.launch = SimpleNamespace(pane='%pane-fern', pid=101001, at=AT.isoformat(), harness='claude',
                                      version=version)
        native = ClaudeHarness()
        self.seat = SimpleNamespace(
            name='seat-fern', folder=tmp_path / 'seats' / 'seat-fern',
            ship=SimpleNamespace(runtime=tmp_path), session=SimpleNamespace(human=human),
            spec=SimpleNamespace(pools=('pool-willow', 'pool-oak'), pool_choices=True),
            pool=SimpleNamespace(name='pool-willow'), is_up=True,
            launched=lambda: self.launch, record=lambda: tmp_path / 'record-fern.jsonl',
            pane=lambda: SimpleNamespace(dead=False, id=self.launch.pane, pid=self.launch.pid),
            harness=SimpleNamespace(state=lambda record: self.state, waiting_for_human=lambda reading: False,
                                    background=native.background, background_versions=native.background_versions),
            failover_to=lambda pool: self.moved.append(pool) or 'synthetic move proof',
            restart=lambda: self.moved.append('restart') or 'synthetic restart proof',
            fresh=lambda: self.moved.append('fresh') or 'synthetic fresh proof')
        case = self

        class Door:
            def __init__(self, seat):
                assert seat is case.seat

            def screen(self):
                case.reads += 1
                if isinstance(case.shown, BaseException):
                    raise case.shown
                return native.screen(case.shown) if case.shown is not None else None

        monkeypatch.setattr(door, 'Door', Door)
        monkeypatch.setattr(inbox_watch, 'alone', lambda seat: self.alone)

    def second(self, **options):
        return idle.second(self.seat, self.state, UNKNOWN, **options)

    def service(self, monkeypatch):
        service = Service.__new__(Service)
        service.ledger = object()
        service.store = SimpleNamespace(new=lambda *args: None)
        service.log = lambda *args, **kwargs: None
        service.resolved_seats = lambda: [self.seat]
        service.known_open = lambda target, pool: True
        monkeypatch.setattr(recovery, 'held', lambda target: None)
        monkeypatch.setattr(quota, 'current_walled', lambda *args: True)
        monkeypatch.setattr(quota, 'candidate_pools', lambda target: ['pool-oak'])
        return service


@pytest.fixture
def case(tmp_path, monkeypatch):
    return lambda state, shown, **options: Case(tmp_path, monkeypatch, state, shown, **options)


# -- what the screen settles -----------------------------------------------------------------------------------

@pytest.mark.parametrize(('bottom', 'alone', 'saw'), [
    (NOTHING, False, 'nothing in the background'),
    (NOTHING, True, 'nothing in the background'),
    (ONE_SHELL, True, 'only its inbox watch in the background'),
])
def test_a_resting_screen_settles_the_tool_state_the_record_could_not(case, bottom, alone, saw):
    built = case(unread(), text(bottom), alone=alone)
    state, hold, shown = built.second()
    assert hold is None and shown == saw
    assert state.pending_tools == () and state.active_tools == ()
    assert state == replace(built.state, pending_tools=(), active_tools=()), 'nothing else of the record changes'
    assert idle.check(state) is None
    assert built.reads == 1


@pytest.mark.parametrize(('bottom', 'alone', 'saw'), [
    (ONE_SHELL, False, '1 shell in the background, not proved to be its inbox watch alone'),
    (MODE + ' · 2 shells', True, '2 shells in the background'),
    (MODE + ' · 2 shells', False, '2 shells in the background'),
    (MODE + ' · 1 shell, 1 monitor', True, '1 shell and 1 monitor in the background'),
    (MODE + ' · 12 shells', True, '12 shells in the background'),
])
def test_anything_else_the_screen_names_is_work_held_under_its_name(case, bottom, alone, saw):
    built = case(unread(), text(bottom), alone=alone)
    state, hold, shown = built.second()
    assert state is built.state, 'the state is left as the record gave it'
    assert hold == idle.Hold('tools-pending', f'tools pending: its screen shows {saw}') and shown == saw


@pytest.mark.parametrize(('shown', 'lacked'), [
    (text(MODE + ' · 1 shell, 1 agent'), "its screen's bottom line is not a measured form"),
    (text(MODE + ' (shift+tab to cycle) · ← for agents'), "its screen's bottom line is not a measured form"),
    (text('  paste again to expand'), "its screen's bottom line is not a measured form"),
    (text(ONE_SHELL, '', '  ● main', '  ◯ general-purpose  synthetic helper'),
     "its screen's bottom line is not a measured form"),
    (text(NOTHING, sign='✽ Pondering… (47s · ↓ 3.9k tokens)'), 'its screen is not the composer at rest'),
    (text(NOTHING, sign='✢ Pondering… (1m 32s · ↓ 3.6k tokens)'), 'its screen is not the composer at rest'),
    (text(NOTHING, box='❯\xa0synthetic typed words'), 'its screen is not the composer at rest'),
    (text(NOTHING, box='❯ Press up to edit queued messages'), 'its screen is not the composer at rest'),
    (text(NOTHING, box='synthetic layout with no composer'), 'its screen is not the composer at rest'),
    ('', 'its screen is not the composer at rest'),
    (None, 'its screen is not the composer at rest'),
    (OSError('synthetic unreadable pane'), 'its screen could not be read'),
])
def test_a_screen_that_does_not_say_settles_nothing_and_the_hold_says_what_it_lacked(case, shown, lacked):
    built = case(unread(), shown, alone=True)
    state, hold, said = built.second()
    assert state is built.state
    assert hold == idle.Hold('tools-unknown', f'tool state unknown: {lacked}') and said == lacked


def test_an_interruption_on_the_screen_is_not_rest(case):
    built = case(unread(), text(NOTHING), alone=True)
    resting = ClaudeHarness().screen(text(NOTHING))
    for changed in (replace(resting, interruption=object()), replace(resting, unknown='synthetic layout'),
                    replace(resting, starting=True), replace(resting, fresh='synthetic question'),
                    replace(resting, queued=1), replace(resting, working=True), replace(resting, box=None)):
        state, hold, said = built.second(screen=changed)
        assert hold.dimension == 'tools-unknown' and said == 'its screen is not the composer at rest', changed
    assert built.second(screen=resting)[1] is None
    assert built.reads == 0, 'a screen the caller already read is not read again'


# -- only together with the record ------------------------------------------------------------------------------

@pytest.mark.parametrize('changes', [
    {'completed_turns': ()},
    {'last_input_at': None},
    {'last_input_at': AT + timedelta(seconds=6)},
])
def test_the_record_itself_must_show_everything_else_at_rest(case, changes):
    built = case(unread(**changes), text(NOTHING), alone=True)
    assert idle.check(built.state).dimension == 'tools-unknown', 'the tool state is asked before the completion'
    assert built.second() == (built.state, UNKNOWN, None)
    assert built.reads == 0, 'the screen is not read where the record does not show the rest'


@pytest.mark.parametrize(('changes', 'dimension'), [
    ({'turn_open': True}, 'turn-open'),
    ({'turn_open': None}, 'turn-unknown'),
    ({'queued': ('synthetic queued input',)}, 'queue-pending'),
    ({'queued': None}, 'queue-unknown'),
    ({'queue_error': 'synthetic ambiguity'}, 'queue-unknown'),
])
def test_a_hold_of_any_other_kind_is_never_the_screens_to_lift(case, changes, dimension):
    built = case(unread(**changes), text(NOTHING), alone=True)
    state, hold = idle.reading(built.seat)
    assert hold.dimension == dimension and built.reads == 0
    assert idle.second(built.seat, built.state, hold) == (built.state, hold, None)
    # even handed the unknown-tools hold, the record's own turn and queue still decide first
    if dimension != 'queue-unknown' or changes.get('queued', ()) is None:
        assert built.second() == (built.state, UNKNOWN, None) and built.reads == 0


def test_a_queue_the_explicit_request_may_pass_is_passed_to_the_screen_too(case):
    built = case(unread(queue_error='synthetic ambiguity'), text(NOTHING))
    assert built.second() == (built.state, UNKNOWN, None) and built.reads == 0
    assert built.second(explicit=True)[1] is None and built.reads == 1


def test_a_record_that_knows_is_never_second_guessed_by_the_screen(case):
    for tools in ((), ('Bash',)):
        built = case(unread(pending_tools=tools), text(MODE + ' · 2 shells'), alone=True)
        reading = idle.reading(built.seat)
        assert (reading.hold is None) == (tools == ())
        assert reading.witness == idle.RECORD and built.reads == 0
        known = idle.Hold('tools-pending', 'tools pending: Bash since unknown')
        assert idle.second(built.seat, built.state, known) == (built.state, known, None)


@pytest.mark.parametrize('version', [None, '2.1.322', '2.1.326', '2.1.3170', ''])
def test_a_version_with_no_measured_bottom_line_has_no_second_witness(case, version):
    built = case(unread(), text(NOTHING), version=version)
    assert built.second() == (built.state, UNKNOWN, None)
    assert built.reads == 0, 'its screen is not read at all'
    assert idle.reading(built.seat).hold == UNKNOWN


def test_a_program_whose_adapter_reads_no_bottom_line_is_held_as_before(case):
    built = case(unread(), text(NOTHING))
    built.seat.harness = SimpleNamespace(state=lambda record: built.state, waiting_for_human=lambda reading: False)
    reading = idle.reading(built.seat)
    assert reading.hold == UNKNOWN and reading.witness == idle.RECORD and built.reads == 0


def test_2_1_325_is_read_by_its_own_forms_only(case):
    page = MODE + ' · 1 shell · ⧉ fern'
    assert case(unread(), text(page), alone=True, version='2.1.325').second()[1] is None
    assert case(unread(), text(ONE_SHELL), alone=True, version='2.1.325').second()[1].dimension == 'tools-unknown'
    assert case(unread(), text(page), alone=True, version='2.1.317').second()[1].dimension == 'tools-unknown'


# -- the reading every consumer shares --------------------------------------------------------------------------

def test_a_reading_is_still_a_pair_and_names_its_witness(case):
    built = case(unread(), text(NOTHING))
    reading = idle.reading(built.seat)
    state, hold = reading
    assert (state, hold) == (reading.state, reading.hold) and len(reading) == 2
    assert hold is None and state.pending_tools == ()
    assert reading.witness == idle.RECORD_AND_SCREEN and reading.reason == 'known idle'
    assert idle.Reading(None, None).witness == idle.RECORD
    assert move_hold.reading(built.seat) is None, 'the move and the status see the same settled reading'


def test_work_the_screen_names_holds_the_reading_and_the_move_under_its_name(case):
    built = case(unread(), text(MODE + ' · 2 shells'))
    reading = idle.reading(built.seat)
    assert reading.hold == idle.Hold('tools-pending', 'tools pending: its screen shows 2 shells in the background')
    assert reading.witness == idle.RECORD_AND_SCREEN and reading.state is not None
    assert reading.state.pending_tools is None, 'the record still does not say'
    assert move_hold.reading(built.seat) == reading.hold.reason


def test_a_screen_that_does_not_say_leaves_the_record_as_the_only_witness(case):
    built = case(unread(), text('  paste again to expand'))
    reading = idle.reading(built.seat)
    assert reading.hold.dimension == 'tools-unknown' and reading.witness == idle.RECORD
    assert reading.hold.reason == "tool state unknown: its screen's bottom line is not a measured form"
    assert move_hold.reading(built.seat) == 'tool state unknown'


def test_a_launch_that_changes_while_reading_is_never_settled_by_the_screen(case):
    built = case(unread(), text(NOTHING))
    first = built.launch

    class Door:
        def __init__(self, seat):
            pass

        def screen(self):
            built.launch = replace_launch(first, pid=202002)
            return ClaudeHarness().screen(text(NOTHING))

    def replace_launch(launch, **changes):
        return SimpleNamespace(**{**vars(launch), **changes})

    import quarterdeck.door as doors
    doors.Door = Door  # the case's monkeypatch restores the module's own Door after the test
    reading = idle.reading(built.seat)
    assert reading.state is None and reading.hold.dimension == 'identity' and reading.witness == idle.RECORD


@pytest.mark.parametrize('path', ['failover', 'select', 'relaunch', 'fresh'])
def test_every_live_path_applies_on_the_record_and_the_screen(case, monkeypatch, path):
    built = case(unread(), text(ONE_SHELL), alone=True)
    service = built.service(monkeypatch)
    if path == 'failover':
        service.failover()
        assert built.moved == ['pool-oak']
    elif path == 'select':
        assert 'tool' not in service.select(built.seat, 'pool-oak') and built.moved == ['pool-oak']
    else:
        answer = service.relaunch(built.seat, path == 'relaunch')
        proof = 'synthetic restart proof' if path == 'relaunch' else 'synthetic fresh proof'
        assert answer == f'{proof}\nat rest by its record and its screen', 'the proof line names both witnesses'
        assert built.moved == ['restart' if path == 'relaunch' else 'fresh']
    assert move_hold.reading(built.seat) is None


@pytest.mark.parametrize('path', ['failover', 'select', 'relaunch'])
@pytest.mark.parametrize('shown', [text(MODE + ' · 2 shells'), text(ONE_SHELL), text('  paste again to expand'),
                                   text(NOTHING, sign='✽ Pondering… (47s · ↓ 3.9k tokens)')])
def test_every_live_path_still_holds_where_the_screen_shows_work_or_does_not_say(case, monkeypatch, path, shown):
    built = case(unread(), shown, alone=False)
    service = built.service(monkeypatch)
    if path == 'failover':
        service.failover()
    elif path == 'select':
        assert 'tool' in service.select(built.seat, 'pool-oak')
    else:
        assert service.relaunch(built.seat, True) is None
        assert service.request_wait[0] in ('tools-pending', 'tools-unknown')
    assert built.moved == []


def test_a_relaunch_proved_by_the_record_alone_says_nothing_of_the_screen(case, monkeypatch):
    built = case(unread(pending_tools=(), active_tools=()), text(MODE + ' · 2 shells'))
    assert built.service(monkeypatch).relaunch(built.seat, True) == 'synthetic restart proof'
    assert built.reads == 0


# -- the person-quiet reading and the watch line ------------------------------------------------------------------

def test_the_person_quiet_reading_uses_the_same_second_witness(case):
    assert person_quiet.idle_hold(case(unread(), text(NOTHING)).seat, unread()) is None
    assert person_quiet.idle_hold(case(unread(), text(ONE_SHELL), alone=True).seat, unread()) is None
    held = person_quiet.idle_hold(case(unread(), text(MODE + ' · 2 shells')).seat, unread())
    assert held == 'tools pending: its screen shows 2 shells in the background'
    assert person_quiet.idle_hold(case(unread(), text('  ? for shortcuts')).seat, unread()).startswith(
        'tool state unknown: ')
    assert person_quiet.idle_hold(case(unread(turn_open=True), text(NOTHING)).seat,
                                  unread(turn_open=True)) == 'a turn is open'


@pytest.mark.parametrize(('bottom', 'alone', 'line'), [
    (NOTHING, False, 'tool state unknown in its record; its screen shows nothing in the background: at rest'),
    (ONE_SHELL, True,
     'tool state unknown in its record; its screen shows only its inbox watch in the background: at rest'),
    (MODE + ' · 1 shell, 1 monitor', False,
     'tool state unknown in its record; its screen shows 1 shell and 1 monitor in the background'),
    ('  paste again to expand', False, "tool state unknown: its screen's bottom line is not a measured form"),
])
def test_the_watch_line_says_what_the_screen_showed_or_lacked(case, bottom, alone, line):
    built = case(unread(), text(bottom), alone=alone)
    shown = ClaudeHarness().screen(text(bottom))
    assert watch.tool_reading(built.seat, built.state, shown) == (line, None)
    assert built.reads == 0, 'the screen the watch already read is the one judged'


def test_the_watch_line_of_a_known_record_is_unchanged(case):
    built = case(unread(pending_tools=(), active_tools=()), text(MODE + ' · 2 shells'))
    assert watch.tool_reading(built.seat, built.state, ClaudeHarness().screen(text(NOTHING))) == (None, None)
    pending = replace(built.state, pending_tools=('Bash',), active_tools=(ActiveTool('Bash', AT),))
    assert watch.tool_reading(built.seat, pending, None)[0] == f'tools pending: Bash since {AT.isoformat()}'
    assert watch.tool_reading(built.seat, None, None) == (None, None)
