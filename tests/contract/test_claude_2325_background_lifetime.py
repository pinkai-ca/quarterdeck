"""Synthetic task notices in forms measured on private Claude Code 2.1.325 captures."""
from datetime import datetime, timedelta, timezone
import json
import os
import re
from types import SimpleNamespace

import pytest

from quarterdeck import idle, inbox_watch, post, ship as shipmod, standing
from quarterdeck.harness import claude
from quarterdeck.harness.json_record import release

AT = datetime(2032, 4, 5, 6, 7, tzinfo=timezone.utc)


def row(kind, second, **fields):
    value = dict(type=kind, timestamp=(AT + timedelta(seconds=second)).isoformat(), **fields)
    if kind != 'queue-operation':
        value['version'] = '2.1.325'
    return value


def call(identity='synthetic-call', command='synthetic command'):
    return dict(type='tool_use', id=identity, name='Bash', input=dict(command=command, run_in_background=True))


def result(second=2, identity='synthetic-call', task='synthetic-task'):
    return row('user', second, message=dict(role='user', content=[dict(
        type='tool_result', tool_use_id=identity, content='synthetic result', is_error=False)]),
        toolUseResult=dict(backgroundTaskId=task))


def notice(status='completed', identity='synthetic-call', task='synthetic-task'):
    return ('<task-notification>\n'
            f'<task-id>{task}</task-id>\n<tool-use-id>{identity}</tool-use-id>\n'
            f'<output-file>/synthetic/output</output-file>\n<status>{status}</status>\n'
            '<summary>synthetic summary</summary>\n</task-notification>')


def carrier(kind, text, second=5):
    if kind in ('enqueue', 'remove'):
        return row('queue-operation', second, operation=kind, content=text,
                   **(dict(reason='absorbed_mid_turn', commandUuid='synthetic-command', deliveryId='synthetic-delivery')
                      if kind == 'remove' else {}))
    if kind == 'attachment':
        # The attachment keeps the original enqueue time, even after the later removal row.
        return row('attachment', 4, attachment=dict(type='queued_command', prompt=text,
            commandMode='task-notification', origin=dict(kind='task-notification', producer='session-task'),
            source_uuid='synthetic-command', delivery_id='synthetic-delivery', runId='synthetic-run'))
    return row('user', second, origin=dict(kind='task-notification', producer='session-task', runId='synthetic-run'),
               message=dict(role='user', content=text))


def initial(command='synthetic command'):
    return [row('user', 0, message=dict(role='user', content='synthetic prompt')),
            row('assistant', 1, message=dict(role='assistant', content=[call(command=command)])), result(),
            row('system', 3, subtype='turn_duration')]


def write(record, rows, mode='w'):
    record.parent.mkdir(parents=True, exist_ok=True)
    with record.open(mode) as stream:
        stream.writelines(json.dumps(entry) + '\n' for entry in rows)


@pytest.mark.parametrize('status', ['completed', 'failed'])
@pytest.mark.parametrize('later', ['remove', 'attachment', 'user'])
def test_enqueue_ends_the_task_once_before_each_later_carrier(tmp_path, status, later):
    record = tmp_path / 'record.jsonl'; text = notice(status)
    write(record, initial())
    assert claude.state(record).pending_tools == ('Bash',)
    write(record, [carrier('enqueue', text, 4)], 'a')
    queued = claude.state(record)
    assert queued.pending_tools == () and queued.active_tools == ()
    assert queued.queued == (text,)
    assert idle.check(queued).dimension == 'queue-pending'
    reader = claude._Reader(record); reader.state()
    assert reader.background.tasks['synthetic-call'].latest == AT + timedelta(seconds=4)
    write(record, [carrier(later, text)], 'a')
    ended = claude.state(record)
    assert ended.pending_tools == () and ended.active_tools == ()
    fresh = claude._Reader(record); fresh.state()
    assert fresh.background.tasks['synthetic-call'].phase == 'ended'
    assert fresh.background.tasks['synthetic-call'].latest == AT + timedelta(seconds=4)
    release(record)
    assert claude.state(record) == ended


def test_the_measured_mid_turn_chain_is_idle_after_the_turn_ends(tmp_path):
    record = tmp_path / 'record.jsonl'; text = notice()
    write(record, [*initial()[:3], carrier('enqueue', text, 4), carrier('remove', text, 5),
                   carrier('attachment', text), row('system', 6, subtype='turn_duration')])
    state = claude.state(record)
    assert state.turn_open is False and state.pending_tools == () and state.queued == ()
    assert idle.check(state) is None
    assert len(state.completed_turns) == 1


def registered_watch(drydock, monkeypatch, commands=None):
    ship = shipmod.load(drydock)
    brief = standing.render(ship, ship.seat('desk')).read_text()
    command = re.search(r'Messages reach you through your watch: run `([^`]+)`', brief)[1]
    record = ship.runtime / 'seats/desk/home/projects/synthetic/record.jsonl'
    rows = initial(command)
    if commands:
        rows[1]['message']['content'].extend(call(identity, text) for identity, text in commands)
        rows[2:2] = [result(2, identity, 'task-'+identity) for identity, _ in commands]
    write(record, rows)
    launch = ship.runtime / 'seats/desk/launch.json'; launch.parent.mkdir(parents=True, exist_ok=True)
    launch.write_text(json.dumps(dict(pid=os.getppid(), at=(AT-timedelta(seconds=1)).isoformat())))
    monkeypatch.setattr(post, 'now', lambda: AT+timedelta(seconds=2))
    registration = inbox_watch.register(ship.runtime, 'desk')
    return SimpleNamespace(ship=ship, name='desk'), record, registration


def test_a_completed_turn_with_one_exact_registered_watch_is_resting(drydock, monkeypatch):
    seat, record, registration = registered_watch(drydock, monkeypatch)
    state = claude.state(record)
    assert state.turn_open is False and state.pending_tools == ('Bash',)
    assert state.active_tools[0].inbox_watch_started == AT+timedelta(seconds=1)
    assert idle.execution(seat, state).pending_tools == ()
    assert idle.check(state, seat=seat) is None
    inbox_watch.retire(seat.ship.runtime, seat.name, registration)


def test_another_open_background_command_stays_pending_beside_the_watch(drydock, monkeypatch):
    seat, record, registration = registered_watch(drydock, monkeypatch, [('synthetic-other', 'synthetic command')])
    state = claude.state(record)
    adjusted = idle.execution(seat, state)
    assert adjusted.pending_tools == ('Bash',)
    assert all(tool.inbox_watch_started is None for tool in adjusted.active_tools)
    assert idle.check(state, seat=seat).dimension == 'tools-pending'
    inbox_watch.retire(seat.ship.runtime, seat.name, registration)


def test_a_bare_command_is_pending_even_with_a_live_own_registration(drydock, monkeypatch):
    seat, record, registration = registered_watch(drydock, monkeypatch)
    rows = initial('qd inbox --wait'); write(record, rows)
    state = claude.state(record)
    assert state.active_tools[0].inbox_watch_started is None
    assert idle.check(state, seat=seat).dimension == 'tools-pending'
    inbox_watch.retire(seat.ship.runtime, seat.name, registration)


@pytest.mark.parametrize('later', ['remove', 'attachment'])
def test_unmeasured_carrier_versions_stay_unknown(tmp_path, later):
    text = notice(); entry = carrier(later, text)
    if later == 'remove':
        # Queue rows inherit their nearest native version; that form is not measured on 322.
        previous = row('system', 4, subtype='synthetic-metadata'); previous['version'] = '2.1.322'
        rows = [*initial(), carrier('enqueue', text, 4), previous, entry]
    else:
        entry['version'] = '2.1.322'; rows = [*initial(), carrier('enqueue', text, 4), entry]
    record = tmp_path / 'record.jsonl'; write(record, rows)
    assert claude.state(record).pending_tools is None


def test_a_conflicting_status_does_not_disappear_as_a_duplicate(tmp_path):
    record = tmp_path / 'record.jsonl'
    write(record, [*initial(), carrier('enqueue', notice(), 4), carrier('remove', notice('failed'))])
    assert claude.state(record).pending_tools is None
