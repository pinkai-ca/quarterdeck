"""Unjailed panes keep the display's cookie path, XAUTHORITY, which a jail never has; and SUDO_ASKPASS reaches no
seat, no server and no service, whatever list is given, a list that names it included: dropped, never refused.

A seat must never be able to raise a sudo prompt on the operator's screen: root beyond the security door goes only
through pkexec. Names only: no value is printed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quarterdeck import jail_environment as je
from quarterdeck import sandbox, seat as seatmod

ASKPASS = 'SUDO_ASKPASS'
DESKTOP = {'DISPLAY': ':0', 'XAUTHORITY': '/run/user/1000/.mutter-Xwaylandauth.ABC', 'WAYLAND_DISPLAY': 'wayland-0',
           'SSH_AUTH_SOCK': '/run/user/1000/keyring/ssh'}
CALLER = {**DESKTOP, 'PATH': '/usr/bin', 'HOME': '/home/op', ASKPASS: '/usr/bin/ssh-askpass', 'LANG': 'C.UTF-8'}
LISTS = {'ALLOWED': (je.ALLOWED, ()), 'UNJAILED': (je.UNJAILED, je.UNJAILED_PREFIXES),
         'SERVICE': (je.SERVICE, je.SERVICE_PREFIXES), 'FROM_PANE': (je.FROM_PANE, je.FROM_PANE_PREFIXES)}


def jail(tmp_path: Path, environment=()) -> sandbox.Jail:
    home, ship = tmp_path / 'home', tmp_path / 'ship'
    return sandbox.Jail(home=home, seat_home=ship / 'runtime/seats/w/home', seat_folder=ship / 'runtime/seats/w',
                        workspace=tmp_path / 'work', ship_folder=ship, runtime=ship / 'runtime',
                        environment=tuple(environment))


def set_names(argv: list[str]) -> list[str]:
    return [argv[at + 1] for at, word in enumerate(argv) if word == '--setenv']


# -- XAUTHORITY: kept by unjailed panes, never by a jail -----------------------------------------------------------


def test_the_desktop_list_keeps_xauthority_for_unjailed_panes_and_the_service():
    assert 'XAUTHORITY' in je.DESKTOP and not je.looks_secret('XAUTHORITY')
    assert set(DESKTOP) <= set(je.unjailed_environment(CALLER)), 'an unjailed pane reaches the desktop through XWayland'
    assert set(DESKTOP) <= set(je.service_environment(CALLER)), 'the service may start the server the panes inherit'


def test_a_jail_never_has_xauthority(tmp_path, monkeypatch):
    for name, value in CALLER.items():
        monkeypatch.setenv(name, value)
    argv = sandbox.command(jail(tmp_path), ['pi'])
    assert je.CLEAR in argv and 'XAUTHORITY' not in set_names(argv), 'only the allowlist is set after --clearenv'
    assert 'XAUTHORITY' not in set_names(je.pane_settings(CALLER)), 'the launcher copies no desktop name in'
    assert 'XAUTHORITY' in sandbox.SESSION_VARIABLES, 'and its own session check names it among the desktop ways'
    assert 'XAUTHORITY' not in je.ALLOWED and not je.from_pane('XAUTHORITY')


# -- SUDO_ASKPASS: dropped from every list, whatever names it --------------------------------------------------------


def test_askpass_is_refused_in_one_place_and_looks_secret_or_not_it_never_passes(monkeypatch):
    assert je.refused(ASKPASS) and ASKPASS in je.REFUSED
    assert not je.refused('XAUTHORITY') and not je.refused('DISPLAY')
    assert je.without_refused(CALLER) == {name: value for name, value in CALLER.items() if name != ASKPASS}
    # It is named on its own, not left to the secret pattern (which its PASS also meets today): a pattern that let it
    # through would still not let it in.
    monkeypatch.setattr(je, 'looks_secret', lambda name: False)
    for names, prefixes in LISTS.values():
        assert ASKPASS not in je.declared(CALLER, (*names, ASKPASS), prefixes, keys=(ASKPASS,))
    assert ASKPASS not in je.unjailed_environment(CALLER) and ASKPASS not in je.service_environment(CALLER)
    assert not je.from_pane(ASKPASS)


@pytest.mark.parametrize('which', sorted(LISTS))
def test_no_list_passes_askpass_even_one_that_names_it(which):
    names, prefixes = LISTS[which]
    assert ASKPASS not in je.declared(CALLER, names, prefixes)
    assert ASKPASS not in je.declared(CALLER, (*names, ASKPASS), prefixes), f'{which} with it named still drops it'
    assert ASKPASS not in je.declared(CALLER, names, (*prefixes, 'SUDO_')), 'nor by a prefix'
    assert ASKPASS not in je.declared(CALLER, names, prefixes, keys=(ASKPASS,)), 'nor as a pool key the ship declares'


def test_the_server_and_the_service_never_hold_askpass():
    assert ASKPASS not in je.unjailed_environment(CALLER)
    assert ASKPASS not in je.service_environment(CALLER)
    assert not je.from_pane(ASKPASS) and ASKPASS not in set_names(je.pane_settings(CALLER))


def test_a_jail_drops_askpass_however_it_is_declared_and_still_launches(tmp_path):
    argv = sandbox.command(jail(tmp_path, environment=[(ASKPASS, '/x'), ('GIT_AUTHOR_NAME', 'w')]), ['pi'],
                           {ASKPASS: '/usr/bin/ssh-askpass', 'PI_SKIP_VERSION_CHECK': '1'})
    names = set(set_names(argv))
    assert ASKPASS not in names and ASKPASS not in argv
    assert {'PI_SKIP_VERSION_CHECK', 'GIT_AUTHOR_NAME'} <= names, 'what else is declared is still set'


def test_an_unjailed_launch_unsets_askpass_whatever_the_server_or_session_holds(tmp_path):
    words = seatmod.launch_words('/bin', ['prog'], socket=None, tmux_tmpdir=tmp_path, markers=['CLAUDECODE'])
    at = words.index('prog')
    assert ['-u', ASKPASS] == words[words.index(ASKPASS) - 1:words.index(ASKPASS) + 1] and words.index(ASKPASS) < at
    again = seatmod.launch_words('/bin', ['prog'], socket=None, tmux_tmpdir=tmp_path, markers=[ASKPASS])
    assert again.count(ASKPASS) == 1, 'named once, even when a marker names it too'


def test_an_unjailed_seats_declared_askpass_is_dropped_not_refused():
    seatmod.refuse_secret_names('w', {ASKPASS: '/usr/bin/ssh-askpass'}, None)  # no refusal: the launch goes on
    with pytest.raises(Exception, match='OPENAI_API_KEY'):  # a secret-looking name beside it still refuses
        seatmod.refuse_secret_names('w', {ASKPASS: '/x', 'OPENAI_API_KEY': 'k'}, None)


def test_the_launcher_never_carries_askpass_into_a_jail(monkeypatch):
    seen = {}
    monkeypatch.setattr(je.os, 'execve', lambda program, argv, env: seen.update(argv=argv, env=env))
    monkeypatch.setenv(ASKPASS, '/usr/bin/ssh-askpass')
    je.main(['--', 'bwrap', je.CLEAR, 'true'])
    assert ASKPASS not in seen['argv'] and set(seen['env']) == {'PATH'}
