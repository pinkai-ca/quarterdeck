"""Synthetic functional transactions; proof and process internals are separate gates."""

import pytest

from quarterdeck import install
from quarterdeck.ship import ShipError
from tests.unit.own_copy_synthetic import (
    NEW, OLD, OLDER, fact, snapshot, transaction, unexpected, vessel,
)


def test_first_install_returns_nonempty_proof_at_canonical_path(transaction):
    tx = transaction
    proofs = install.upgrade(tx.ship)
    assert proofs
    assert any(f'installed {NEW[:7]} at runtime/venv' in line for line in proofs)
    assert any('no proven previous copy' in line for line in proofs)
    assert len(tx.named('environment')) == len(tx.named('install')) == len(tx.named('proof')) == 1
    assert tx.named('proof')[0][1] == 'venv'
    assert tx.named('start') == tx.named('stop') == tx.named('copy') == []
    assert tx.read()['current']['commit'] == NEW
    assert tx.read()['pending'] is None
    assert not (tx.ship.runtime / 'venv.prev').exists()


@pytest.mark.parametrize('failure', ['install', 'resources', 'proof'])
@pytest.mark.parametrize('running', [False, True])
def test_failed_sync_restores_original_at_same_path_and_prior_service(transaction, failure, running):
    tx = transaction
    tx.seed(previous=True)
    before = snapshot(tx.ship.runtime / 'venv')
    tx.running, tx.failure = running, failure
    with pytest.raises(ShipError, match='recovery completed'):
        install.upgrade(tx.ship)
    assert snapshot(tx.ship.runtime / 'venv') == before
    assert not (tx.ship.runtime / 'venv.prev').exists()
    assert not (tx.ship.runtime / 'venv.swap').exists()
    data = tx.read()
    assert data['current'] == fact(OLD)
    assert data['previous_copy'] is None and data['pending'] is None
    assert data['last_failure']['phase'] == ('proving' if failure == 'proof' else
                                             'resources' if failure == 'resources' else 'installing')
    assert tx.running is running
    assert tx.named('start') == ([('start', 'original')] if running else [])
    assert len(tx.named('copy')) == 1


@pytest.mark.parametrize('failure', ['install', 'resources', 'proof'])
def test_failed_first_install_leaves_no_half_copy_or_service(transaction, failure):
    tx = transaction
    tx.failure = failure
    with pytest.raises(ShipError, match='incomplete first install removed'):
        install.upgrade(tx.ship)
    assert not (tx.ship.runtime / 'venv').exists()
    assert not (tx.ship.runtime / 'venv.prev').exists()
    assert not (tx.ship.runtime / 'upgrade').exists()
    assert tx.named('start') == tx.named('stop') == []
    assert tx.read()['current'] is None and tx.read()['pending'] is None
    assert tx.read()['last_failure'] is not None


def test_failed_backup_keeps_current_and_restarts_it(transaction):
    tx = transaction
    tx.seed(previous=True)
    before = snapshot(tx.ship.runtime / 'venv')
    tx.running, tx.failure = True, 'copy'
    with pytest.raises(ShipError, match='recovery completed'):
        install.upgrade(tx.ship)
    assert snapshot(tx.ship.runtime / 'venv') == before
    assert tx.named('install') == tx.named('proof') == []
    assert tx.named('start') == [('start', 'original')]
    assert not (tx.ship.runtime / 'venv.prev').exists()
    assert tx.read()['current'] == fact(OLD)
    assert tx.read()['previous_copy'] is None
    assert tx.read()['last_failure']['phase'] == 'backing_up'


def test_same_commit_is_true_noop_before_lock_backup_or_state_write(transaction, monkeypatch):
    tx = transaction
    tx.seed(previous=True)
    tx.commit, tx.running = OLD, True
    before = snapshot(tx.ship.folder)
    for seam in ('install_lock', '_save', '_snapshot_operation', '_proof', 'queue_rebrief'):
        monkeypatch.setattr(install, seam, unexpected)
    proofs = install.upgrade(tx.ship)
    assert proofs[-1] == f'already runs {OLD[:7]}; nothing to do'
    assert snapshot(tx.ship.folder) == before
    assert all(call[0] == 'git' for call in tx.calls)
    assert tx.running is True


def test_force_same_commit_reinstalls_and_replaces_only_backup(transaction):
    tx = transaction
    tx.seed(previous=True)
    tx.commit, tx.running = OLD, True
    proofs = install.upgrade(tx.ship, force=True)
    assert any(f'installed {OLD[:7]} at runtime/venv' in line for line in proofs)
    assert len(tx.named('copy')) == len(tx.named('install')) == len(tx.named('proof')) == 1
    assert tx.named('environment') == []
    assert (tx.ship.runtime / 'venv/content.txt').read_text() == 'new candidate'
    assert (tx.ship.runtime / 'venv.prev/content.txt').read_text() == 'original'
    assert not (tx.ship.runtime / 'venv.prev/venv').exists()
    assert not (tx.ship.runtime / 'venv.swap').exists()
    assert tx.read()['previous_copy']['commit'] == OLD
    assert tx.named('start') == [('start', 'new candidate')]
    assert tx.running is True


def test_back_swaps_three_names_proves_live_and_needs_no_source(transaction, monkeypatch):
    tx = transaction
    tx.seed(previous=True)
    moves = []
    rename = install._rename

    def record(ship, data, source, destination, **kwargs):
        moves.append((source, destination))
        return rename(ship, data, source, destination, **kwargs)

    monkeypatch.setattr(install, '_rename', record)
    monkeypatch.setattr(install, 'source_repo', unexpected)
    monkeypatch.setattr(install, 'git', unexpected)
    proofs = install.upgrade(tx.ship, back=True)
    assert moves == [('venv', 'venv.swap'), ('venv.prev', 'venv'), ('venv.swap', 'venv.prev')]
    assert tx.named('proof') == [('proof', 'venv', OLDER, 'previous')]
    assert tx.named('archive') == tx.named('install') == tx.named('copy') == []
    assert any(f'returned {OLDER[:7]} at runtime/venv' in line and f'was {OLD[:7]}' in line
               for line in proofs)
    assert tx.read()['current']['commit'] == OLDER
    assert tx.read()['previous_copy']['commit'] == OLD
    assert not (tx.ship.runtime / 'venv.swap').exists()


def test_second_back_returns_to_other_copy_without_rebuilding(transaction):
    tx = transaction
    tx.seed(previous=True)
    install.upgrade(tx.ship, back=True)
    proofs = install.upgrade(tx.ship, back=True)
    assert tx.read()['current']['commit'] == OLD
    assert tx.read()['previous_copy']['commit'] == OLDER
    assert (tx.ship.runtime / 'venv/content.txt').read_text() == 'original'
    assert (tx.ship.runtime / 'venv.prev/content.txt').read_text() == 'previous'
    assert any(f'returned {OLD[:7]}' in line for line in proofs)
    assert tx.named('install') == tx.named('archive') == tx.named('git') == []
    assert len(tx.named('proof')) == 2


@pytest.mark.parametrize('running', [False, True])
def test_back_failed_proof_restores_original_and_discards_candidate(transaction, running):
    tx = transaction
    tx.seed(previous=True)
    before = snapshot(tx.ship.runtime / 'venv')
    tx.failure, tx.running = 'proof', running
    with pytest.raises(ShipError, match='recovery completed'):
        install.upgrade(tx.ship, back=True)
    assert snapshot(tx.ship.runtime / 'venv') == before
    assert not (tx.ship.runtime / 'venv.prev').exists()
    assert not (tx.ship.runtime / 'venv.swap').exists()
    assert tx.read()['current'] == fact(OLD)
    assert tx.read()['previous_copy'] is None and tx.read()['pending'] is None
    assert tx.read()['last_failure']['phase'] == 'proving'
    assert tx.named('start') == ([('start', 'original')] if running else [])
    assert tx.running is running
    assert tx.named('install') == tx.named('archive') == tx.named('git') == []


def test_legacy_without_backup_refuses_back_and_keeps_record(transaction):
    tx = transaction
    tx.seed(legacy=True)
    before = snapshot(tx.ship.folder)
    with pytest.raises(ShipError, match='first successful sync creates one'):
        install.upgrade(tx.ship, back=True)
    assert snapshot(tx.ship.folder) == before
    assert tx.named('proof') == tx.named('start') == tx.named('install') == []


def test_legacy_same_commit_noop_warns_without_migration_or_reproof(transaction):
    tx = transaction
    tx.seed(legacy=True)
    tx.commit = OLD
    before = snapshot(tx.ship.folder)
    proofs = install.upgrade(tx.ship)
    assert proofs[-1] == f'already runs {OLD[:7]}; nothing to do'
    assert any('historical target' in line and 'old in-place management' in line for line in proofs)
    assert snapshot(tx.ship.folder) == before
    assert tx.named('proof') == tx.named('copy') == tx.named('lock') == []


@pytest.mark.parametrize('failure, cause', [
    ('environment', 'environment could not be made'),
    ('install', 'package build failed'),
    ('resources', 'resource synchronization failed'),
    ('proof', 'proof command failed'),
])
def test_failed_first_copy_names_the_fixed_cause_in_message_and_record(transaction, failure, cause):
    tx = transaction
    tx.failure = failure
    with pytest.raises(ShipError, match=cause):
        install.upgrade(tx.ship)
    assert tx.read()['last_failure']['reason'] == cause
    assert not (tx.ship.runtime / 'venv').exists()


def test_bad_resource_declaration_names_its_fixed_cause(transaction, monkeypatch):
    tx = transaction
    def unavailable(source):
        raise ValueError('synthetic declaration failure')
    monkeypatch.setattr(install.resources, 'plan', unavailable)
    with pytest.raises(ShipError, match='resource declaration invalid'):
        install.upgrade(tx.ship)
    assert tx.read()['last_failure']['reason'] == 'resource declaration invalid'


@pytest.mark.parametrize('command', ['origin', '--version', 'laws', '--help', None])
def test_proof_failure_keeps_both_fixed_category_and_proved_command(transaction, monkeypatch, command):
    tx = transaction
    def failed(ship, selected):
        if command is not None:
            selected['proof_command'] = command
        raise ShipError('synthetic proof step failed')
    monkeypatch.setattr(install, '_proof', failed)
    with pytest.raises(ShipError, match='proof command failed' + (f' at {command}' if command else ';')):
        install.upgrade(tx.ship)
    failure = tx.read()['last_failure']
    assert failure['reason'] == 'proof command failed'
    assert failure.get('step') == command
    assert 'cause' not in failure and 'proof_command' not in failure
