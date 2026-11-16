"""Native notice input/turn accounting, with synthetic records and no live restart."""

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from types import SimpleNamespace

import pytest

from quarterdeck import cli, idle, move_hold, post, recovery, watch
from quarterdeck.harness import claude
from quarterdeck.harness.types import Screen
from quarterdeck.seat import Seat
from quarterdeck.service import Service


AT = datetime(2032, 4, 5, 6, 7, tzinfo=timezone.utc)
RESUME_NOTE = (
    'No completion record was found for it in the previous session. It may have been stopped'
    ' (via the UI, Monitor timeout, or agent teardown — these leave no transcript marker),'
    ' or it may have been running when the previous Claude Code process exited.'
)


def row(kind, second, **fields):
    return dict(type=kind, version='2.1.317',
                timestamp=(AT + timedelta(seconds=second)).isoformat(), **fields)


def completed_background_turn():
    return [
        row('user', 0, message=dict(role='user', content='synthetic finished input')),
        row('assistant', 1, message=dict(role='assistant', content=[dict(
            type='tool_use', id='call-spruce', name='Bash',
            input=dict(command='synthetic background work', run_in_background=True))])),
        row('user', 2, message=dict(role='user', content=[dict(
            type='tool_result', tool_use_id='call-spruce', content='synthetic result')]),
            toolUseResult=dict(backgroundTaskId='task-willow')),
        row('system', 3, subtype='turn_duration'),
    ]


def notice(status='completed', *, note=False, version='2.1.317'):
    if note is True:
        note = RESUME_NOTE
    text = ('<task-notification>\n<task-id>task-willow</task-id>\n'
            '<tool-use-id>call-spruce</tool-use-id>\n'
            f'<status>{status}</status>\n<summary>synthetic summary</summary>\n'
            + (f'<note>{note}</note>\n' if note else '')
            + '</task-notification>')
    return {**row('user', 5, origin=dict(kind='task-notification'),
                  message=dict(role='user', content=text)), 'version': version}


@pytest.fixture(autouse=True)
def old_notice_clock(monkeypatch):
    """The reader judges a notice's age on its own clock, never wall time: here every notice is an hour old."""
    at_clock(monkeypatch, 3600)


def at_clock(monkeypatch, seconds):
    monkeypatch.setattr(claude._Reader, 'clock', staticmethod(lambda: AT + timedelta(seconds=seconds)), raising=False)


def record(tmp_path, rows):
    path = tmp_path / 'record-spruce.jsonl'
    path.write_text(''.join(json.dumps(entry) + '\n' for entry in rows))
    os.utime(path, (AT.timestamp(), AT.timestamp()))
    return path


@pytest.mark.parametrize('version', ['2.1.317', '2.1.322'])
@pytest.mark.parametrize('status', ['completed', 'failed', 'killed'])
def test_terminal_notice_preserves_finished_input_and_turn(tmp_path, version, status):
    rows = [{**entry, 'version': version} for entry in completed_background_turn()]
    path = record(tmp_path, rows + [notice(status, version=version)])
    state = claude.state(path)
    assert state.last_input == 'synthetic finished input'
    assert state.last_input_at == AT
    assert state.turn_open is False and state.turns == 1
    assert len(state.completed_turns) == 1
    assert state.completed_turns[0].ended == AT + timedelta(seconds=3)
    assert [item.text for item in claude.inputs(path)] == ['synthetic finished input']
    assert state.pending_tools == ()
    assert idle.check(state) is None


def test_notice_during_an_open_turn_does_not_close_or_replace_it(tmp_path):
    path = record(tmp_path, completed_background_turn()[:-1] + [notice()])
    state = claude.state(path)
    assert state.turn_open is True
    assert state.last_input == 'synthetic finished input' and state.last_input_at == AT
    assert state.completed_turns == ()
    assert idle.check(state).dimension == 'turn-open'


def test_queue_notification_waits_until_the_native_user_carrier_consumes_it(tmp_path):
    payload = notice()['message']['content']
    rows = completed_background_turn() + [row('queue-operation', 4, operation='enqueue', content=payload)]
    path = record(tmp_path, rows)
    state = claude.state(path)
    assert state.pending_tools == ()
    assert state.queued == (payload,)
    assert state.last_input_at == AT and state.turn_open is False
    assert idle.check(state).dimension == 'queue-pending'
    with path.open('a') as stream:
        stream.write(json.dumps(notice()) + '\n')
    after = claude.state(path)
    assert after.queued == () and after.last_input_at == AT and after.turn_open is False
    assert idle.check(after) is None
    queued_input = [item for item in claude.inputs(path) if item.text == payload]
    assert len(queued_input) == 1 and queued_input[0].kind == 'consumed'
    assert queued_input[0].at == AT + timedelta(seconds=4)
    assert queued_input[0].consumed_at == AT + timedelta(seconds=5)


@pytest.mark.parametrize('difference', ['version', 'missing-version', 'origin', 'status', 'malformed', 'blocks'])
def test_unclassified_native_notice_remains_held(tmp_path, difference):
    entry = notice()
    if difference == 'version':
        entry['version'] = 'synthetic-unmeasured-version'
    elif difference == 'missing-version':
        del entry['version']
    elif difference == 'origin':
        entry['origin']['kind'] = 'synthetic-unrecognized-origin'
    elif difference == 'status':
        entry['message']['content'] = entry['message']['content'].replace('completed', 'synthetic-status')
    elif difference == 'malformed':
        entry['message']['content'] = '<task-notification>synthetic incomplete notice'
    else:
        entry['message']['content'] = [dict(type='text', text=entry['message']['content'])]
    assert claude.prompt_text(entry) is not None
    state = claude.state(record(tmp_path, completed_background_turn() + [entry]))
    assert idle.check(state) is not None
    assert state.turn_open is True or state.pending_tools is None


def test_notice_text_typed_by_a_person_is_still_input(tmp_path):
    entry = notice()
    del entry['origin']
    path = record(tmp_path, completed_background_turn() + [entry])
    state = claude.state(path)
    assert state.last_input == entry['message']['content']
    assert state.turn_open is True and state.last_input_at == AT + timedelta(seconds=5)


def resumed_seat(tmp_path, monkeypatch, *, stopped, rows=None, launch_second=4):
    path = record(tmp_path, rows if rows is not None else completed_background_turn() + [
        notice('stopped' if stopped else 'completed', note=stopped)])
    reader = claude.ClaudeHarness()
    screen = Screen('synthetic empty composer', box='', working=False, queued=0)
    monkeypatch.setattr(reader, 'screen', lambda _: screen)
    launch = SimpleNamespace(pane='%pane-spruce', pid=101001,
                             at=(AT + timedelta(seconds=launch_second)).isoformat(), harness='claude')
    pane = SimpleNamespace(id=launch.pane, pid=launch.pid, dead=False)
    calls = []
    spec = SimpleNamespace(name='seat-spruce')
    ship = SimpleNamespace(runtime=tmp_path / 'runtime', seat=lambda _: spec)
    seat = SimpleNamespace(name=spec.name, spec=spec, ship=ship,
                           folder=ship.runtime / 'seats' / spec.name, trusted=ship.runtime / 'seats' / spec.name,
                           session=SimpleNamespace(human=True), harness=reader,
                           pane=lambda: pane, launched=lambda: launch,
                           record=lambda: path, join=lambda: SimpleNamespace(record=path, reason=None),
                           read_pane=lambda: (pane, screen.text),
                           restart=lambda: calls.append('restart') or 'synthetic restart applied')
    seat.is_working = lambda: Seat.is_working(seat)
    monkeypatch.setattr(recovery, 'held', lambda _: None)
    monkeypatch.setattr(post, 'now', lambda: AT + timedelta(seconds=7))
    service = Service.__new__(Service)
    service.seat = lambda _: seat
    service.request_wait = None
    return seat, service, calls


@pytest.mark.parametrize('stopped', [False, True])
def test_resumed_notice_only_seat_reads_idle_and_service_restart_applies(tmp_path, monkeypatch, stopped):
    seat, service, calls = resumed_seat(tmp_path, monkeypatch, stopped=stopped)
    state = seat.harness.state(seat.record())
    assert state.last_input_at == AT
    assert state.completed_turns[-1].ended == AT + timedelta(seconds=3)
    assert state.turn_open is False and state.queued == ()
    assert seat.harness.screen('').box == '' and not seat.harness.screen('').working
    assert watch.activity(seat, now=AT + timedelta(hours=1)).state == 'idle'
    answer = service.relaunch_request(dict(seat=seat.name, resume=True, caller='synthetic-controller',
                                         created_at=(AT + timedelta(seconds=6)).isoformat()))
    if stopped:
        assert state.pending_tools is None
        assert idle.check(state).dimension == 'tools-unknown'
        assert move_hold.reading(seat) == 'tool state unknown'
        assert answer == 'synthetic restart applied' and calls == ['restart']
        assert seat.harness.state(seat.record()) == state
    else:
        assert state.pending_tools == ()
        assert answer == 'synthetic restart applied' and calls == ['restart']


def test_new_manual_restart_uses_its_existing_idle_command_path(tmp_path, monkeypatch):
    seat, _, calls = resumed_seat(tmp_path, monkeypatch, stopped=True)
    assert idle.check(seat.harness.state(seat.record())).dimension == 'tools-unknown'
    monkeypatch.setattr(cli, 'load_ship', lambda _: seat.ship)
    monkeypatch.setattr(cli, 'seat_of', lambda *_: seat)
    monkeypatch.setattr(cli.service, 'request', lambda *_args, **_kwargs: pytest.fail('the idle command is direct'))
    answer = cli.cmd_restart(argparse.Namespace(ship='synthetic', seat=seat.name, cancel=None))
    assert answer == 'synthetic restart applied'
    assert calls == ['restart']


def test_incremental_notice_read_has_the_same_input_and_turns_as_full_parse(tmp_path):
    path = record(tmp_path, completed_background_turn())
    before = claude.state(path)
    assert before.pending_tools == ('Bash',)
    with path.open('a') as stream:
        stream.write(json.dumps(notice()) + '\n')
    after = claude.state(path)
    assert after == claude._Reader(path).state()
    assert after.completed_turns == before.completed_turns
    assert after.last_input == before.last_input and after.last_input_at == before.last_input_at


def test_notice_woken_assistant_is_working_until_its_native_end_and_keeps_usage(tmp_path, monkeypatch):
    seat, service, calls = resumed_seat(tmp_path, monkeypatch, stopped=False)
    path = seat.record()
    assert seat.harness.state(path).turn_open is False
    reply = row('assistant', 6, message=dict(
        role='assistant', id='message-oak', model='synthetic-model', stop_reason='end_turn',
        content=[dict(type='text', text='synthetic notice response')],
        usage=dict(input_tokens=7, output_tokens=3)))
    with path.open('a') as stream:
        stream.write(json.dumps(reply) + '\n')
    os.utime(path, ((AT + timedelta(seconds=6)).timestamp(),) * 2)
    working = seat.harness.state(path)
    assert working.turn_open is True and working.last_input_at == AT
    assert watch.activity(seat, now=AT + timedelta(seconds=7)).state == 'working'
    assert idle.check(working).dimension == 'turn-open'
    answer = service.relaunch_request(dict(seat=seat.name, resume=True, caller='synthetic-controller',
                                         created_at=(AT + timedelta(seconds=6)).isoformat()))
    assert answer == 'refused: seat-spruce waits for a turn is open; human request not deferred'
    assert calls == []

    queued = []
    monkeypatch.setattr(cli, 'load_ship', lambda _: seat.ship)
    monkeypatch.setattr(cli, 'seat_of', lambda *_: seat)
    monkeypatch.setattr(cli.service, 'request', lambda *args, **kwargs: queued.append(kwargs) or 'synthetic queued restart')
    assert cli.cmd_restart(argparse.Namespace(ship='synthetic', seat=seat.name, cancel=None)) == 'synthetic queued restart'
    assert queued[0]['seat'] == seat.name and calls == []

    with path.open('a') as stream:
        stream.write(json.dumps(row('system', 8, subtype='turn_duration')) + '\n')
    done = seat.harness.state(path)
    assert done == claude._Reader(path).state()
    assert done.turn_open is False and done.last_input_at == AT and done.turns == 2
    turn = done.completed_turns[-1]
    assert (turn.started, turn.ended) == (AT + timedelta(seconds=6), AT + timedelta(seconds=8))
    assert turn.reply == 'synthetic notice response'
    assert (turn.tokens.read_uncached, turn.tokens.written) == (7, 3)
    assert idle.check(done) is None
    assert cli.cmd_restart(argparse.Namespace(ship='synthetic', seat=seat.name, cancel=None)) == 'synthetic restart applied'
    assert calls == ['restart'] and len(queued) == 1


def test_real_prompt_replaces_the_pending_notice_for_accounting(tmp_path):
    rows = completed_background_turn() + [notice(),
            row('user', 6, message=dict(role='user', content='synthetic next input')),
            row('assistant', 7, message=dict(role='assistant', content=[dict(type='text', text='synthetic reply')],
                                            usage=dict(output_tokens=2))),
            row('system', 8, subtype='turn_duration')]
    state = claude.state(record(tmp_path, rows))
    assert state.last_input == 'synthetic next input' and state.turns == 2
    assert state.completed_turns[-1].input == 'synthetic next input'
    assert state.completed_turns[-1].tokens.written == 2


@pytest.mark.parametrize('carrier', ['user', 'enqueue-and-user'])
@pytest.mark.parametrize('suffix', ['', ' Check the output file for partial results before assuming it completed.'])
@pytest.mark.parametrize('human', [False, True])
def test_exact_prior_launch_resume_forms_admit_only_relaunch(tmp_path, monkeypatch, carrier, suffix, human):
    entry = notice('stopped', note=RESUME_NOTE + suffix)
    rows = completed_background_turn()
    if carrier == 'enqueue-and-user':
        rows += [row('queue-operation', 4, operation='enqueue', content=entry['message']['content'])]
    seat, service, calls = resumed_seat(tmp_path, monkeypatch, stopped=True, rows=rows + [entry])
    seat.session.human = human
    original = seat.harness.state(seat.record())
    assert original.pending_tools is None and original.turn_open is False and original.queued == ()
    assert watch.activity(seat, now=AT + timedelta(hours=1)).state == 'idle'
    assert idle.reading(seat).hold.dimension == 'tools-unknown'
    # The ordinary cached record and ordinary move/status proof remain unknown.
    assert idle.reading(seat, explicit=True, relaunch=True).hold is None
    assert move_hold.reading(seat) == 'tool state unknown'
    answer = service.relaunch_request(dict(seat=seat.name, resume=True, caller='synthetic-controller',
                                         created_at=(AT + timedelta(seconds=6)).isoformat()))
    assert answer == 'synthetic restart applied' and calls == ['restart']
    assert seat.harness.state(seat.record()) == original


@pytest.mark.parametrize('launch_second', [1, 2])
def test_current_launch_task_still_holds(tmp_path, monkeypatch, launch_second):
    seat, service, calls = resumed_seat(tmp_path, monkeypatch, stopped=True, launch_second=launch_second)
    assert idle.reading(seat, explicit=True, relaunch=True).hold.dimension == 'tools-unknown'
    assert service.relaunch(seat, True) is None and calls == []


@pytest.mark.parametrize('launch_second', [5.5, 10])
def test_a_later_measured_launch_settles_an_older_resume_notice(tmp_path, monkeypatch, launch_second):
    seat, service, calls = resumed_seat(tmp_path, monkeypatch, stopped=True, launch_second=launch_second)
    assert idle.reading(seat, explicit=True, relaunch=True).hold is None
    assert service.relaunch(seat, True) == 'synthetic restart applied' and calls == ['restart']
    assert seat.harness.state(seat.record()).pending_tools is None


@pytest.mark.parametrize('launch_time', ['synthetic-unknown-time', None, '2032-04-05T06:07:04'])
def test_unknown_launch_start_cannot_scope_the_task(tmp_path, monkeypatch, launch_time):
    seat, service, calls = resumed_seat(tmp_path, monkeypatch, stopped=True)
    seat.launched().at = launch_time
    assert service.relaunch(seat, True) is None and calls == []
    assert service.request_wait == ('tools-unknown', 'tool state unknown')


@pytest.mark.parametrize('difference', ['note', 'status', 'version', 'unmeasured-carrier', 'unknown-start',
                                         'later-running', 'earlier-running', 'other-note', 'other-status',
                                         'delegated', 'other-task'])
def test_other_task_evidence_is_never_hidden_by_the_resume_allowance(tmp_path, monkeypatch, difference):
    rows = completed_background_turn()
    entry = notice('stopped', note=True)
    if difference == 'note':
        entry = notice('stopped', note=RESUME_NOTE + ' synthetic extra text')
    elif difference == 'status':
        entry = notice('killed', note=RESUME_NOTE)
    elif difference == 'version':
        entry['version'] = '2.1.322'
    elif difference == 'unmeasured-carrier':
        entry = row('attachment', 5, attachment=dict(type='queued_command', commandMode='task-notification',
                    origin=dict(kind='task-notification'), prompt=entry['message']['content']))
    elif difference == 'unknown-start':
        del rows[1]['timestamp']
    elif difference in ('later-running', 'earlier-running'):
        running = row('attachment', 6 if difference == 'later-running' else 4,
                      attachment=dict(type='task_status', taskType='local_bash',
                                      taskId='task-willow', status='running'))
        if difference == 'later-running':
            rows += [entry]
            entry = running
        else:
            rows += [running]
    elif difference in ('other-note', 'other-status'):
        other = notice('stopped' if difference == 'other-note' else 'completed',
                       note='synthetic other note' if difference == 'other-note' else False)
        other['timestamp'] = (AT + timedelta(seconds=4)).isoformat()
        rows += [other]
    elif difference == 'delegated':
        rows[1]['message']['content'][0]['name'] = 'Agent'
    elif difference == 'other-task':
        rows += [row('user', 4.1, message=dict(role='user', content='synthetic other input')),
                 row('assistant', 4.2, message=dict(role='assistant', content=[dict(
                     type='tool_use', id='call-oak', name='Bash',
                     input=dict(command='synthetic other work', run_in_background=True))])),
                 row('user', 4.3, message=dict(role='user', content=[dict(
                     type='tool_result', tool_use_id='call-oak', content='synthetic result')]),
                     toolUseResult=dict(backgroundTaskId='task-ash')),
                 row('system', 4.4, subtype='turn_duration')]
    seat, service, calls = resumed_seat(tmp_path, monkeypatch, stopped=True, rows=rows + [entry])
    assert idle.reading(seat, explicit=True, relaunch=True).hold is not None
    assert service.relaunch(seat, True) is None and calls == []


GRACE = getattr(claude, 'NOTICE_GRACE_SECONDS', 60)
REPLY = dict(role='assistant', id='message-elm', model='synthetic-model', stop_reason='end_turn',
             content=[dict(type='text', text='synthetic notice response')], usage=dict(input_tokens=7, output_tokens=3))


def append(path, *rows):
    with path.open('a') as stream:
        stream.write(''.join(json.dumps(entry) + '\n' for entry in rows))


@pytest.mark.parametrize('age', [0, 2, GRACE - 0.001])
@pytest.mark.parametrize('stopped', [False, True])
def test_a_fresh_unanswered_notice_reads_turn_unknown_and_a_restart_waits(tmp_path, monkeypatch, age, stopped):
    seat, service, calls = resumed_seat(tmp_path, monkeypatch, stopped=stopped)
    at_clock(monkeypatch, 5 + age)  # the notice is at second 5
    state = seat.harness.state(seat.record())
    assert state.turn_open is None
    assert state == claude._Reader(seat.record()).state()
    assert state.last_input_at == AT and state.queued == () and state.completed_turns[-1].ended == AT + timedelta(seconds=3)
    assert idle.check(state).dimension == 'turn-unknown'
    assert watch.activity(seat, now=AT + timedelta(seconds=5 + age)).state == 'unknown'
    assert idle.reading(seat, explicit=True, relaunch=True).hold.dimension == 'turn-unknown'
    answer = service.relaunch_request(dict(seat=seat.name, resume=True, caller='synthetic-controller',
                                         created_at=(AT + timedelta(seconds=6)).isoformat()))
    assert answer.startswith('refused: seat-spruce waits for turn state unknown') and calls == []
    assert service.relaunch(seat, True) is None and calls == []
    assert service.request_wait == ('turn-unknown', 'turn state unknown')


@pytest.mark.parametrize('working', [True, False])
def test_while_a_notice_is_fresh_the_screen_decides_whether_the_seat_works(tmp_path, monkeypatch, working):
    seat, _, _ = resumed_seat(tmp_path, monkeypatch, stopped=False)
    at_clock(monkeypatch, 6)
    screen = Screen('synthetic composer', box='', working=working, queued=0)
    monkeypatch.setattr(seat.harness, 'screen', lambda _: screen)
    assert seat.harness.state(seat.record()).turn_open is None
    assert seat.is_working() is working


@pytest.mark.parametrize('age', [GRACE, GRACE + 1, 3600])
@pytest.mark.parametrize('stopped', [False, True])
def test_an_old_unanswered_notice_reads_idle_and_its_restart_applies(tmp_path, monkeypatch, age, stopped):
    seat, service, calls = resumed_seat(tmp_path, monkeypatch, stopped=stopped)
    at_clock(monkeypatch, 5 + age)
    state = seat.harness.state(seat.record())
    assert state.turn_open is False
    assert seat.is_working() is False
    answer = service.relaunch_request(dict(seat=seat.name, resume=True, caller='synthetic-controller',
                                         created_at=(AT + timedelta(seconds=6)).isoformat()))
    assert answer == 'synthetic restart applied' and calls == ['restart']


def test_the_same_cached_reading_turns_idle_once_the_grace_has_passed(tmp_path, monkeypatch):
    seat, _, _ = resumed_seat(tmp_path, monkeypatch, stopped=False)
    path = seat.record()
    at_clock(monkeypatch, 6)
    assert claude.state(path).turn_open is None
    at_clock(monkeypatch, 5 + GRACE)
    assert claude.state(path).turn_open is False  # the same bytes: only the reader's clock moved
    at_clock(monkeypatch, 6)
    assert claude.state(path).turn_open is None


def test_an_assistant_row_after_a_fresh_notice_reads_as_before(tmp_path, monkeypatch):
    seat, _, _ = resumed_seat(tmp_path, monkeypatch, stopped=False)
    path = seat.record()
    at_clock(monkeypatch, 6)
    append(path, row('assistant', 6, message=REPLY))
    working = claude.state(path)
    assert working.turn_open is True and working == claude._Reader(path).state()
    assert idle.check(working).dimension == 'turn-open'
    append(path, row('system', 8, subtype='turn_duration'))
    at_clock(monkeypatch, 9)
    done = claude.state(path)
    assert done.turn_open is False and done.turns == 2 and idle.check(done) is None


def test_an_unclassified_assistant_row_ends_the_window_as_before(tmp_path, monkeypatch):
    path = record(tmp_path, completed_background_turn() + [notice(), row('assistant', 6, message=dict(
        role='assistant', content=[dict(type='synthetic-unknown-block')]))])
    at_clock(monkeypatch, 6)
    state = claude.state(path)
    assert state.turn_open is False and state.pending_tools is None
    assert idle.check(state).dimension == 'tools-unknown'


def test_rows_other_than_an_assistant_row_keep_the_window_open(tmp_path, monkeypatch):
    path = record(tmp_path, completed_background_turn() + [
        notice(), row('api-request', 5.2), row('attachment', 5.3, attachment=dict(type='synthetic-attachment'))])
    at_clock(monkeypatch, 6)
    assert claude.state(path).turn_open is None
    at_clock(monkeypatch, 5 + GRACE)
    assert claude.state(path).turn_open is False


def test_a_typed_prompt_after_a_fresh_notice_opens_its_turn_as_before(tmp_path, monkeypatch):
    path = record(tmp_path, completed_background_turn() + [
        notice(), row('user', 6, message=dict(role='user', content='synthetic next input'))])
    at_clock(monkeypatch, 6)
    state = claude.state(path)
    assert state.turn_open is True and state.last_input == 'synthetic next input'


def test_a_notice_whose_time_cannot_be_read_stays_unknown_until_a_reply(tmp_path, monkeypatch):
    entry = notice()
    del entry['timestamp']
    path = record(tmp_path, completed_background_turn() + [entry])
    assert claude.state(path).turn_open is None  # an hour after every other row: its own age is not known
    append(path, row('assistant', 6, message=REPLY))
    assert claude.state(path).turn_open is True


def test_a_notice_during_an_open_turn_opens_no_window_of_its_own(tmp_path, monkeypatch):
    path = record(tmp_path, completed_background_turn()[:-1] + [notice()])
    at_clock(monkeypatch, 6)
    assert claude.state(path).turn_open is True
    append(path, row('system', 7, subtype='turn_duration'))
    assert claude.state(path).turn_open is False
