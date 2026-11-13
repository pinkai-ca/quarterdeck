"""Synthetic functional transactions; proof and process internals are separate gates."""

import json
import shutil

import pytest

from quarterdeck import copy_state, install
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
    assert not (tx.ship.runtime / 'venv.backup').exists()


# Every phase a sync can fail in, with the failure that stops it there; 'start' and 'rebrief' need a running ship.
PHASES = [('archive', 'preparing'), ('declaration', 'preparing'), ('stop', 'stopping'),
          ('snapshot', 'snapshotting'), ('copy', 'backing_up'), ('install', 'installing'),
          ('resources', 'resources'), ('proof', 'proving'), ('start', 'target_ready'),
          ('rebrief', 'target_ready')]


@pytest.mark.parametrize('failure, phase, running', [
    (failure, phase, running) for failure, phase in PHASES for running in (False, True)
    if running or failure not in ('stop', 'start')])  # a stopped ship is neither stopped nor started
def test_failed_sync_keeps_both_copies_and_their_record_and_back_still_returns(transaction, failure, phase, running):
    tx = transaction
    tx.seed(previous=True)
    live, previous = snapshot(tx.ship.runtime / 'venv'), snapshot(tx.ship.runtime / 'venv.prev')
    record = tx.read()
    tx.running, tx.failure = running, failure
    with pytest.raises(ShipError, match='recovery completed'):
        install.upgrade(tx.ship)
    assert snapshot(tx.ship.runtime / 'venv') == live
    assert snapshot(tx.ship.runtime / 'venv.prev') == previous
    assert not (tx.ship.runtime / 'venv.backup').exists() and not (tx.ship.runtime / 'venv.swap').exists()
    assert not (tx.ship.runtime / 'upgrade').exists()
    data = tx.read()
    assert (data['current'], data['previous_copy'], data['pending']) == (
        record['current'], record['previous_copy'], record['pending'])
    assert data['last_failure']['phase'] == phase  # the one field a failure adds: why it failed
    assert tx.running is running
    if running and failure != 'stop':
        assert tx.named('start')[-1] == ('start', 'original')
    tx.failure = None
    proofs = install.upgrade(tx.ship, back=True)
    assert any(f'returned {OLDER[:7]} at runtime/venv' in line for line in proofs)
    assert (tx.ship.runtime / 'venv/content.txt').read_text() == 'previous'
    assert (tx.ship.runtime / 'venv.prev/content.txt').read_text() == 'original'
    assert (tx.read()['current']['commit'], tx.read()['previous_copy']['commit']) == (OLDER, OLD)


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
    assert (tx.ship.runtime / 'venv.prev/content.txt').read_text() == 'previous'
    assert not (tx.ship.runtime / 'venv.swap').exists()
    data = tx.read()
    assert data['current'] == fact(OLD)
    assert data['previous_copy'] == fact(OLDER) and data['pending'] is None
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
    assert not (tx.ship.runtime / 'venv.backup').exists()
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
    assert (tx.ship.runtime / 'venv.prev/content.txt').read_text() == 'previous'
    assert not (tx.ship.runtime / 'venv.backup').exists()
    assert tx.read()['current'] == fact(OLD)
    assert tx.read()['previous_copy'] == fact(OLDER)
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
    assert not (tx.ship.runtime / 'venv.swap').exists() and not (tx.ship.runtime / 'venv.backup').exists()
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


@pytest.mark.parametrize('back', [False, True])
@pytest.mark.parametrize('command', ['origin', '--version', 'laws', '--help', None])
def test_proof_failure_keeps_both_fixed_category_and_proved_command(transaction, monkeypatch, command, back):
    tx = transaction
    if back:
        tx.seed(previous=True)
        previous = tx.read()
        previous['previous_copy']['proof_command'] = '--help'
        (tx.ship.runtime / 'installed.json').write_text(json.dumps(previous))
    def failed(ship, selected):
        if command is not None:
            selected['proof_command'] = command
        raise ShipError('synthetic proof step failed')
    monkeypatch.setattr(install, '_proof', failed)
    with pytest.raises(ShipError, match='proof command failed' + (f' at {command}' if command else ';')):
        install.upgrade(tx.ship, back=back)
    failure = tx.read()['last_failure']
    assert failure['reason'] == 'proof command failed'
    assert failure.get('step') == command
    assert 'cause' not in failure and 'proof_command' not in failure


def test_backup_becomes_the_previous_copy_only_after_activation_and_a_cut_cleanup_finishes_it(transaction, monkeypatch):
    tx = transaction
    tx.seed(previous=True)
    rename, cut = install._rename, []

    def cut_once(ship, data, source, destination, **kwargs):
        if (source, destination) == ('venv.backup', 'venv.prev') and not cut:
            cut.append(True)
            assert data['pending']['activated'] and data['current']['commit'] == NEW
            assert (ship.runtime / 'venv.backup/content.txt').read_text() == 'original'
            raise ShipError('synthetic cut before the backup takes the previous role')
        return rename(ship, data, source, destination, **kwargs)

    monkeypatch.setattr(install, '_rename', cut_once)
    with pytest.raises(ShipError, match='copy active but cleanup incomplete'):
        install.upgrade(tx.ship)
    assert tx.read()['pending']['activated'] is True
    assert install.recover(tx.ship) == ['completed the activated copy cleanup']
    assert (tx.ship.runtime / 'venv/content.txt').read_text() == 'new candidate'
    assert (tx.ship.runtime / 'venv.prev/content.txt').read_text() == 'original'
    assert not (tx.ship.runtime / 'venv.backup').exists()
    data = tx.read()
    assert (data['current']['commit'], data['previous_copy']['commit'], data['pending']) == (NEW, OLD, None)


def test_a_journal_from_before_the_backup_role_still_recovers_from_its_backup_at_prev(transaction):
    """An interrupted sync by the earlier code kept its backup at venv.prev and had consumed the older copy."""
    tx = transaction
    tx.seed()
    runtime = tx.ship.runtime
    shutil.copytree(runtime / 'venv', runtime / 'venv.prev')  # the earlier code's complete backup
    (runtime / 'venv/content.txt').write_text('new candidate')  # its interrupted install
    data = tx.read()
    data['pending'] = dict(kind='sync', token='a' * 32, phase='installing', target=fact(NEW),
                           old_current=fact(), old_previous=fact(OLDER),
                           old_live_identity=copy_state.root_id(runtime / 'venv'),
                           old_previous_identity=None, was_running=False, mutable=True,
                           backup_started=True, backup_ready=True,
                           backup_identity=copy_state.root_id(runtime / 'venv.prev'),
                           moves=[], rename=None, activated=False, restored=False)
    (runtime / 'installed.json').write_text(json.dumps(data) + '\n')
    assert install.recover(tx.ship)[0] == 'interrupted copy operation undone; earlier copy restored'
    assert (runtime / 'venv/content.txt').read_text() == 'original'
    assert not (runtime / 'venv.prev').exists()
    data = tx.read()
    assert (data['current'], data['previous_copy'], data['pending']) == (fact(), None, None)


def test_a_stray_backup_without_a_journal_refuses_before_any_change(transaction):
    tx = transaction
    tx.seed(previous=True)
    (tx.ship.runtime / 'venv.backup').mkdir()
    record = tx.read()
    with pytest.raises(ShipError, match='venv.backup exists without a pending operation'):
        install.upgrade(tx.ship)
    assert tx.read() == record
