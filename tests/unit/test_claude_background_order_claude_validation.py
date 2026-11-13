"""A background task's notification written before its own call rows binds when the call arrives (Claude Code 2.1.317).

When the inbox watch returns at once because mail is waiting, Claude Code writes the queue-operation enqueue that
carries the completed task notification before the call's tool_use and its result. The reader holds such a
notification by root call id and binds it when the call's result creates the task. One that never meets its call
by the record's end leaves the record's tools unknown, as before, and a subagent call is never bound from the main
file. Rows are synthetic and recreate only the measured structure and order.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from quarterdeck.harness import claude

AT = datetime(2032, 9, 10, 11, 12, tzinfo=timezone.utc)
VERSION = '2.1.317'
CALL, TASK = 'call-juniper', 'task-juniper'
WATCH = 'qd inbox --wait'


def row(kind, second, **fields):
    entry = dict(type=kind, timestamp=(AT + timedelta(seconds=second)).isoformat(), **fields)
    if kind != 'queue-operation':
        entry.update(version=VERSION, isSidechain=fields.get('isSidechain', False))
    return entry


def prompt(second=0):
    return row('user', second, message=dict(role='user', content='synthetic order juniper'))


def call(second, identity=CALL, sidechain=False):
    return row('assistant', second, isSidechain=sidechain, message=dict(role='assistant', content=[dict(
        type='tool_use', id=identity, name='Bash',
        input=dict(command=WATCH, description='synthetic', run_in_background=True))]))


def result(second, identity=CALL, task=TASK, sidechain=False):
    return row('user', second, isSidechain=sidechain, message=dict(role='user', content=[dict(
        type='tool_result', tool_use_id=identity, content='synthetic handle', is_error=False)]),
        toolUseResult=dict(stdout='', stderr='', interrupted=False, isImage=False, noOutputExpected=False,
                           backgroundTaskId=task))


def enqueue(second, identity=CALL, task=TASK, status='completed', note=False):
    text = ('<task-notification>\n'
            f'<task-id>{task}</task-id>\n<tool-use-id>{identity}</tool-use-id>\n'
            '<output-file>/synthetic/output</output-file>\n'
            f'<status>{status}</status>\n<summary>synthetic summary</summary>\n'
            + ('<note>synthetic resume uncertainty</note>\n' if note else '') + '</task-notification>')
    return row('queue-operation', second, operation='enqueue', content=text)


def ended(second):
    return row('system', second, subtype='turn_duration')


def read(tmp_path, rows):
    record = tmp_path / 'record-juniper.jsonl'
    record.write_text(''.join(json.dumps(entry) + '\n' for entry in rows))
    return claude.state(record)


def test_the_measured_order_binds_the_early_notification_and_the_watch_reads_complete(tmp_path):
    """The desk's order: enqueue (latest timestamp) first, then the call and its result, then the turn's end."""
    state = read(tmp_path, [prompt(0), enqueue(4.774), call(4.194), result(4.216), ended(5)])
    assert state.turn_open is False
    assert state.pending_tools == () and state.active_tools == (), 'known complete: nothing left running'


def test_the_file_order_case_still_reads_as_before(tmp_path):
    state = read(tmp_path, [prompt(0), call(1), result(2), enqueue(3), ended(4)])
    assert state.pending_tools == () and state.active_tools == ()


def test_before_any_notification_the_watch_is_pending_and_named(tmp_path):
    state = read(tmp_path, [prompt(0), call(1), result(2), ended(3)])
    assert state.pending_tools == ('Bash',) and state.active_tools[0].inbox_watch_started is not None


def test_a_notification_that_never_meets_its_call_leaves_the_tools_unknown(tmp_path):
    state = read(tmp_path, [prompt(0), enqueue(1, identity='call-absent'), call(2), result(3), ended(4)])
    assert state.pending_tools is None, 'never guessed: an unbound notification is unknown, as before'


def test_a_subagent_call_is_never_bound_from_the_main_files_notification(tmp_path):
    rows = [prompt(0), enqueue(1, identity='call-sub', task='task-sub'),
            call(2, identity='call-sub', sidechain=True), result(3, identity='call-sub', task='task-sub', sidechain=True),
            ended(4)]
    assert read(tmp_path, rows).pending_tools is None


@pytest.mark.parametrize('early', [
    enqueue(1.5, task='task-other'),  # a different task id for the same call
    enqueue(1.5, status='stopped', note=True),  # resume's stopped-with-note never says the command ended
    enqueue(1.0),  # dated before the task's own start (its result row)
], ids=['other-task', 'stopped-with-note', 'before-start'])
def test_an_early_notification_that_does_not_prove_the_end_leaves_the_task_unknown(tmp_path, early):
    state = read(tmp_path, [prompt(0), early, call(1.1), result(1.2), ended(3)])
    assert state.pending_tools is None


def test_an_early_notification_for_a_call_whose_result_never_binds_stays_unknown(tmp_path):
    rows = [prompt(0), enqueue(3), call(1), ended(4)]  # the call's result never arrives
    assert read(tmp_path, rows).pending_tools is None
