"""A reserved submit is not a submission when the Door refuses its fresh frame."""
from types import SimpleNamespace

import pytest
from quarterdeck import acts, pending
from quarterdeck.door import Door
from quarterdeck.harness.types import Screen


@pytest.mark.parametrize('reason', ['activity unknown; no key sent', None])
def test_box_journal_records_the_actual_submit_result(reason):
    events = []
    data = dict(phase='submitting')
    def save(**fields):
        data.update(fields)
        events.append(dict(fields))
    proof = SimpleNamespace(data=data, save=save)
    door = SimpleNamespace(submit=lambda _: reason)
    result = pending._submit_from_box(door, proof, SimpleNamespace(id='%synthetic'),
                                     Screen('', box='payload', working=False), at='synthetic time', saw='exact box')
    assert result == reason
    assert data['look']['outcome'] == ('not submitted' if reason else 'submitted')
    assert events[0]['look']['outcome'] == 'submitting'
    assert (events[-1]['look'].get('reason') == reason)
    assert door.proof is proof


def test_explicit_submit_does_not_confirm_or_close_when_the_door_declines(monkeypatch):
    proof = SimpleNamespace(data=dict(phase='submitting', attempts=[], payload='payload'))
    proof.save = lambda **fields: proof.data.update(fields)
    pane = SimpleNamespace(id='%synthetic')
    screen = Screen('', box='payload', working=False)
    seat = SimpleNamespace(name='synthetic', session=SimpleNamespace(human=False))
    door = SimpleNamespace(screen=lambda: screen, submit=lambda _: 'unknown; no key sent',
                           confirm=lambda _: pytest.fail('no submit means no confirmation'))
    seat.pane = lambda: pane
    monkeypatch.setattr('quarterdeck.door.Door', lambda _: door)
    monkeypatch.setattr(pending, '_submit_candidate', lambda *args: (proof, None))
    monkeypatch.setattr(pending, '_message_of', lambda *args: None)
    service = SimpleNamespace(attempts=SimpleNamespace(all=lambda: []))
    result = pending._submit_claimed(service, seat)
    assert 'not submitted' in result
    assert proof.data['look']['outcome'] == 'not submitted'


def test_operator_type_records_declined_submit_without_confirming(monkeypatch):
    pane = SimpleNamespace(id='%synthetic')
    screen = Screen('', box='', working=False)
    launched = SimpleNamespace(pane='%synthetic', pid=123, at='synthetic time')
    seat = SimpleNamespace(name='synthetic', launched=lambda: launched)
    shown = Screen('', box='payload', working=False)
    door = SimpleNamespace(harness=SimpleNamespace(box=lambda line: line),
        tmux=SimpleNamespace(paste=lambda *args: None),
        box_shows=lambda *args: shown, matches=lambda *args: True,
        submit=lambda _: 'unknown; no key sent',
        confirm=lambda *args, **kwargs: pytest.fail('must not confirm an unsent key'))
    monkeypatch.setattr('quarterdeck.door.Door', lambda _: door)
    monkeypatch.setattr(acts, 'authority', lambda *args: None)
    monkeypatch.setattr(acts, '_frame', lambda *args: (pane, screen))
    monkeypatch.setattr(acts, 'guard', lambda *args, **kwargs: None)
    monkeypatch.setattr(acts, '_settled', lambda *args: (pane, screen))
    monkeypatch.setattr(acts, 'write', lambda *args: None)
    monkeypatch.setattr(acts, '_finish', lambda seat, act, outcome, detail: (outcome, detail))
    outcome, detail = acts.type_line(SimpleNamespace(ship=None), seat, 'payload', None)
    assert outcome == 'not submitted'
    assert 'no key sent' in detail
