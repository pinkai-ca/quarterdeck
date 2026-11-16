"""Synthetic copies of root/child evidence measured on private native 2.1.325 captures."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from quarterdeck import idle
from quarterdeck.harness import claude, reading_scope

AT = datetime(2032, 4, 5, 6, 7, tzinfo=timezone.utc)
SESSION = 'synthetic-session'
CHILD = 'synthetic-child'


def row(kind, second, child=False, **fields):
    value = dict(type=kind, version='2.1.325', sessionId=SESSION,
                 timestamp=(AT+timedelta(seconds=second)).isoformat(), **fields)
    if child:
        value.update(isSidechain=True, agentId=CHILD)
    return value


def message(kind, second, content, child=False, **fields):
    return row(kind, second, child, message=dict(role=kind, content=content), **fields)


def use(call, name='Bash', **args):
    return dict(type='tool_use', id=call, name=name, input=args)


def result(call):
    return dict(type='tool_result', tool_use_id=call, content='synthetic result', is_error=False)


def root_rows():
    return [message('user', 0, 'synthetic input'),
            message('assistant', 1, [use('synthetic-agent-call', 'Agent', description='synthetic',
                                       prompt='synthetic', subagent_type='general-purpose')]),
            message('user', 2, [result('synthetic-agent-call')], toolUseResult=dict(
                agentId=CHILD, isAsync=True, status='async_launched', description='synthetic',
                prompt='synthetic', outputFile='/synthetic/agent.output', canContinueAgent=True,
                canReadOutputFile=True, resolvedModel='synthetic-model')),
            row('system', 10, subtype='turn_duration')]


def child_rows(background=True, ended=4):
    rows = [message('user', 2, 'synthetic child input', True)]
    if background:
        rows.extend([message('assistant', 3, [use('synthetic-child-call', command='synthetic command',
                                                run_in_background=True)], True),
                     message('user', 3.5, [result('synthetic-child-call')], True,
                             toolUseResult=dict(backgroundTaskId='synthetic-child-task'))])
    if ended is not None:
        entry = message('assistant', ended, [dict(type='text', text='synthetic reply')], True)
        entry['message']['stop_reason'] = 'end_turn'
        rows.append(entry)
    return rows


def notice(second=6, call='synthetic-child-call', task='synthetic-child-task', status='completed'):
    text = ('<task-notification>\n'
            f'<task-id>{task}</task-id>\n<tool-use-id>{call}</tool-use-id>\n'
            f'<output-file>/synthetic/output</output-file>\n<status>{status}</status>\n'
            '<summary>synthetic summary</summary>\n</task-notification>')
    return row('queue-operation', second, operation='enqueue', content=text)


def write(path, rows, mode='w'):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode) as stream:
        stream.writelines(json.dumps(entry)+'\n' for entry in rows)


def fixture(tmp_path, children=None, root=None):
    record = tmp_path/(SESSION+'.jsonl')
    child = record.with_suffix('')/'subagents'/('agent-'+CHILD+'.jsonl')
    write(record, root_rows() if root is None else root)
    if children is not False:
        write(child, child_rows() if children is None else children)
    return record, child


def consume(entry, second=7):
    return row('queue-operation', second, operation='remove', content=entry['content'], reason='absorbed_mid_turn')


def test_an_async_agent_result_keeps_its_child_pending(tmp_path):
    record, _ = fixture(tmp_path, children=child_rows(False, None))
    state = claude.state(record)
    assert state.pending_tools == ('Agent',)
    assert idle.check(state).dimension == 'tools-pending'


def test_agent_with_no_child_file_cannot_be_idle(tmp_path):
    record, _ = fixture(tmp_path, children=False)
    state = claude.state(record)
    assert state.pending_tools == ('Agent',)
    assert idle.check(state).dimension == 'tools-pending'


def test_ended_child_without_background_work_settles_its_agent(tmp_path):
    record, _ = fixture(tmp_path, children=child_rows(False))
    state = claude.state(record)
    assert state.pending_tools == () and state.active_tools == ()
    assert idle.check(state) is None


def test_a_child_return_does_not_end_its_background_command(tmp_path):
    record, _ = fixture(tmp_path)
    state = claude.state(record)
    assert state.pending_tools == ('Bash', 'Agent')
    assert all(tool.inbox_watch_started is None for tool in state.active_tools)
    assert idle.check(state).dimension == 'tools-pending'


def test_root_notice_joins_child_start_but_waits_for_the_reawakened_turn(tmp_path):
    entry = notice()
    record, child = fixture(tmp_path, root=[*root_rows(), entry, consume(entry)])
    state = claude.state(record)
    assert state.pending_tools == ('Agent',)
    write(child, [message('user', 7, 'synthetic callback', True)], 'a')
    assert claude.state(record).pending_tools == ('Agent',)
    ending = message('assistant', 8, [dict(type='text', text='synthetic done')], True)
    ending['message']['stop_reason'] = 'end_turn'
    write(child, [ending], 'a')
    state = claude.state(record)
    assert state.pending_tools == ()
    assert idle.check(state) is None
    assert state == claude._Reader(record).state()


def test_interim_completed_agent_notice_never_settles_pending_child_work(tmp_path):
    entry = notice(call='synthetic-agent-call', task=CHILD)
    entry['content'] = entry['content'].replace('</task-notification>',
        '<note>synthetic interim result</note><result>synthetic reply</result>'
        '<usage><tool_uses>1</tool_uses></usage></task-notification>')
    record, _ = fixture(tmp_path, root=[*root_rows(), entry, consume(entry)])
    state = claude.state(record)
    assert state.pending_tools == ('Bash', 'Agent')


def test_notice_nowhere_stays_unknown(tmp_path):
    entry = notice(call='synthetic-nowhere')
    record, _ = fixture(tmp_path, children=child_rows(False), root=[*root_rows(), entry, consume(entry)])
    assert claude.state(record).pending_tools is None


@pytest.mark.parametrize('difference', ['session', 'agent', 'task', 'version', 'result'])
def test_child_identity_and_its_own_background_result_are_required(tmp_path, difference):
    children = child_rows(ended=8)
    if difference in ('session', 'agent'):
        for entry in children:
            entry['sessionId' if difference == 'session' else 'agentId'] = 'synthetic-other'
    elif difference == 'task':
        children[2]['toolUseResult']['backgroundTaskId'] = 'synthetic-other'
    elif difference == 'version':
        children[1]['version'] = '2.1.326'
    else:
        children.pop(2)
    entry = notice()
    record, _ = fixture(tmp_path, children=children, root=[*root_rows(), entry, consume(entry)])
    assert claude.state(record).pending_tools is None


def test_child_only_updates_invalidate_a_presentation_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(reading_scope, '_TIMESTAMP_TICK_NS', 0)
    record, child = fixture(tmp_path, children=child_rows(False))
    with reading_scope.reading_pass():
        assert claude.ClaudeHarness.state(record).pending_tools == ()
        write(child, [message('user', 5, 'synthetic reawakening', True)], 'a')
        assert claude.ClaudeHarness.state(record).pending_tools == ('Agent',)


def launch(record, second=5, *, joined=True):
    folder = record.parent.parent.parent.parent
    data = dict(at=(AT+timedelta(seconds=second)).isoformat(), harness='claude', version='2.1.325',
                resume=SESSION, pid=12345, pool='synthetic-pool')
    (folder/'launch.json').write_text(json.dumps(data))
    (folder/'record').write_text(str(record)+'\n')
    if joined:
        (folder/'record-launches.jsonl').write_text(json.dumps(dict(record=str(record),at=data['at'],
            pool=data['pool'],harness='claude'))+'\n')


def test_pending_child_before_a_proven_current_launch_ends_by_relaunch(tmp_path):
    root = tmp_path/'runtime/seats/synthetic/home/projects/synthetic'
    record, _ = fixture(root)
    launch(record)
    reader = claude._Reader(record);state = reader.state()
    task = reader.children.tasks['synthetic-child-call']
    assert task.phase == 'ended-by-relaunch' and task.terminal == 'relaunch'
    assert task.latest == AT+timedelta(seconds=5)
    assert state.pending_tools == () and idle.check(state) is None


@pytest.mark.parametrize('difference', ['call-after-launch', 'unjoined-launch'])
def test_relaunch_does_not_settle_new_or_unproved_child_work(tmp_path, difference):
    root = tmp_path/'runtime/seats/synthetic/home/projects/synthetic'
    record, _ = fixture(root)
    launch(record, 2.5 if difference == 'call-after-launch' else 5, joined=difference!='unjoined-launch')
    state = claude.state(record)
    assert state.pending_tools == ('Bash', 'Agent')


def test_task_and_unmeasured_agent_forms_remain_unknown(tmp_path):
    rows = root_rows();rows[1]['message']['content'][0]['name'] = 'Task'
    record, _ = fixture(tmp_path, children=child_rows(False), root=rows)
    assert claude.state(record).pending_tools is None


def test_an_older_child_return_cannot_settle_a_new_agent_invocation(tmp_path):
    rows = root_rows()
    rows[1]['timestamp'] = (AT+timedelta(seconds=8)).isoformat()
    rows[2]['timestamp'] = (AT+timedelta(seconds=9)).isoformat()
    record, _ = fixture(tmp_path, children=child_rows(False), root=rows)
    assert claude.state(record).pending_tools == ('Agent',)


def test_a_child_record_arriving_after_the_root_notice_refreshes_the_read(tmp_path, monkeypatch):
    monkeypatch.setattr(reading_scope, '_TIMESTAMP_TICK_NS', 0)
    entry = notice()
    record, child = fixture(tmp_path, children=False, root=[*root_rows(), entry, consume(entry)])
    with reading_scope.reading_pass():
        assert claude.ClaudeHarness.state(record).pending_tools is None
        write(child, child_rows(ended=8))
        assert claude.ClaudeHarness.state(record).pending_tools == ()


@pytest.mark.parametrize('missing', [False, True])
def test_unproved_launch_time_does_not_end_a_child_command(tmp_path, missing):
    root = tmp_path/'runtime/seats/synthetic/home/projects/synthetic'
    record, _ = fixture(root)
    launch(record)
    folder = record.parent.parent.parent.parent
    data = json.loads((folder/'launch.json').read_text())
    data['at'] = '2032-04-05T06:07:05'
    if missing:
        data.pop('at')
    (folder/'launch.json').write_text(json.dumps(data))
    (folder/'record-launches.jsonl').write_text(json.dumps(dict(record=str(record), at=data.get('at'),
        pool=data['pool'], harness='claude'))+'\n')
    assert claude.state(record).pending_tools == ('Bash', 'Agent')


def running(second):
    return row('attachment', second, True, attachment=dict(type='task_status', taskType='local_bash',
               taskId='synthetic-child-task', status='running', description='synthetic',
               outputFilePath='/synthetic/output', deltaSummary=None,
               shell=dict(command='synthetic command', kind='bash', toolUseId='synthetic-child-call')))


# The background evidence rule meets the child join here: anything seen of a child's command after the launch,
# dated after it or not dated at all, holds the command; it never ends by the relaunch.
@pytest.mark.parametrize('seen', ['dated-after-launch', 'undated'])
def test_a_child_command_seen_after_the_launch_is_never_ended_by_it(tmp_path, seen):
    root = tmp_path/'runtime/seats/synthetic/home/projects/synthetic'
    status = running(6)
    if seen == 'undated':
        del status['timestamp']
    children = child_rows(ended=None)
    children.append(status)
    record, _ = fixture(root, children=children)
    launch(record)
    reader = claude._Reader(record)
    state = reader.state()
    task = reader.children.tasks['synthetic-child-call']
    assert task.phase != 'ended-by-relaunch'
    assert state.pending_tools is None if seen == 'undated' else state.pending_tools == ('Bash', 'Agent')


def test_a_child_command_seen_only_before_the_launch_still_ends_by_it(tmp_path):
    root = tmp_path/'runtime/seats/synthetic/home/projects/synthetic'
    children = child_rows(ended=4.5)
    children.insert(3, running(4))
    record, _ = fixture(root, children=children)
    launch(record)
    reader = claude._Reader(record)
    state = reader.state()
    assert reader.children.tasks['synthetic-child-call'].phase == 'ended-by-relaunch'
    assert state.pending_tools == () and idle.check(state) is None


def test_a_record_not_named_as_a_native_root_has_no_children_to_join(tmp_path):
    """Only '<session>.jsonl' has the measured child folder beside it; any other name joins nothing, and its own
    background commands read as on main."""
    named = tmp_path/'synthetic-record'
    write(named, [message('user', 0, 'synthetic input'),
                  message('assistant', 1, [use('synthetic-call', command='synthetic', run_in_background=True)]),
                  message('user', 2, [result('synthetic-call')], toolUseResult=dict(backgroundTaskId='synthetic-task')),
                  row('system', 3, subtype='turn_duration')])
    assert claude.state(named).pending_tools == ('Bash',)
