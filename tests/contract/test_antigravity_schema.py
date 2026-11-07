"""Synthetic wire examples checked against installed 1.5.2 descriptors; see docs/ANTIGRAVITY.md."""
import sqlite3
from datetime import datetime, timezone

import pytest

from quarterdeck.fakerecord import AntigravityShape, _pb_bytes, _pb_str, _pb_uint
from quarterdeck.harness import antigravity as agy
from quarterdeck.harness.base import Tokens


@pytest.mark.parametrize('status,open_', [(0,None),(1,True),(2,True),(3,True),(4,True),(5,True),
                                         (6,False),(7,True),(8,True),(9,True),(10,None),(11,True),
                                         (12,False),(99,None)])
def test_persisted_step_status_enum_not_transcript_label_guess(tmp_path, status, open_):
    # These literal values are the exa.cortex_pb.CortexStepStatus descriptor mapping,
    # independent of the sets used by the decoder. DONE alone is not a turn terminator.
    rec = AntigravityShape(tmp_path / 'home', tmp_path, 'synthetic', 'model')
    rec.begin('question', 'model', 'high')
    with sqlite3.connect(rec.path) as con:
        con.execute('UPDATE steps SET status=?', (status,))
    assert agy.state(rec.path).turn_open is open_
    assert len(agy.turns(rec.path)) == (1 if status in (6,12) else 0)


@pytest.mark.parametrize('reason', [0,5,99])
def test_unknown_executor_reason_cannot_prove_idle_or_completion(tmp_path, reason):
    rec = AntigravityShape(tmp_path / 'home', tmp_path, 'synthetic', 'model')
    rec.begin('question', 'model', 'high')
    rec.end('answer')
    with sqlite3.connect(rec.path) as con:
        con.execute('UPDATE executor_metadata SET data=?',
                    (_pb_uint(1,reason)+_pb_uint(2,5)+_pb_uint(3,1)+_pb_str(9,rec.exec_id),))
    assert agy.state(rec.path).turn_open is None
    assert agy.turns(rec.path) == []


@pytest.mark.parametrize('suffix,body,failed', [
    ('exa.cortex.core.tools.RunCommand','08001000',False),
    ('exa.cortex.core.tools.RunCommand','0801',True),
    ('exa.cortex.core.tools.RunCommand','1001',True),
    ('exa.cortex.core.tools.RunCommand','4001',None),
    ('exa.cortex_pb.CortexStepRunCommand','0a026c7312012e3000',False),
    ('exa.cortex_pb.CortexStepRunCommand','3002',True),
    ('exa.cortex_pb.CortexStepRunCommand','a80201',True),
    ('exa.cortex_pb.CortexStepRunCommand','0a026c73',None),
    ('unsupported.RunCommand','0801',None),
])
def test_tool_outcomes_dispatch_on_full_verified_type_url(tmp_path, suffix, body, failed):
    rec = AntigravityShape(tmp_path / 'home', tmp_path, 'synthetic', 'model')
    rec.begin('question', 'model', 'high')
    rec.tool('run_command','echo x','x',False)
    rec.end('answer')
    # Literal protobufs above include valid string command/cwd for CortexStepRunCommand.
    # No error status or human-readable result can mask a bad Any decoder in these cases.
    payload = _pb_bytes(140, _pb_bytes(2, _pb_bytes(6,
        _pb_str(1, 'type.googleapis.com/'+suffix) + _pb_bytes(2, bytes.fromhex(body)))))
    with sqlite3.connect(rec.path) as con:
        con.execute('UPDATE steps SET status=3,step_payload=? WHERE step_type=132', (payload,))
    assert agy.turns(rec.path)[0].tools[0].failed is failed


def test_usage_enum_wire_shape_zero_presence_and_no_generator_double_count(tmp_path):
    rec = AntigravityShape(tmp_path / 'home', tmp_path, 'synthetic', 'model')
    rec.begin('question', 'model', 'high')
    rec.tool('run_command','echo x','x',False)
    rec.end('answer')
    # Model enum=0, input=120 (already uncached), output=30, cache read=560.
    usage = bytes.fromhex('08001078181e28b004')
    with sqlite3.connect(rec.path) as con:
        # Generation 0 lists both the planner AND its tool. Its usage is a fallback,
        # charged once even though it is associated with both steps.
        m = _pb_str(12, rec.exec_id)
        con.execute('UPDATE steps SET metadata=? WHERE idx=1', (m,))
        gm = _pb_bytes(1,_pb_bytes(4,usage)+_pb_str(19,'actual-model')) + _pb_bytes(2,b'\x01\x02') + _pb_str(4,rec.exec_id)
        con.execute('UPDATE gen_metadata SET data=? WHERE idx=0', (gm,))
        # Final response explicitly reports a zero-valued usage submessage.
        con.execute('UPDATE steps SET metadata=? WHERE idx=3', (m+_pb_bytes(9,b''),))
    turn = agy.turns(rec.path)[0]
    assert turn.tokens == Tokens(560,120,None,30)
    assert agy._parse_usage(None) == Tokens.unknown()
    assert agy._parse_usage(b'') == Tokens(0,0,None,0)


def test_nonplanner_usage_and_missing_final_time_are_not_lost(tmp_path):
    rec = AntigravityShape(tmp_path / 'home', tmp_path, 'synthetic', 'model')
    rec.begin('question', 'model', 'high')
    rec.end('answer')
    before = agy.turns(rec.path)[0]
    # A checkpoint step may carry usage too. It is not a planner response or a tool.
    with sqlite3.connect(rec.path) as con:
        meta = _pb_str(12,rec.exec_id) + _pb_bytes(9,bytes.fromhex('10141805'))
        con.execute('INSERT INTO steps VALUES (2,23,3,0,?,?,?)', (meta,b'',b''))
        con.execute('UPDATE executor_metadata SET data=?',
                    (_pb_uint(1,4)+_pb_uint(2,2)+_pb_uint(3,2)+_pb_str(9,rec.exec_id),))
    after = agy.turns(rec.path)[0]
    assert after.tokens.read_uncached == before.tokens.read_uncached + 20
    assert after.tokens.written == before.tokens.written + 5
    assert after.ended is None and after.seconds is None, 'an earlier completed step cannot timestamp the last step'


def test_error_message_after_executor_boundary_does_not_invent_an_open_turn(tmp_path):
    rec = AntigravityShape(tmp_path/'home',tmp_path,'synthetic','model')
    rec.begin('question','model','high')
    rec.end('partial reply')
    with sqlite3.connect(rec.path) as con:
        con.execute('UPDATE executor_metadata SET data=?',
                    (_pb_uint(1,1)+_pb_uint(2,1)+_pb_uint(3,1)+_pb_str(9,rec.exec_id),))
        # ERROR_MESSAGE=17 is the descriptor enum, distinct from step status ERROR=7.
        # This trailer belongs to no execution and has no completion timestamp.
        con.execute('INSERT INTO steps VALUES (2,17,3,0,?,?,?)',(b'',b'',b''))
    state, turns = agy.read(rec.path)
    assert state.turn_open is False and state.turns == 1
    assert len(turns) == 1 and turns[0].ended is None and turns[0].seconds is None


def test_new_input_reports_its_model_before_generation_and_confirms_exact_text(tmp_path):
    from types import SimpleNamespace
    from quarterdeck.door import Door
    from quarterdeck.fakerecord import _pb_ts

    rec = AntigravityShape(tmp_path/'home',tmp_path,'synthetic','older-model')
    rec.begin('old question','older-model','high')
    rec.end('answer')
    rec.begin('new question\nwith a second line','new-model','high')
    config = _pb_bytes(1,_pb_str(28,'new-model'))
    with sqlite3.connect(rec.path) as con:
        con.execute('UPDATE steps SET step_payload=? WHERE idx=2',
                    (_pb_bytes(19,_pb_str(2,'new question\nwith a second line')+_pb_bytes(12,config)),))
    state = agy.state(rec.path)
    assert state.model == 'new-model' and state.turn_open is True
    assert state.last_input == 'new question\nwith a second line'
    harness = agy.AntigravityHarness()
    seat = SimpleNamespace(harness=harness,record=lambda:rec.path,tmux=None)
    assert Door(seat).confirm('new question\nwith a second line',seconds=0).result == 'delivered'
