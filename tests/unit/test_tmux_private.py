"""Private transport does not alter legacy addressing or manufacture creator evidence."""
from types import SimpleNamespace

import pytest

from quarterdeck import tmux as module
from quarterdeck.tmux import Tmux, TmuxError


@pytest.mark.parametrize('name,address',[(None,[]),('synthetic-name',['-L','synthetic-name'])])
def test_default_and_named_transport_keep_their_native_arguments(monkeypatch,name,address):
    seen=[]
    def run(command,**kwargs):
        seen.append(command)
        return SimpleNamespace(returncode=0,stdout='synthetic\n')
    monkeypatch.setattr(module.subprocess,'run',run)
    assert Tmux(name).run('list-sessions')=='synthetic\n'
    assert seen==[['tmux','-u',*address,'list-sessions']]


def test_an_absolute_observer_never_creates_or_repairs_a_namespace(tmp_path):
    observer=Tmux(str(tmp_path/'s'))
    with pytest.raises(TmuxError):observer.new_session('MustNotStart')
    assert not list(tmp_path.iterdir())
    assert not observer.close() and 'authority' in observer.held
