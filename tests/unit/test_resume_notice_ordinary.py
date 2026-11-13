"""Native notice input/turn accounting, with synthetic records and no live restart."""

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from types import SimpleNamespace

import pytest

from quarterdeck import cli, idle, post, recovery, watch
from quarterdeck.harness import claude
from quarterdeck.harness.types import Screen
from quarterdeck.seat import Seat
from quarterdeck.service import Service


AT = datetime(2032, 4, 5, 6, 7, tzinfo=timezone.utc)


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
    text = ('<task-notification>\n<task-id>task-willow</task-id>\n'
            '<tool-use-id>call-spruce</tool-use-id>\n'
            f'<status>{status}</status>\n<summary>synthetic summary</summary>\n'
            + ('<note>synthetic resume uncertainty</note>\n' if note else '')
            + '</task-notification>')
    return {**row('user', 5, origin=dict(kind='task-notification'),
                  message=dict(role='user', content=text)), 'version': version}


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


def test_queue_notification_still_needs_native_dequeue(tmp_path):
    payload = notice()['message']['content']
    rows = completed_background_turn() + [row('queue-operation', 4, operation='enqueue', content=payload), notice()]
    path = record(tmp_path, rows)
    state = claude.state(path)
    assert state.pending_tools == ()
    assert state.queued == (payload,)
    assert state.last_input_at == AT and state.turn_open is False
    assert idle.check(state).dimension == 'queue-pending'
    with path.open('a') as stream:
        stream.write(json.dumps(row('queue-operation', 6, operation='remove', content=payload)) + '\n')
    assert idle.check(claude.state(path)) is None


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


def resumed_seat(tmp_path, monkeypatch, *, stopped):
    path = record(tmp_path, completed_background_turn() + [
        notice('stopped' if stopped else 'completed', note=stopped)])
    reader = claude.ClaudeHarness()
    screen = Screen('synthetic empty composer', box='', working=False, queued=0)
    monkeypatch.setattr(reader, 'screen', lambda _: screen)
    launch = SimpleNamespace(pane='%pane-spruce', pid=101001,
                             at=(AT + timedelta(seconds=4)).isoformat(), harness='claude')
    pane = SimpleNamespace(id=launch.pane, pid=launch.pid, dead=False)
    calls = []
    spec = SimpleNamespace(name='seat-spruce')
    ship = SimpleNamespace(runtime=tmp_path / 'runtime', seat=lambda _: spec)
    seat = SimpleNamespace(name=spec.name, spec=spec, ship=ship,
                           folder=ship.runtime / 'seats' / spec.name,
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
def test_resumed_notice_only_seat_reads_idle_and_service_keeps_tool_evidence(tmp_path, monkeypatch, stopped):
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
        assert service.request_wait == ('tools-unknown', 'tool state unknown')
        assert answer == 'refused: seat-spruce waits for tool state unknown; human request not deferred'
        assert calls == []
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
