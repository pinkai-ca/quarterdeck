"""servicectl.scoped: the service starts in a systemd scope of its own, named for the ship, or says why not."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

from quarterdeck import jail_environment as je, servicectl


def stand_in(folder: Path, code: int) -> Path:
    folder.mkdir(exist_ok=True)
    program = folder / 'systemd-run'
    program.write_text(f'#!/bin/sh\necho "$@" >> {folder}/calls\necho refused >&2\nexit {code}\n')
    program.chmod(0o755)
    return program


def test_the_scope_is_named_for_the_ship_and_expands_nothing(tmp_path):
    program = stand_in(tmp_path / 'bin', 0)
    words, why = servicectl.scoped(SimpleNamespace(name='enter prise'), {'PATH': str(program.parent)})
    assert why is None and words[0] == str(program) and words[-1] == '--'
    assert {'--user', '--scope', '--collect', '--expand-environment=no'} <= set(words)
    unit = servicectl.scope_unit(words)
    assert unit.startswith('qd-service-enter_prise-') and unit.endswith('.scope')
    assert 'qd-probe-enter_prise-' in (tmp_path / 'bin' / 'calls').read_text(), 'probed before it is used'


def test_without_systemd_run_or_its_manager_the_service_starts_as_today_and_says_why(tmp_path):
    assert servicectl.scoped(SimpleNamespace(name='x'), {'PATH': str(tmp_path)}) == ([], 'systemd-run is absent')
    program = stand_in(tmp_path / 'bin', 1)
    words, why = servicectl.scoped(SimpleNamespace(name='x'), {'PATH': str(program.parent)})
    assert words == [] and "cannot reach the user's service manager (refused)" in why


def test_the_declared_environment_keeps_its_list_and_the_pools_keys_and_never_a_seat():
    environ = {'PATH': '/bin', 'HOME': '/h', 'QD_SEAT': 'una', 'QD_TMUX_SOCKET': '/s', 'GIT_CONFIG_GLOBAL': '/dev/null',
               'LC_ALL': 'C', 'OPENAI_API_KEY': 'x', 'DISPLAY': ':0', 'POOL_API_KEY': 'k', 'QD_DECOY_TOKEN': 'x',
               'GIT_ASKPASS': 'x', 'XDG_RUNTIME_DIR': '/run/user/1', 'GIT_DIR': '/r/.git', 'GIT_WORK_TREE': '/r',
               'GIT_INDEX_FILE': '/i', 'GIT_OBJECT_DIRECTORY': '/o', 'GIT_SSH_COMMAND': 'ssh -i k', 'GIT_SSH': 's',
               'QD_SHIP': '/s', 'QD_DEFAULT_SHIP': '/d', 'QD_SESSION_OWNER': 't', 'GIT_AUTHOR_NAME': 'a',
               'PYTHONPATH': '/p'}
    kept = je.service_environment(environ)
    # Named exactly: no other QD_ or GIT_ name, and QD_TMUX_SOCKET only where servicectl finds it declared. No pool
    # key either: no seat runs on a key (ship.key_refusal), so the service holds none.
    assert set(kept) == {'PATH', 'HOME', 'LC_ALL', 'XDG_RUNTIME_DIR', 'DISPLAY', 'QD_SHIP',
                         'QD_DEFAULT_SHIP', 'GIT_AUTHOR_NAME'}  # DISPLAY: the desktop's, for the server it may start


def test_the_service_names_no_pool_key_whatever_the_ship_file_declares():
    import inspect
    assert list(inspect.signature(je.service_environment).parameters) == ['environ'], 'no way to pass one in'
    assert not hasattr(servicectl, 'pool_keys')


def test_an_unjailed_seat_refuses_a_secret_name_but_the_one_its_pools_declared_key_fills(monkeypatch):
    import pytest
    from quarterdeck.seat import refuse_secret_names
    from quarterdeck.ship import Pool, ShipError
    from quarterdeck.harness.home import key_environment

    caller = {'PATH': '/bin', 'HOME': '/h', 'POOL_KEY_NAME': 'k-123', 'UNDECLARED_API_KEY': 'u-456', 'QD_SEAT': 'una'}
    keyed = Pool('keyed', 'anthropic', api_key='env:POOL_KEY_NAME')
    plain = Pool('plain', 'anthropic')
    # The service starts from its declared environment: no key, declared or not, and no seat.
    service = je.service_environment(caller)
    assert 'POOL_KEY_NAME' not in service and 'UNDECLARED_API_KEY' not in service and 'QD_SEAT' not in service
    monkeypatch.setattr(os, 'environ', dict(caller))  # the rule below is the unjailed seat's own, read where it is
    filled = key_environment(keyed, 'PROGRAM_API_KEY')  # as a harness fills its program's key variable
    refuse_secret_names('w', {'HOME': '/h', **filled}, keyed)  # the key pool still launches with its key
    with pytest.raises(ShipError, match='names OTHER_TOKEN, which looks secret'):
        refuse_secret_names('w', {**filled, 'OTHER_TOKEN': 'x'}, keyed)
    with pytest.raises(ShipError, match='names PROGRAM_API_KEY, which looks secret'):
        refuse_secret_names('w', filled, plain)  # no declared key: nothing secret-named passes
    refuse_secret_names('w', {'HOME': '/h', 'XDG_CONFIG_HOME': '/c'}, plain)


def test_the_unjailed_list_has_the_desktop_names_and_the_jails_has_none():
    environ = {'DISPLAY': ':0', 'WAYLAND_DISPLAY': 'w', 'XDG_SESSION_TYPE': 'x', 'XDG_CURRENT_DESKTOP': 'G',
               'DBUS_SESSION_BUS_ADDRESS': 'd', 'SSH_AUTH_SOCK': 's', 'PATH': '/bin', 'QD_SEAT': 'una',
               'OPENAI_API_KEY': 'x', 'QD_FAKE_STEPS': '1', 'HARMLESS': 'x'}
    pane = je.unjailed_environment(environ)
    assert set(pane) == {'DISPLAY', 'WAYLAND_DISPLAY', 'XDG_SESSION_TYPE', 'XDG_CURRENT_DESKTOP',
                         'DBUS_SESSION_BUS_ADDRESS', 'SSH_AUTH_SOCK', 'PATH'}
    assert not set(je.DESKTOP) & {*je.ALLOWED, *je.FROM_PANE}, 'the jail never receives the desktop'
    assert not [name for name in (*je.UNJAILED, *je.SERVICE) if je.looks_secret(name)]
