"""Functional declaration and shared-up gates on the isolated source contract.

Setup is not composed at this pin. These exercise cmd_up, its intended shared
entry point, without asserting setup consent or the later sync parser integration.
"""

from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from quarterdeck import cli, copy_state, install, ship as shipmod
from quarterdeck.ship import ShipError
from tests.unit.own_copy_synthetic import NEW, OLD, fact, inert_copy, state, unexpected, vessel


@pytest.mark.parametrize('line, expected', [('', None), ('code = "checkout"\n', 'checkout')])
def test_load_code_mode(vessel, line, expected):
    vessel.file.write_text('[ship]\nname = "synthetic-vessel"\n' + line)
    assert shipmod.load(vessel.folder).code == expected


@pytest.mark.parametrize('literal', ['"installed"', '""', 'true', '12', '[]', '{}'])
def test_load_bad_code_names_allowed_forms(vessel, literal):
    vessel.file.write_text('[ship]\ncode = ' + literal + '\n')
    with pytest.raises(ShipError) as refused:
        shipmod.load(vessel.folder)
    assert str(refused.value) == 'ship.code must be absent, or "checkout"'


def test_checkout_up_does_not_install_or_recover(vessel, monkeypatch):
    monkeypatch.setattr(install, 'recover', unexpected)
    monkeypatch.setattr(install, 'upgrade', unexpected)
    monkeypatch.setattr(install, 'dirty_notice', lambda ship, fact=None: [])
    assert install.prepare_up(replace(vessel, code='checkout')) == ([], False)


def test_checkout_sync_refusal(vessel):
    with pytest.raises(ShipError) as refused:
        install.upgrade(replace(vessel, code='checkout'))
    assert str(refused.value) == ('this ship follows the checkout; it has no copy to sync; '
                                  'remove code = "checkout" to give it one')


def test_first_up_prepares_one_copy_and_requests_reload(vessel, monkeypatch):
    calls = []
    monkeypatch.setattr(install, 'recover', lambda ship: [])

    def install_one(ship):
        calls.append(ship)
        inert_copy(ship.runtime / 'venv', 'installed')
        state(ship, fact(NEW))
        return ['synthetic first copy installed']

    monkeypatch.setattr(install, 'upgrade', install_one)
    assert install.prepare_up(vessel) == (['synthetic first copy installed'], True)
    assert calls == [vessel]


def test_existing_up_keeps_installed_commit_and_reports_it(vessel, monkeypatch):
    inert_copy(vessel.runtime / 'venv', 'installed')
    state(vessel, fact(OLD))
    monkeypatch.setattr(install, 'recover', lambda ship: [])
    monkeypatch.setattr(install, 'upgrade', unexpected)
    seen = []

    def notice(ship, selected=None):
        seen.append(selected['commit'])
        return ['synthetic dirty notice']

    monkeypatch.setattr(install, 'dirty_notice', notice)
    proofs, reload_required = install.prepare_up(vessel)
    assert seen == [OLD]
    assert proofs[0] == 'synthetic dirty notice'
    assert reload_required is False


@pytest.mark.parametrize('name', [None, 'synthetic-worker'])
def test_shared_up_prepares_then_delegates_once(vessel, monkeypatch, name):
    calls = []
    if name:
        vessel.seats[name] = SimpleNamespace(name=name)
    monkeypatch.setattr(cli, 'load_ship', lambda args: vessel)

    def prepare(ship):
        calls.append('install')
        inert_copy(ship.runtime / 'venv', 'first installed copy')
        state(ship, fact(NEW))
        return ['synthetic copy prepared']

    def child(argv, **kwargs):
        calls.append(('child', argv))
        assert copy_state.select(vessel.folder).qd is not None
        return SimpleNamespace(returncode=0, stdout=json.dumps(['synthetic child launch']))

    monkeypatch.setattr(install, 'recover', lambda ship: [])
    monkeypatch.setattr(install, 'upgrade', prepare)
    monkeypatch.setattr(install, 'running_from', lambda path: False)
    monkeypatch.setattr(install.subprocess, 'run', child)
    monkeypatch.setattr(cli, 'seat_of', unexpected)
    monkeypatch.setattr(cli.seatmod, 'bring_up', unexpected)
    monkeypatch.setattr(cli.service, 'start', unexpected)
    args = SimpleNamespace(seat=name, fresh=True)
    assert cli.cmd_up(args) == ['synthetic copy prepared', 'synthetic child launch']
    argv = [str(vessel.runtime / 'venv/bin/qd'), '--ship', str(vessel.folder), '--json', 'up']
    assert calls == ['install', ('child', argv + ([name] if name else []) + ['--fresh'])]


@pytest.mark.parametrize('name', [None, 'synthetic-worker'])
def test_shared_up_install_failure_starts_nothing(vessel, monkeypatch, name):
    if name:
        vessel.seats[name] = SimpleNamespace(name=name)
    monkeypatch.setattr(cli, 'load_ship', lambda args: vessel)

    def fail(ship):
        raise ShipError('synthetic first install failed')

    monkeypatch.setattr(install, 'upgrade', fail)
    monkeypatch.setattr(install, 'recover', lambda ship: [])
    monkeypatch.setattr(install, 'delegate_up', unexpected)
    monkeypatch.setattr(cli, 'seat_of', unexpected)
    monkeypatch.setattr(cli.seatmod, 'bring_up', unexpected)
    monkeypatch.setattr(cli.service, 'start', unexpected)
    with pytest.raises(ShipError, match='synthetic first install failed'):
        cli.cmd_up(SimpleNamespace(seat=name, fresh=False))


@pytest.mark.parametrize('name, fresh', [(None, False), ('synthetic-worker', True)])
def test_delegation_preserves_ship_seat_fresh_and_child_proof(vessel, monkeypatch, name, fresh):
    inert_copy(vessel.runtime / 'venv', 'installed')
    state(vessel, fact())
    monkeypatch.setattr(install, 'running_from', lambda path: True)
    calls = []

    def child(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(['synthetic launched']))

    monkeypatch.setattr(install.subprocess, 'run', child)
    assert install.delegate_up(vessel, seat=name, fresh=fresh, reload_required=True) == ['synthetic launched']
    argv = [str(vessel.runtime / 'venv/bin/qd'), '--ship', str(vessel.folder), '--json', 'up']
    if name:
        argv += [name]
    if fresh:
        argv += ['--fresh']
    assert calls == [(argv, dict(capture_output=True, text=True))]


@pytest.mark.parametrize('outcome', ['cannot-start', 'failed', 'unreadable', 'wrong-shape'])
def test_child_failure_never_replays_launch_in_caller(vessel, monkeypatch, outcome):
    inert_copy(vessel.runtime / 'venv', 'installed')
    state(vessel, fact())
    monkeypatch.setattr(cli, 'load_ship', lambda args: vessel)
    monkeypatch.setattr(install, 'prepare_up', lambda ship: ([], True))
    monkeypatch.setattr(install, 'running_from', lambda path: False)
    monkeypatch.setattr(cli, 'seat_of', unexpected)
    monkeypatch.setattr(cli.seatmod, 'bring_up', unexpected)
    monkeypatch.setattr(cli.service, 'start', unexpected)
    calls = []

    def child(argv, **kwargs):
        calls.append(argv)
        if outcome == 'cannot-start':
            raise OSError('synthetic process failure')
        return SimpleNamespace(returncode=1 if outcome == 'failed' else 0,
                               stdout={'unreadable': '{', 'wrong-shape': '{}'}.get(outcome, '[]'))

    monkeypatch.setattr(install.subprocess, 'run', child)
    with pytest.raises(ShipError, match='no (fallback or )?retry made'):
        cli.cmd_up(SimpleNamespace(seat=None, fresh=False))
    assert len(calls) == 1


@pytest.mark.parametrize('code', [None, 'checkout'])
def test_dirty_notice_uses_selected_commit_or_checkout_files(vessel, monkeypatch, code):
    calls = []
    monkeypatch.setattr(install, 'source_repo', lambda ship: vessel.folder)

    def git(repo, *args):
        calls.append(args)
        assert args == ('status', '--porcelain', '--untracked-files=normal')
        return b' M synthetic.py\n'

    monkeypatch.setattr(install, 'git', git)
    notice = install.dirty_notice(replace(vessel, code=code), fact(OLD))
    expected = ('this ship follows the checkout, including its working files' if code else
                f'copy is commit {OLD[:7]}, not the working files')
    assert notice == ['uncommitted changes · ' + expected]
    assert len(calls) == 1
