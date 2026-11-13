"""Project sync's ordering, pinned target and truthful stop-on-failure results."""
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from quarterdeck import cli, install, ship as shipmod, sync_all
from quarterdeck.ship import ShipError

OLD = '1' * 40
TARGET = '2' * 40
OTHER = '3' * 40


def write_ship(project, name, *, checkout=False):
    folder = project / 'ships' / name
    folder.mkdir(parents=True)
    text = f'[ship]\nname = "{name}"\noperator = "synthetic-operator-zzzz"\n'
    if checkout:
        text += 'code = "checkout"\n'
    (folder / 'ship.toml').write_text(text)
    return folder


def modeled_project(tmp_path, monkeypatch, *, fail=None, after_failure=None, marked=True):
    project = tmp_path / 'synthetic-project-zzzz'
    crew = ['alpha-crew-zzzz', 'beta-crew-zzzz']
    office = 'aaa-office-zzzz'  # sorts first, but the explicit mark must put it last
    for name in [*crew, office, 'default', 'drydock', 'forum', 'cascade', 'practice', '.hidden-ship-zzzz']:
        write_ship(project, name)
    write_ship(project, 'checkout-ship-zzzz', checkout=True)
    if marked:
        (project / 'ships/.default').write_text(office + '\n')
    states = {name: OLD for name in [*crew, office, 'practice', '.hidden-ship-zzzz']}
    calls, resolutions = [], []
    pinned = (project, TARGET)

    def resolve(repo, ref):
        resolutions.append((repo, ref))
        return pinned

    def observe(folder):
        return states[folder.name], None

    def upgrade(ship, ref, tmux=None, *, force=False, target=None):
        assert target is pinned
        calls.append((ship.folder.name, ref, force))
        if ship.folder.name == fail:
            if after_failure:
                after_failure(states)
            raise ShipError('synthetic per-ship check failed')
        states[ship.folder.name] = target[1]
        return ['synthetic copy checked']

    monkeypatch.setattr(install, 'resolve_target', resolve)
    monkeypatch.setattr(install, 'upgrade', upgrade)
    monkeypatch.setattr(sync_all, 'observe', observe)
    return SimpleNamespace(project=project, calls=calls, resolutions=resolutions, states=states,
                           office=office, crew=crew, pinned=pinned)


def eligible_rows(result):
    return [row for row in result['ships'] if row['disposition'] != 'passed over']


def test_all_ships_succeed_in_folder_order_with_marked_office_last(tmp_path, monkeypatch):
    world = modeled_project(tmp_path, monkeypatch)
    real_default = shipmod.default_ship
    mark_reads = []

    def shared_default(project):
        mark_reads.append(project)
        return real_default(project)

    monkeypatch.setattr(shipmod, 'default_ship', shared_default)
    result = sync_all.run(world.project, 'synthetic-release-zzzz', force=True)
    expected = sorted(world.states.keys() - {world.office}) + [world.office]
    assert [name for name, _, _ in world.calls] == expected
    assert all(ref == 'synthetic-release-zzzz' and force for _, ref, force in world.calls)
    assert result['ok'] and all(row['commit'] == TARGET for row in eligible_rows(result))
    assert all(row['disposition'] == 'synced' for row in eligible_rows(result))
    assert world.resolutions == [(world.project, 'synthetic-release-zzzz')]
    assert mark_reads == [world.project]


def test_second_failure_keeps_first_synced_and_rest_untried(tmp_path, monkeypatch):
    # Use only the two crew ships and marked office to make the contract's second failure exact.
    world = modeled_project(tmp_path, monkeypatch, fail='beta-crew-zzzz')
    for name in ['practice', '.hidden-ship-zzzz']:
        folder = world.project / 'ships' / name
        (folder / 'ship.toml').unlink()
        folder.rmdir()
        del world.states[name]
    result = sync_all.run(world.project)
    rows = eligible_rows(result)
    assert [name for name, _, _ in world.calls] == world.crew
    assert [(row['ship'], row['disposition'], row['commit']) for row in rows] == [
        (world.crew[0], 'synced', TARGET), (world.crew[1], 'failed', OLD),
        (world.office, 'not tried', OLD)]
    assert not result['ok'] and world.states[world.crew[0]] == TARGET
    text = '\n'.join(sync_all.render(result))
    assert f'{world.crew[1]}: failed and still on {OLD}: synthetic per-ship check failed' in text
    assert f'{world.office}: not tried and still on {OLD}' in text


def test_each_template_and_checkout_ship_is_passed_over_once(tmp_path, monkeypatch):
    world = modeled_project(tmp_path, monkeypatch)
    loaded = []
    real_load = shipmod.load

    def load(folder):
        loaded.append(folder.name)
        return real_load(folder)

    monkeypatch.setattr(shipmod, 'load', load)
    result = sync_all.run(world.project)
    skipped = [row for row in result['ships'] if row['disposition'] == 'passed over']
    assert {row['ship'] for row in skipped} == {'default', 'drydock', 'forum', 'cascade', 'checkout-ship-zzzz'}
    assert all(name not in loaded for name in ['default', 'drydock', 'forum', 'cascade'])
    assert 'checkout-ship-zzzz' not in [name for name, _, _ in world.calls]
    lines = sync_all.render(result)
    for row in skipped:
        assert sum(line.startswith(row['ship'] + ': passed over:') for line in lines) == 1
        assert row['reason']


def test_unmarked_multiple_ship_project_needs_no_implicit_default(tmp_path, monkeypatch):
    world = modeled_project(tmp_path, monkeypatch, marked=False)

    def unexpected(*args, **kwargs):
        pytest.fail('unmarked --all should not ask the single-ship fallback to choose a default')

    monkeypatch.setattr(shipmod, 'default_ship', unexpected)
    result = sync_all.run(world.project)
    assert result['ok']
    assert [name for name, _, _ in world.calls] == sorted(world.states)


def test_untried_ship_reports_a_later_independent_copy_change(tmp_path, monkeypatch):
    world = modeled_project(tmp_path, monkeypatch, fail='alpha-crew-zzzz',
                            after_failure=lambda states: states.update({'aaa-office-zzzz': OTHER}))
    result = sync_all.run(world.project)
    row = next(row for row in result['ships'] if row['ship'] == world.office)
    assert row['disposition'] == 'not tried' and row['before'] == OLD and row['commit'] == OTHER
    assert f'{world.office}: not tried and observed on {OTHER}' in '\n'.join(sync_all.render(result))


def test_failure_after_activation_reports_observed_target_not_claimed_restoration(tmp_path, monkeypatch):
    world = modeled_project(tmp_path, monkeypatch, fail='alpha-crew-zzzz',
                            after_failure=lambda states: states.update({'alpha-crew-zzzz': TARGET}))
    result = sync_all.run(world.project)
    row = next(row for row in result['ships'] if row['ship'] == 'alpha-crew-zzzz')
    assert row['disposition'] == 'failed' and row['commit'] == TARGET
    assert f'alpha-crew-zzzz: failed and observed on {TARGET}' in '\n'.join(sync_all.render(result))


def test_copy_observation_failure_reports_unknown(monkeypatch, tmp_path):
    def unreadable(folder):
        raise OSError('synthetic copy metadata unavailable')

    monkeypatch.setattr(sync_all.copy_state, 'select', unreadable)
    commit, reason = sync_all.observe(tmp_path)
    assert commit is None and 'observation failed' in reason


def git(project, *args):
    done = subprocess.run(['git', '-C', str(project), *args], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def test_resolve_target_pins_one_real_local_commit(tmp_path):
    git(tmp_path, 'init', '-q')
    (tmp_path / 'synthetic.txt').write_text('synthetic first revision\n')
    git(tmp_path, 'add', '.')
    git(tmp_path, '-c', 'user.name=synthetic', '-c', 'user.email=synthetic@example.invalid',
        'commit', '-qm', 'Synthetic source revision')
    expected = git(tmp_path, 'rev-parse', 'HEAD')
    assert install.resolve_target(tmp_path) == (tmp_path.resolve(), expected)


def test_moving_head_during_first_ship_does_not_change_any_target(tmp_path, monkeypatch):
    real_resolve = install.resolve_target
    world = modeled_project(tmp_path, monkeypatch)
    git(world.project, 'init', '-q')
    git(world.project, 'add', '.')
    git(world.project, '-c', 'user.name=synthetic', '-c', 'user.email=synthetic@example.invalid',
        'commit', '-qm', 'Synthetic first fleet source')
    first = git(world.project, 'rev-parse', 'HEAD')
    resolutions, targets = [], []

    def resolve(project, ref):
        resolutions.append((project, ref))
        return real_resolve(project, ref)

    def upgrade(ship, ref, tmux=None, *, force=False, target=None):
        targets.append(target[1])
        if len(targets) == 1:
            (world.project / 'synthetic-next.txt').write_text('synthetic next source\n')
            git(world.project, 'add', '.')
            git(world.project, '-c', 'user.name=synthetic', '-c', 'user.email=synthetic@example.invalid',
                'commit', '-qm', 'Synthetic later fleet source')
        world.states[ship.folder.name] = target[1]
        return ['synthetic copy checked']

    monkeypatch.setattr(install, 'resolve_target', resolve)
    monkeypatch.setattr(install, 'upgrade', upgrade)
    result = sync_all.run(world.project)
    assert git(world.project, 'rev-parse', 'HEAD') != first
    assert resolutions == [(world.project, 'HEAD')]
    assert targets == [first] * len(world.states)
    assert result['target'] == first and result['ok']


@pytest.mark.parametrize('verb', ['sync', 'upgrade'])
def test_all_back_refusal_precedes_project_or_install_work(monkeypatch, capsys, verb):
    def unexpected(*args, **kwargs):
        pytest.fail('incompatible options reached project sync')

    monkeypatch.setattr(sync_all, 'run', unexpected)
    assert cli.main([verb, '--all', '--back']) == 2
    assert 'cannot be combined with --back' in capsys.readouterr().err


def test_explicit_ship_conflicts_with_project_all_before_any_sync(monkeypatch, capsys, tmp_path):
    def unexpected(*args, **kwargs):
        pytest.fail('explicit --ship plus --all reached a sync')

    monkeypatch.setattr(sync_all, 'run', unexpected)
    assert cli.main(['--ship', str(tmp_path / 'synthetic-named-zzzz'), 'sync', '--all']) == 2
    assert '--all selects project ships; omit --ship' in capsys.readouterr().err


@pytest.mark.parametrize('verb', ['sync', 'upgrade'])
def test_cli_json_failure_keeps_structured_dispositions(tmp_path, monkeypatch, capsys, verb):
    from quarterdeck import standing

    world = modeled_project(tmp_path, monkeypatch, fail='alpha-crew-zzzz')
    monkeypatch.setattr(standing, 'PROJECT_DIR', world.project)
    monkeypatch.setenv('QD_SHIP', str(tmp_path / 'unrelated-ship-zzzz'))
    monkeypatch.delenv('QD_SEAT', raising=False)
    assert cli.main(['--json', verb, '--all']) == 2
    found = json.loads(capsys.readouterr().out)
    assert found['target'] == TARGET and not found['ok']
    assert any(row['disposition'] == 'failed' for row in found['ships'])
    assert any(row['disposition'] == 'not tried' for row in found['ships'])


def test_upgrade_all_alias_note_is_once_and_json_data_is_unmodified(tmp_path, monkeypatch, capsys):
    from quarterdeck import standing

    world = modeled_project(tmp_path, monkeypatch)
    monkeypatch.setattr(standing, 'PROJECT_DIR', world.project)
    monkeypatch.delenv('QD_SHIP', raising=False)
    monkeypatch.delenv('QD_SEAT', raising=False)
    assert cli.main(['upgrade', '--all']) == 0
    text = capsys.readouterr().out
    assert text.count('`upgrade` is now `qd sync`') == 1
    assert 'synced to ' + TARGET in text
