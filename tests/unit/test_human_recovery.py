"""Explicit recovery needs all relevant tool waits; the first displayed tool is insufficient."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import watch
from quarterdeck.harness import get
from quarterdeck.harness.base import Join, State
from quarterdeck.service import Service
from tests.unit.test_opencode_record import database, session, user, assistant, part
from tests.unit.test_fresh_after_order import worker


def claude_record(path, tools, *, sidechain=False):
    rows = [{'type': 'user', 'timestamp': '2030-01-01T00:00:00Z', 'message': {'content': 'Synthetic request'}}]
    for index, (name, args) in enumerate(tools):
        rows.append({'type': 'assistant', 'timestamp': '2030-01-01T00:00:01Z',
                     'isSidechain': sidechain and index > 0,
                     'message': {'id': str(index), 'content': [
                         {'type': 'tool_use', 'id': str(index), 'name': name, 'input': args}]}})
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))


def seat_for(path, name):
    harness = get(name)
    calls = []
    seat = SimpleNamespace(harness=harness, record=lambda: path, join=lambda: Join(path),
                           session=SimpleNamespace(human=False),
                           pane=lambda: SimpleNamespace(dead=False), launched=lambda: None,
                           is_working=lambda: harness.state(path).turn_open,
                           require_restart_target=lambda: None,
                           restart=lambda: calls.append('restart') or 'restarted',
                           fresh=lambda: calls.append('fresh') or 'fresh')
    return seat, calls


@pytest.mark.parametrize('resume', [False, True])
def test_claude_question_only_is_a_human_wait_and_explicit_recovery_never_ends_the_turn(tmp_path, resume):
    path = tmp_path / 'record'
    claude_record(path, [('AskUserQuestion', {})])
    seat, calls = seat_for(path, 'claude')
    before = seat.harness.read(path)
    assert watch.activity(seat).state == 'needs a human'
    assert Service.relaunch(None, seat, resume)
    assert calls == ['restart' if resume else 'fresh']
    assert seat.harness.read(path) == before and before[0].turn_open is True
    assert before[0].completed_turns == () and before[0].pending_tools == ('AskUserQuestion',)


@pytest.mark.parametrize('case', ['parallel', 'sidechain', 'background', 'partial', 'unknown', 'unreadable', 'unknown-row', 'invalid-input'])
def test_question_display_cannot_hide_execution_or_unknown_record_evidence(tmp_path, monkeypatch, case):
    path = tmp_path / 'record'
    tools = [('AskUserQuestion', {})]
    if case == 'invalid-input':
        tools = [('AskUserQuestion', 'unparsed')]
    if case in ('parallel', 'sidechain'):
        tools.append(('Bash', {'command': 'synthetic'}))
    if case == 'background':
        tools.insert(0, ('Bash', {'run_in_background': True}))
    claude_record(path, tools, sidechain=case == 'sidechain')
    if case == 'partial':
        with path.open('a') as out:
            out.write('{"type":"assistant",')
    if case == 'unknown-row':
        with path.open('a') as out:
            out.write('{"type":"future-execution"}\n')
    seat, calls = seat_for(path, 'claude')
    if case == 'unknown':
        monkeypatch.setattr(type(seat.harness), 'state', staticmethod(lambda _: State(turn_open=None)))
    if case == 'unreadable':
        path.unlink()
    # Even an idle-looking screen / legacy fallback cannot authorize explicit recovery.
    seat.is_working = lambda: False
    assert Service.relaunch(None, seat, False) is None and calls == []


@pytest.mark.parametrize('other', [None, 'shell', 'unknown', 'child', 'child-generating', 'malformed', 'background', 'previous-root'])
def test_opencode_question_recovery_uses_all_parts_and_child_sessions(tmp_path, other):
    path = tmp_path / 'record.db'
    con = database(path)
    session(con)
    user(con, 'input', 'root', 10, 'Synthetic request')
    assistant(con, 'step', 'input', 'root', 20, completed=False, finish='tool-calls')
    part(con, 'question', 'step', 'root', 21,
         {'type': 'tool', 'tool': 'question', 'state': {'status': 'running', 'time': {'start': 1}}})
    if other in ('shell', 'unknown'):
        part(con, 'other', 'step', 'root', 22,
             {'type': 'tool', 'tool': 'shell', 'state': {'status': 'running' if other == 'shell' else 'unrecognized'}})
    if other in ('child', 'child-generating'):
        session(con, 'child', 30, 'root')
        user(con, 'child-input', 'child', 40, 'Synthetic child')
        assistant(con, 'child-step', 'child-input', 'child', 50, completed=False)
        if other == 'child':
            part(con, 'child-tool', 'child-step', 'child', 51,
                 {'type': 'tool', 'tool': 'shell', 'state': {'status': 'running'}})
    if other == 'malformed':
        con.execute('INSERT INTO part VALUES (?,?,?,?,?,?)', ('broken', 'step', 'root', 22, 22, '{'))
    if other == 'background':
        part(con, 'background', 'step', 'root', 22,
             {'type': 'tool', 'tool': 'task', 'state': {'status': 'completed', 'input': {'background': True}}})
    if other == 'previous-root':
        session(con, 'previous-root', -100)
        user(con, 'old-input', 'previous-root', -90, 'Prior synthetic work')
        assistant(con, 'old-step', 'old-input', 'previous-root', -80, completed=False)
    con.commit()
    con.close()
    seat, calls = seat_for(path, 'opencode')
    state = seat.harness.state(path)
    assert state.turn_open is True and state.open_tool is not None
    assert bool(Service.relaunch(None, seat, False)) is (other is None)
    assert calls == (['fresh'] if other is None else [])


def test_default_unknown_set_and_unknown_join_are_not_idle_or_question_only_proof(tmp_path, monkeypatch):
    seat, calls = seat_for(tmp_path / 'record', 'claude')
    monkeypatch.setattr(type(seat.harness), 'state', staticmethod(lambda _: State(turn_open=True, open_tool='AskUserQuestion')))
    assert not seat.harness.waiting_for_human(seat.harness.state(None))
    seat.is_working = lambda: False
    assert Service.relaunch(None, seat, False) is None
    seat.join = lambda: Join(None, 'record unreadable: synthetic')
    assert Service.relaunch(None, seat, False) is None and calls == []


@pytest.mark.parametrize('contents', ['', '{"type":"session_meta","payload":{}}\n'])
def test_empty_or_metadata_only_record_does_not_make_an_explicit_relaunch_safe(tmp_path, contents):
    path = tmp_path / 'record'
    path.write_text(contents)
    seat, calls = seat_for(path, 'codex')
    assert Service.relaunch(None, seat, False) is None and calls == []


def test_saved_question_menu_is_an_untouched_obstruction_and_never_exclusivity_proof(monkeypatch):
    from quarterdeck.door import Door
    from tests.contract.test_padded_box import door_for, capture

    text = (Path(__file__).parents[1] / 'fixtures/opencode/1.19.5/question-tool.txt').read_text()
    door = door_for('opencode', text, monkeypatch)
    calls = []
    door.tmux.keys = lambda *args: calls.append(args)
    door.tmux.paste = lambda *args, **kwargs: calls.append(args)
    assert door.screen().interruption.name == 'native question tool'
    assert door.reading().clears == 'needs a human'
    assert door.deliver('synthetic request').result == 'withheld' and calls == []
    door.seat.session.human = True
    assert door.deliver('synthetic request').result == 'withheld' and calls == []
    for changed in [text.replace('↑↓ select', 'unknown action'), text.replace('Type your own answer', 'Approve changes')]:
        assert get('opencode').screen(changed).interruption is None
        assert not door_for('opencode', changed, monkeypatch).reading().open
    draft = door_for('opencode', capture('opencode', 'a human draft'), monkeypatch)
    assert draft.reading().reason == 'text in the box'


def test_closed_record_with_complete_empty_tools_retains_ordinary_explicit_recovery(tmp_path):
    path = tmp_path / 'record'
    claude_record(path, [])
    with path.open('a') as out:
        out.write(json.dumps({'type': 'system', 'subtype': 'turn_duration', 'timestamp': '2030-01-01T00:00:02Z'}) + '\n')
    seat, calls = seat_for(path, 'claude')
    assert Service.relaunch(None, seat, True) == 'restarted' and calls == ['restart']


@pytest.mark.parametrize('extra', [None, 'previous-call', 'spawn_agent', 'exec', 'functions.exec_command', 'malformed-name'])
def test_codex_question_proof_keeps_prior_calls_and_child_execution(tmp_path, extra):
    path = tmp_path / 'record'
    def event(kind, payload):
        return {'type': kind, 'timestamp': '2030-01-01T00:00:00Z', 'payload': payload}
    rows = [event('event_msg', {'type': 'task_started'})]
    if extra:
        name = None if extra == 'malformed-name' else 'shell' if extra == 'previous-call' else extra
        rows.append(event('response_item', {'type': 'function_call', 'name': name,
                                           'call_id': 'old', 'arguments': '{}'}))
        rows.append(event('event_msg', {'type': 'task_started'}))
    rows.append(event('response_item', {'type': 'function_call', 'name': 'request_user_input', 'call_id': 'ask',
                                       'arguments': json.dumps({'questions': [{'id': 'choice', 'header': 'Choice',
                                           'question': 'Choose a synthetic option', 'options': [
                                               {'label': 'First', 'description': 'First option'},
                                               {'label': 'Second', 'description': 'Second option'}]}]})}))
    path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    seat, calls = seat_for(path, 'codex')
    assert bool(Service.relaunch(None, seat, False)) is (extra is None)
    assert calls == (['fresh'] if extra is None else [])


@pytest.mark.parametrize('worker', ['opencode'], indirect=True)
def test_explicit_question_recovery_exception_never_releases_automatic_fresh_or_failover(worker, monkeypatch):
    from dataclasses import replace
    from datetime import timedelta
    from quarterdeck import post

    w = worker
    w.record.begin()
    part(w.record.con, 'ask', 'report-step', 'root', 25,
         {'type': 'tool', 'tool': 'question', 'state': {'status': 'running'}})
    # Use the actual assistant id chosen by the synthetic helper, not an orphaned part.
    mid = w.record.con.execute("SELECT id FROM message WHERE session_id='root' ORDER BY time_created DESC LIMIT 1").fetchone()[0]
    w.record.con.execute("UPDATE part SET message_id=? WHERE id='ask'", (mid,))
    w.record.result()
    w.record.con.commit()
    state = w.seat.harness.state(w.record.path)
    assert w.seat.harness.waiting_for_human(state)
    order = w.svc.store.new('order', 'issuer', 'worker', 'Synthetic request')
    w.svc.store.new('report', 'worker', 'issuer', 'Synthetic report', re=order.id)
    w.seat.spec = replace(w.seat.spec, pools=('local', 'spare'))
    w.seat.ship.pools['spare'] = replace(w.seat.pool, name='spare')
    monkeypatch.setattr(w.svc.ledger, 'walled', lambda name, *args, **kwargs: name == 'local')
    monkeypatch.setattr(w.seat, 'fresh', lambda: pytest.fail('automatic fresh gained explicit recovery exception'))
    monkeypatch.setattr(w.seat, 'failover_to', lambda _: pytest.fail('automatic failover gained explicit recovery exception'))
    w.svc.fresh_after_order()
    w.svc.failover()
    assert w.seat.harness.state(w.record.path) == state
