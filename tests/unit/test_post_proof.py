"""Post proof routes use the caller's visible launch, never a hidden recipient's absence."""

from types import SimpleNamespace

import pytest

from quarterdeck import cli, sandbox
from quarterdeck.ship import ShipError
from quarterdeck.tmux import TmuxError


def declaration(tmp_path, *, human=False):
    spec = SimpleNamespace(session='crew', sandbox=False)
    return SimpleNamespace(runtime=tmp_path, seats={'sender': spec, 'recipient': spec},
                           seat=lambda name: spec, session_of=lambda spec: SimpleNamespace(human=human))


@pytest.mark.parametrize('argv,expected', [
    (['bwrap','--','program'], True), (['/usr/bin/bwrap','--','program'], True),
    (['program','--option'], False), (('program',''), False),
    (None,None), ([],None), ('bwrap',None), ([None],None), ([''],None),
    (['bwrap',7],None),
])
def test_saved_launch_view_is_known_or_explicitly_unknown(argv, expected):
    assert sandbox.seats_hidden(argv) is expected


@pytest.mark.parametrize('launch', [None, ['bwrap','--','program'], ['/usr/bin/bwrap','--','program'], [], 'malformed'])
@pytest.mark.parametrize('human', [False, True])
def test_restricted_or_unproved_sender_never_reads_recipient_runtime(tmp_path, monkeypatch, launch, human):
    ship = declaration(tmp_path, human=human)
    monkeypatch.setenv('QD_SEAT','sender')
    def seat_of(ship,name):
        assert name=='sender', 'hidden recipient must not be opened'
        return SimpleNamespace(launched=lambda: None if launch is None else SimpleNamespace(argv=launch))
    monkeypatch.setattr(cli,'seat_of',seat_of)
    monkeypatch.setattr(cli.service,'alive',lambda *a:pytest.fail('host service pid is not proof inside a jail'))
    monkeypatch.setattr(cli.post,'watch_armed',lambda *a:pytest.fail('hidden watch is unknown, not unarmed'))
    result = cli.delivery_note(ship,'recipient')
    assert ('watch state is not visible here' if human else 'recipient state is not visible here') in result
    assert 'is down' not in result and 'no watch armed' not in result and 'service down' not in result


def test_unreadable_caller_launch_does_not_make_the_recipient_down(tmp_path, monkeypatch):
    monkeypatch.setenv('QD_SEAT','sender')
    def unreadable(*args): raise ShipError('synthetic unreadable selection')
    monkeypatch.setattr(cli,'seat_of',unreadable)
    assert 'recipient state is not visible here' in cli.delivery_note(declaration(tmp_path),'recipient')


@pytest.mark.parametrize('caller', [False, True])
@pytest.mark.parametrize('up,alive,expected', [
    (False,False,'recipient is down; carried as its first message at qd up'),
    (True,False,'service down; qd up restarts it, then it is typed'),
    (True,True,"the service types it into recipient's pane"),
])
def test_known_unrestricted_down_and_service_routes_remain_unchanged(tmp_path,monkeypatch,caller,up,alive,expected):
    if caller: monkeypatch.setenv('QD_SEAT','sender')
    else: monkeypatch.delenv('QD_SEAT',raising=False)
    def seat_of(ship,name):
        if name=='sender': return SimpleNamespace(launched=lambda:SimpleNamespace(argv=['program']))
        return SimpleNamespace(session=SimpleNamespace(human=False),is_up=up)
    monkeypatch.setattr(cli,'seat_of',seat_of)
    monkeypatch.setattr(cli.service,'alive',lambda *a:alive)
    assert cli.delivery_note(declaration(tmp_path),'recipient')==expected


@pytest.mark.parametrize('armed', [False, True])
def test_visible_human_watch_is_reported_only_when_read(tmp_path,monkeypatch,armed):
    monkeypatch.delenv('QD_SEAT',raising=False)
    monkeypatch.setattr(cli,'seat_of',lambda *a:SimpleNamespace(session=SimpleNamespace(human=True)))
    monkeypatch.setattr(cli.post,'watch_armed',lambda *a:armed)
    expected='watch armed, prints it' if armed else 'no watch armed on recipient; waits in the inbox'
    assert cli.delivery_note(declaration(tmp_path,human=True),'recipient')==expected


@pytest.mark.parametrize('error,expected', [(ShipError('synthetic unknown selection'),'recipient state is unknown here'),
                                           (PermissionError('synthetic hidden file'),'recipient state is unknown here'),
                                           (TmuxError('synthetic inaccessible transport'),'tmux is out of reach from here')])
def test_unavailable_recipient_reading_is_not_a_down_fact(tmp_path,monkeypatch,error,expected):
    monkeypatch.delenv('QD_SEAT',raising=False)
    def unreadable(*args): raise error
    monkeypatch.setattr(cli,'seat_of',unreadable)
    assert expected in cli.delivery_note(declaration(tmp_path),'recipient')
