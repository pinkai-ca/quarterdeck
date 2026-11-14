"""The Monitor tool's event notice, and notices the background reader cannot parse.

Measured on Claude Code 2.1.317 records, shape only: a Monitor call whose result carries {persistent, taskId,
timeoutMs}, and its <task-notification> with exactly one <event> after its summary, in two forms: task-id alone
while it runs, or task-id, tool-use-id, output-file and status when it ends. Its carriers are the queue enqueue
and remove, the queued_command attachment with origin {kind, producer}, and the user row. A notice the reader
cannot parse used to turn the whole record's tool state unknown; it now holds only a Bash task it names.
Identities and text are synthetic.
"""
from __future__ import annotations

import pytest

from quarterdeck import idle
from tests.unit.test_claude_background_ordinary import CALL, TASK, call, ended, prompt, read, result, row, text

MONITOR, MONITOR_TASK = 'call-monitor-larch', 'task-monitor-larch'
VERSION = '2.1.317'


def monitor_started(second=4):
    return [row('assistant', second, message=dict(role='assistant', content=[dict(
                type='tool_use', id=MONITOR, name='Monitor',
                input=dict(command='synthetic watch', description='synthetic', timeout_ms=300000))])),
            result(second + 1, MONITOR, value=dict(persistent=True, taskId=MONITOR_TASK, timeoutMs=300000))]


def event(form='running', task=MONITOR_TASK, identity=MONITOR, status='completed'):
    header = (f'<task-id>{task}</task-id>\n' if form == 'running' else
              f'<task-id>{task}</task-id>\n<tool-use-id>{identity}</tool-use-id>\n'
              f'<output-file>/synthetic/output</output-file>\n<status>{status}</status>\n')
    return f'<task-notification>\n{header}<summary>synthetic summary</summary>\n<event>synthetic event</event>\n</task-notification>'


def carried(payload, carrier, second, version=VERSION):
    if carrier in ('enqueue', 'remove'):
        return row('queue-operation', second, operation=carrier, content=payload)
    if carrier == 'queue':  # as measured: the enqueue, then its remove when the program takes it
        return [row('queue-operation', second, operation='enqueue', content=payload),
                row('queue-operation', second + 0.01, operation='remove', content=payload)]
    if carrier == 'user':
        entry = row('user', second, origin=dict(kind='task-notification'), message=dict(role='user', content=payload))
    else:
        entry = row('attachment', second, attachment=dict(type='queued_command', commandMode='task-notification',
                                                           origin=dict(kind='task-notification', producer='session-task'),
                                                           prompt=payload))
    entry['version'] = version
    return entry


def flat(found):
    return found if isinstance(found, list) else [found]


def replied(second):
    return [row('assistant', second, message=dict(role='assistant', content=[dict(type='text', text='synthetic')])),
            ended(second + 1)]


def bash_done():
    """A person's input, a background Bash task started and ended by its own notice, the turn ended."""
    return [prompt(0), call(1), result(2), ended(3), *carried(text(), 'queue', 3.5)]


@pytest.mark.parametrize('carrier', ['queue', 'attachment', 'user'])
@pytest.mark.parametrize('form', ['running', 'ended'])
def test_a_monitor_event_notice_leaves_the_tool_state_known_and_idle(tmp_path, carrier, form):
    rows = bash_done() + [prompt(4.5, 'synthetic watch request'), *monitor_started(5), ended(6.5),
                          *flat(carried(event(form), carrier, 7)), *replied(8)]
    _, state = read(tmp_path, rows)
    assert state.pending_tools == (), 'the Monitor notice is the Monitor\'s own, not unknown evidence'
    assert idle.check(state) is None


@pytest.mark.parametrize('form', ['running', 'ended'])
def test_a_monitor_notice_never_settles_a_running_bash_task(tmp_path, form):
    rows = [prompt(0), call(1), result(2), ended(3), prompt(3.5, 'synthetic watch request'), *monitor_started(4),
            ended(5.5), *carried(event(form), 'queue', 6), *replied(7)]
    _, state = read(tmp_path, rows)
    assert state.pending_tools == ('Bash',), 'the Bash task still runs; the Monitor notice ended nothing of it'


@pytest.mark.parametrize('form', ['running', 'ended'])
def test_a_monitor_shaped_notice_naming_the_bash_task_makes_it_unknown_never_ended(tmp_path, form):
    payload = event(form, task=TASK, identity=CALL)
    _, state = read(tmp_path, [prompt(0), call(1), result(2), ended(3), carried(payload, 'enqueue', 4)])
    assert state.pending_tools is None, 'it names the Bash task: that task is unknown, never settled'


AGENT = ('<task-notification>\n<task-id>task-agent-larch</task-id>\n<tool-use-id>call-agent-larch</tool-use-id>\n'
         '<output-file>/synthetic/output</output-file>\n<status>completed</status>\n<summary>synthetic</summary>\n'
         '<note>synthetic</note>\n<result>synthetic</result>\n<usage>synthetic</usage>\n</task-notification>')
BROKEN = '<task-notification>\n<task-id>task-other-larch</task-id>\n<summary>synthetic'


@pytest.mark.parametrize('payload', [AGENT, BROKEN, event('running', task='task-unbound-larch')],
                         ids=['agent-completion', 'malformed', 'monitor-of-an-earlier-file'])
def test_an_unparseable_notice_holds_itself_not_the_record(tmp_path, payload):
    _, state = read(tmp_path, bash_done() + [*carried(payload, 'queue', 4), *replied(5)])
    assert state.pending_tools == (), 'a notice naming no tracked Bash task leaves the record known'
    assert idle.check(state) is None


def test_an_unparseable_notice_naming_a_bash_task_holds_that_task(tmp_path):
    payload = AGENT.replace('call-agent-larch', CALL).replace('task-agent-larch', TASK)
    _, state = read(tmp_path, [prompt(0), call(1), result(2), ended(3), carried(payload, 'enqueue', 4)])
    assert state.pending_tools is None, 'the Bash task it names is unknown, never ended'


def test_an_unmeasured_versions_monitor_notice_still_holds_only_itself(tmp_path):
    rows = bash_done() + [prompt(4.5, 'synthetic watch request'), *monitor_started(5), ended(6.5),
                          *flat(carried(event('ended'), 'attachment', 7, version='2.1.399')), *replied(8)]
    _, state = read(tmp_path, rows)
    assert state.pending_tools == ()
