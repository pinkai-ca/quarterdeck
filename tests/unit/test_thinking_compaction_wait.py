"""A native compaction may justify waiting, never a recovery input or mode key."""

from dataclasses import replace
from datetime import timedelta
import json

import pytest

from quarterdeck import pending, post, recovery
from quarterdeck.door import Door
from quarterdeck.harness.base import Interruption
from quarterdeck.service import Service
from quarterdeck.ship import ShipError
from tests.unit.test_opencode_record import AT, MS, message, part, session
from tests.unit.test_thinking_generation import append_auto_cap, append_compaction
from tests.unit.test_thinking_recovery import (
    database_connection, forbid_human_content_and_input, journal, world,
)


def compacting_recovery(world):
    world.native.auto_complete = False
    recovery.run(world.service)
    proof = journal(world)
    assert proof.data['phase'] == 'delivered'
    assert proof.data['recovery']['recovery_input'] == 'recovery-input'
    assert proof.data['recovery']['error'] is None
    con = database_connection(world.record)
    con.execute("UPDATE message SET data=json_set(data,'$.time.completed',?,'$.finish','tool-calls') "
                "WHERE id='recovery-answer'", (MS + 10020,))
    part(con, 'recovery-tool', 'recovery-answer', 'root', 10019,
         {'type': 'tool', 'tool': 'bash', 'callID': 'recovery-call',
          'state': {'status': 'completed', 'time': {'start': MS + 10018, 'end': MS + 10019}}})
    append_compaction(con)
    con.execute("UPDATE message SET data=json_remove(data,'$.time.completed','$.finish') WHERE id='summary'")
    con.execute("DELETE FROM part WHERE id='summary-text'")
    con.commit()
    con.close()
    world.native.working = True
    found = world.seat.harness.state(world.record)
    assert found.turn_open and found.pending_tools == () and found.queued == ()
    assert found.capped is None and found.work_id is None
    assert found.input_id == 'compact-input'
    return proof


def no_new_action(world, calls):
    assert world.native.calls == calls
    assert len(world.native.inputs) == 1
    assert world.native.selected == 'qd-recovery-off'
    assert len(pending.journals(world.seat)[0]) == 1
    assert world.service.store.is_open(world.order)


def test_active_native_compaction_is_only_a_quiet_held_wait(world):
    compacting_recovery(world)
    calls = list(world.native.calls)
    before = world.seat.harness.state(world.record)
    for _ in range(2):
        recovery.run(world.service)
        proof = journal(world)
        assert proof.data['recovery']['mode'] == 'submitted'
        assert proof.data['recovery']['error'] is None, 'proved active compaction must not become an unknown-work alarm'
        reading = Door(world.seat).reading()
        assert not reading.open and reading.waiting and reading.clears != 'needs a human'
        assert Door(world.seat).deliver('Late work remains held').result == 'withheld'
        assert Door(world.seat).deliver('A rebrief remains held').result == 'withheld'
        assert Service.control(world.service, world.seat, 'effort', 'high').startswith('effort held:')
        assert Service.relaunch(world.service, world.seat, False) is None
        Service.fresh_after_order(world.service)
        for action in (world.seat.up, world.seat.restart, world.seat.fresh,
                       lambda: world.seat.failover_to('synthetic')):
            with pytest.raises(ShipError, match='mode unresolved'):
                action()
        no_new_action(world, calls)
    after = world.seat.harness.state(world.record)
    assert after.work_id is None and after.capped is None and after.turn_open
    assert after.completed_turns == before.completed_turns, 'a wait is not a synthetic turn ending'


def finish_summary(con):
    con.execute("UPDATE message SET data=json_set(data,'$.time.completed',?,'$.finish','stop') WHERE id='summary'",
                (MS + 20020,))
    part(con, 'summary-text', 'summary', 'root', 20015, {'type': 'text', 'text': 'Synthetic summary'})


@pytest.mark.parametrize('marked', [True, False])
def test_only_marked_continuation_and_true_idle_can_restore_after_compaction(world, marked):
    compacting_recovery(world)
    calls = list(world.native.calls)
    recovery.run(world.service)
    assert journal(world).data['recovery']['error'] is None
    no_new_action(world, calls)
    con = database_connection(world.record)
    finish_summary(con)
    append_auto_cap(con, marked=marked)
    con.execute("UPDATE message SET data=json_set(data,'$.finish','stop','$.tokens',json(?)) WHERE id='auto-cap'",
                (json.dumps({'input': 100, 'output': 5, 'reasoning': 0, 'total': 105}),))
    con.execute("DELETE FROM part WHERE id IN ('auto-thinking','auto-end')")
    part(con, 'auto-answer', 'auto-cap', 'root', 20045, {'type': 'text', 'text': 'Synthetic continued final answer'})
    con.commit()
    con.close()
    world.native.working = False
    recovery.run(world.service)
    proof = journal(world)
    if marked:
        assert proof.data['recovery']['mode'] == 'done' and proof.data['recovery']['error'] is None
        assert world.native.calls == calls + [('keys', ('C-t',))]
        assert world.native.selected == 'default'
        recovery.run(world.service)
        assert world.native.calls == calls + [('keys', ('C-t',))]
        assert len(world.native.inputs) == 1 and len(pending.journals(world.seat)[0]) == 1
    else:
        assert proof.data['recovery']['mode'] != 'done' and proof.data['recovery']['error']
        assert Door(world.seat).reading().clears == 'needs a human'
        no_new_action(world, calls)


def test_ended_summary_alone_never_restores_or_becomes_accepted_work(world):
    compacting_recovery(world)
    calls = list(world.native.calls)
    con = database_connection(world.record)
    finish_summary(con)
    con.commit()
    con.close()
    world.native.working = False
    found = world.seat.harness.state(world.record)
    assert found.work_id is None and found.capped is None
    recovery.run(world.service)
    assert journal(world).data['recovery']['error']
    assert Door(world.seat).reading().clears == 'needs a human'
    no_new_action(world, calls)


@pytest.mark.parametrize('fault', [
    'foreign-root', 'different-work', 'superseding', 'wrong-parent', 'unmarked',
    'malformed-marker', 'mixed-marker', 'missing-summary-flag', 'wrong-model', 'wrong-provider',
    'missing-start', 'changed-original', 'changed-recovery-input',
    'later-assistant', 'child', 'background', 'running-tool', 'unknown-part',
    'replaced-launch', 'changed-config', 'selected-original',
])
def test_unproved_compaction_never_suppresses_the_mode_alarm(world, fault):
    compacting_recovery(world)
    calls = list(world.native.calls)
    con = database_connection(world.record)
    if fault == 'foreign-root':
        con.execute("UPDATE session SET id='foreign' WHERE id='root'")
        con.execute("UPDATE message SET session_id='foreign'")
        con.execute("UPDATE part SET session_id='foreign'")
    elif fault in ('different-work', 'superseding'):
        at = 15000 if fault == 'different-work' else 21000
        message(con, 'other-user', 'root', at, {'role': 'user', 'agent': 'build',
            'time': {'created': MS + at}, 'model': {'modelID': 'qwen3.8-27b', 'providerID': 'synthetic'}})
        part(con, 'other-text', 'other-user', 'root', at, {'type': 'text', 'text': 'Unrelated accepted work'})
    elif fault == 'wrong-parent':
        con.execute("UPDATE message SET data=json_set(data,'$.parentID','input') WHERE id='summary'")
    elif fault == 'unmarked':
        con.execute("UPDATE part SET data=json_set(data,'$.auto',json('false')) WHERE id='compact-part'")
    elif fault == 'malformed-marker':
        con.execute("UPDATE part SET data=json_set(data,'$.auto','true') WHERE id='compact-part'")
    elif fault == 'mixed-marker':
        part(con, 'mixed', 'compact-input', 'root', 20001, {'type': 'text', 'text': 'Unproved extra input'})
    elif fault == 'missing-summary-flag':
        con.execute("UPDATE message SET data=json_remove(data,'$.summary') WHERE id='summary'")
    elif fault == 'wrong-model':
        con.execute("UPDATE message SET data=json_set(data,'$.modelID','unrelated-model') WHERE id='summary'")
    elif fault == 'wrong-provider':
        con.execute("UPDATE message SET data=json_set(data,'$.providerID','unrelated-provider') WHERE id='summary'")
    elif fault == 'missing-start':
        con.execute("UPDATE message SET data=json_remove(data,'$.time.created') WHERE id='summary'")
    elif fault == 'changed-original':
        con.execute("UPDATE part SET data=json_set(data,'$.text','Changed original evidence') WHERE id='thinking'")
    elif fault == 'changed-recovery-input':
        con.execute("UPDATE part SET data=json_set(data,'$.text','Different accepted input') WHERE id='recovery-input-text'")
    elif fault == 'later-assistant':
        message(con, 'later', 'root', 21000, {'role': 'assistant', 'parentID': 'recovery-input',
            'agent': 'build', 'modelID': 'qwen3.8-27b', 'providerID': 'synthetic',
            'time': {'created': MS + 21000}})
    elif fault == 'child':
        session(con, 'child', 21000, 'root')
        message(con, 'child-user', 'child', 21001, {'role': 'user', 'time': {'created': MS + 21001}})
        part(con, 'child-text', 'child-user', 'child', 21001, {'type': 'text', 'text': 'Synthetic child work'})
        message(con, 'child-answer', 'child', 21002, {'role': 'assistant', 'parentID': 'child-user',
            'time': {'created': MS + 21002}})
    elif fault == 'background':
        part(con, 'background', 'recovery-answer', 'root', 10019, {'type': 'tool', 'tool': 'task',
            'state': {'status': 'completed', 'input': {'background': True}}})
    elif fault == 'running-tool':
        part(con, 'running', 'recovery-answer', 'root', 10019,
             {'type': 'tool', 'tool': 'bash', 'state': {'status': 'running'}})
    elif fault == 'unknown-part':
        part(con, 'unknown', 'summary', 'root', 20012, {'type': 'future_tool_call', 'name': 'bash'})
    con.commit()
    con.close()
    if fault == 'replaced-launch':
        launch = world.seat.launched()
        replace(launch, at=(AT + timedelta(seconds=2)).isoformat()).write(world.seat.folder / 'launch.json')
    elif fault == 'changed-config':
        path = world.seat.home / 'config/opencode/opencode.json'
        path.write_text(path.read_text() + '\n')
    elif fault == 'selected-original':
        world.native.selected = 'default'
        (world.seat.home / 'state/opencode/model.json').write_text(
            json.dumps({'variant': {'synthetic/qwen3.8-27b': 'default'}}))
    recovery.run(world.service)
    assert journal(world).data['recovery']['mode'] != 'done'
    assert journal(world).data['recovery']['error']
    assert Door(world.seat).reading().clears == 'needs a human'
    assert world.native.calls == calls and len(world.native.inputs) == 1
    assert len(pending.journals(world.seat)[0]) == 1


@pytest.mark.parametrize('obstruction', ['draft', 'dialog', 'wall', 'unknown', 'queue'])
def test_compaction_wait_does_not_hide_current_screen_obstructions(world, monkeypatch, obstruction):
    compacting_recovery(world)
    calls = list(world.native.calls)
    screen = Door(world.seat).screen()
    if obstruction == 'draft':
        screen = replace(screen, box='A human draft')
    elif obstruction == 'dialog':
        screen = replace(screen, interruption=Interruption('Synthetic login', 'LOGIN'))
    elif obstruction == 'wall':
        monkeypatch.setattr(Door, 'wall', lambda *args: 'synthetic quota wall')
    elif obstruction == 'unknown':
        screen = replace(screen, unknown='Synthetic unrecognized frame')
    else:
        screen = replace(screen, queued=1)
    monkeypatch.setattr(Door, 'screen', lambda *args: screen)
    recovery.run(world.service)
    assert journal(world).data['recovery']['error']
    assert Door(world.seat).reading().clears == 'needs a human'
    no_new_action(world, calls)


@pytest.mark.parametrize('obstruction', ['draft', 'dialog', 'dismissable-dialog', 'wall'])
def test_a_new_obstruction_is_immediate_without_another_recovery_duty(world, monkeypatch, obstruction):
    compacting_recovery(world)
    calls = list(world.native.calls)
    assert journal(world).data['recovery']['error'] is None
    screen = Door(world.seat).screen()
    if obstruction == 'draft':
        screen = replace(screen, box='New human draft')
    elif obstruction in ('dialog', 'dismissable-dialog'):
        screen = replace(screen, interruption=Interruption('Synthetic login', 'LOGIN',
                                                          'Escape' if obstruction == 'dismissable-dialog' else None))
    else:
        monkeypatch.setattr(Door, 'wall', lambda *args: 'synthetic quota wall')
    reading = Door(world.seat).reading(screen)
    assert not reading.open and not reading.waiting
    assert reading.reason == {'draft': 'text in the box', 'dialog': 'Synthetic login',
                              'dismissable-dialog': 'Synthetic login',
                              'wall': 'synthetic quota wall'}[obstruction]
    if obstruction != 'wall':
        assert reading.clears == 'needs a human'
    monkeypatch.setattr(Door, 'screen', lambda *args: screen)
    assert Door(world.seat).deliver('Still no new input or dialog key').result == 'withheld'
    no_new_action(world, calls)


@pytest.mark.parametrize('mode', ['submitted', 'held'])
def test_active_compaction_cannot_erase_a_published_error_or_hold(world, mode):
    proof = compacting_recovery(world)
    error = 'Synthetic already published uncertainty; must remain visible'
    proof.save(recovery={**proof.data['recovery'], 'mode': mode, 'error': error})
    world.service.store.new('report', 'ship', 'captain', error)
    before, calls = proof.path.read_bytes(), list(world.native.calls)
    recovery.run(world.service)
    assert proof.path.read_bytes() == before
    assert Door(world.seat).reading().clears == 'needs a human'
    no_new_action(world, calls)


@pytest.mark.parametrize('phase,mode', [('prepared', 'disabling'), ('submitting', 'submitted')])
def test_compaction_never_reinterprets_uncertain_control_or_submission(world, phase, mode):
    proof = compacting_recovery(world)
    proof.save(phase=phase, recovery={**proof.data['recovery'], 'mode': mode,
                                    'error': 'Synthetic uncertain control or submit'})
    calls = list(world.native.calls)
    recovery.run(world.service)
    assert journal(world).data['recovery']['mode'] != 'done'
    assert Door(world.seat).reading().clears == 'needs a human'
    no_new_action(world, calls)


def test_human_ownership_during_compaction_keeps_visible_metadata_hold_without_reads(world, monkeypatch):
    proof = compacting_recovery(world)
    world.seat.ship.sessions['crew'] = replace(world.seat.session, human=True)
    before, calls = proof.path.read_bytes(), list(world.native.calls)
    forbid_human_content_and_input(world, monkeypatch)
    recovery.run(world.service)
    reading = Door(world.seat).reading()
    assert not reading.open and not reading.waiting and reading.clears == 'needs a human'
    assert Service.relaunch(world.service, world.seat, False) is None
    assert proof.path.read_bytes() == before
    no_new_action(world, calls)
