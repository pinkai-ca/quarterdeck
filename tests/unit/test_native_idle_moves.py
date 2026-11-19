"""Native tool observations through service gates, with invented records and inert transport.

Codex, Grok Build, OpenCode and pi name outstanding tools. Antigravity's
reader reports unknown or empty pending tools and has separate open-tool
evidence. An open turn may hold a move before its tool state is considered.
"""

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from quarterdeck import credential, move_hold, trusted_files
from quarterdeck.seat import Launched, Seat
from quarterdeck.service import Service
from quarterdeck.ship import Pool, SeatSpec, Session, Ship
from quarterdeck.tmux import Pane
from quarterdeck.harness.synthetic_record import PiShape
from tests.antigravity_shapes import AntigravityShape, _pb_str, _pb_uint

from . import test_codex_completed_items_ordinary as codex_shape
from . import test_opencode_record as opencode_shape


SESSION = 'synthetic-native-move'
MODEL = 'synthetic-model'
TOOL = 'example'


def codex_record(home, mode):
    call = codex_shape.row('response_item', {
        'type': 'function_call', 'call_id': 'synthetic-call', 'name': TOOL, 'arguments': '{}',
    })
    rows = codex_shape.history(call)
    rows[0]['payload']['id'] = SESSION
    if mode == 'unknown':
        rows.append(codex_shape.row('event_msg', {'type': 'unmeasured-tool-event'}))
    if mode == 'idle':
        rows.append(codex_shape.row('response_item', {
            'type': 'function_call_output', 'call_id': 'synthetic-call', 'output': 'Made-up result',
        }))
    return codex_shape.write(home, rows)


def grok_record(home, mode):
    updates = [
        dict(sessionUpdate='user_message_chunk', content=dict(type='text', text='Made-up input')),
        dict(sessionUpdate='tool_call', toolCallId='synthetic-call', title=TOOL, status='in_progress'),
    ]
    if mode != 'pending':
        updates.append(dict(sessionUpdate='turn_completed', prompt_id='synthetic-prompt'))
    if mode == 'idle':
        updates.append(dict(sessionUpdate='tool_call_update', toolCallId='synthetic-call', status='completed'))
    rows = [dict(timestamp=2208988800 + index, method='session/update',
                 params=dict(sessionId=SESSION, update=update)) for index, update in enumerate(updates)]
    record = home / 'updates.jsonl'
    record.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    return record


def opencode_record(home, mode):
    record = home / 'data-001' / 'opencode' / 'opencode.db'
    with closing(opencode_shape.database(record)) as db, db:
        opencode_shape.session(db, SESSION)
        opencode_shape.user(db, 'synthetic-input', SESSION, 10, 'Made-up input')
        opencode_shape.assistant(db, 'synthetic-step', 'synthetic-input', SESSION, 20, finish='tool-calls')
        state = dict(status={'idle': 'completed', 'pending': 'running', 'unknown': 'unmeasured'}[mode],
                     time=dict(start=opencode_shape.MS + 21))
        if mode == 'idle':
            state['time']['end'] = opencode_shape.MS + 30
        opencode_shape.part(db, 'synthetic-call', 'synthetic-step', SESSION, 21,
                            dict(type='tool', tool=TOOL, state=state))
        if mode != 'pending':
            opencode_shape.assistant(db, 'synthetic-answer', 'synthetic-input', SESSION, 40)
    return record


def pi_record(home, mode):
    record = PiShape(home, home, SESSION, MODEL)
    record.begin('Made-up input', MODEL, 'off')
    record.tool_start(TOOL, 'made-up argument')
    if mode != 'pending':
        record.tool_end('Made-up result', False)
        record.end('Made-up answer')
    if mode == 'unknown':
        record.entry('unmeasured-tool-event')
    return record.path


def antigravity_record(home, mode):
    record = AntigravityShape(home, home, SESSION, MODEL)
    record.begin('Made-up input', MODEL, 'high')
    record.tool_start(TOOL, 'made-up argument')
    if mode != 'open-unknown':
        record.tool_end('Made-up result', False)
        record.end('Made-up answer')
    if mode == 'unknown-exit':
        # The reader has no completed boundary for an unmeasured executor reason.
        with record._conn() as db:
            db.execute('UPDATE executor_metadata SET data=?',
                       (_pb_uint(1, 99) + _pb_uint(2, 5) + _pb_uint(3, 3) + _pb_str(9, record.exec_id),))
    return record.path


CASES = [
    ('codex', 'openai', codex_record, 'pending', (TOOL,), 'tools-pending'),
    ('codex', 'openai', codex_record, 'unknown', None, 'tools-unknown'),
    ('codex', 'openai', codex_record, 'idle', (), None),
    ('grok', 'xai', grok_record, 'pending', (TOOL,), 'turn-open'),
    ('grok', 'xai', grok_record, 'unknown', None, 'tools-unknown'),
    ('grok', 'xai', grok_record, 'idle', (), None),
    ('opencode', 'local', opencode_record, 'pending', (TOOL,), 'turn-open'),
    ('opencode', 'local', opencode_record, 'unknown', None, 'tools-unknown'),
    ('opencode', 'local', opencode_record, 'idle', (), None),
    ('pi', 'local', pi_record, 'pending', (TOOL,), 'turn-open'),
    ('pi', 'local', pi_record, 'unknown', None, 'tools-unknown'),
    ('pi', 'local', pi_record, 'idle', (), None),
    ('antigravity', 'google', antigravity_record, 'open-unknown', None, 'turn-open'),
    ('antigravity', 'google', antigravity_record, 'unknown-exit', None, 'turn-unknown'),
    ('antigravity', 'google', antigravity_record, 'idle', (), None),
]


@pytest.mark.parametrize('operation', ['relaunch', 'select'])
@pytest.mark.parametrize('program,provider,build,mode,tools,hold', CASES,
                         ids=[f'{program}-{mode}' for program, _, _, mode, _, _ in CASES])
def test_native_readers_gate_service_move(tmp_path, monkeypatch, program, provider, build, mode, tools, hold, operation):
    names = ('first', 'second')
    endpoint = 'http://127.0.0.1:9/v1' if provider == 'local' else None
    pools = {name: Pool(name, provider, endpoint=endpoint) for name in names}
    spec = SeatSpec('builder', 'crew', 'worker', program, names, model=MODEL,
                    sandbox=False, pool_choices={name: {} for name in names})
    ship = Ship(tmp_path / 'ship', 'synthetic', 'operator', tmp_path, pools,
                {'crew': Session('crew', 'Synthetic')}, {spec.name: spec})
    pane = Pane('%1', 'Synthetic', spec.name, 1201, 100, 40, False, False, None)
    tmux = SimpleNamespace(pane=lambda identifier: pane if identifier == pane.id else None)
    seat = Seat(ship, spec, tmux)
    seat.home.mkdir(parents=True)
    seat.move_to('first')  # the first launch has already saved its active pool
    record = build(seat.home, mode)
    # This fixture has already been joined to the launch. The service uses the ordinary ownership checks.
    when = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    before = Launched(pane.id, pane.pid, when, 'first', None, [seat.harness.program], 'synthetic', program)
    before.write(seat.trusted / trusted_files.LAUNCH)
    (seat.trusted / trusted_files.RECORD).write_text(f'{record}\n')
    assert seat.record() == record
    state = seat.harness.state(record)
    assert state.pending_tools == tools
    if mode == 'open-unknown':
        assert state.open_tool == TOOL
    if hold is None:
        assert state.turn_open is False and state.queued == () and state.completed_turns

    actions = []

    def stop():
        actions.append('stop')
        (seat.trusted / trusted_files.LAUNCH).unlink()
        return 'synthetic transport stopped'

    def start(resume, *, observation=None, target=None):
        assert resume and target.id == SESSION and target.record == record
        actions.append('start')
        replace(before, pane='%2', pid=1202, pool=seat.pool.name, resume=target.id).write(
            seat.trusted / trusted_files.LAUNCH)
        return 'synthetic transport started with resume'

    # These replace only the transport, native program startup and provider calls.
    monkeypatch.setattr(seat, '_relaunch_down', stop)
    monkeypatch.setattr(seat, '_up', start)
    monkeypatch.setattr(seat, '_observe_selection', lambda *_: ('synthetic', None))
    monkeypatch.setattr(credential, 'reseed', lambda *_args, **_kwargs: [])
    service = Service(ship, tmux)
    monkeypatch.setattr(service, 'known_open', lambda *_: True)
    try:
        if operation == 'relaunch':
            proof = service.relaunch(seat, True)
            assert (proof is None) == (hold is not None)
            if hold:
                assert service.request_wait[0] == hold
        else:
            proof = service.select(seat, 'second')
            assert ('not moved' in proof) == (hold is not None)
        reading = json.loads(move_hold.path(seat).read_text())
        assert reading['category'] == hold
        if hold:
            assert not actions and seat.launched() == before and seat.pool.name == 'first'
        else:
            assert actions == ['stop', 'start']
            assert 'started with resume' in proof
            assert seat.launched().resume == SESSION
            assert seat.pool.name == ('first' if operation == 'relaunch' else 'second')
    finally:
        service.ledger.close()
