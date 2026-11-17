"""Setup resolves Git identity before writing, using only temporary homes and fake operations."""
import os
from types import SimpleNamespace

import pytest

from quarterdeck import cli, setup, ship
from quarterdeck.drill import Check


@pytest.fixture
def project(tmp_path, monkeypatch):
    home, folder = tmp_path / 'home', tmp_path / 'project'
    home.mkdir()
    folder.mkdir()
    for key in tuple(os.environ):
        if key.startswith('GIT_') or key in ('EMAIL', 'QD_SHIP', 'QD_SEAT'):
            monkeypatch.delenv(key)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(home / 'config'))
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(home / '.gitconfig'))
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    ship._git(folder, 'init', '--quiet')
    monkeypatch.setattr(setup.Operations, 'project_folder', lambda _: folder)
    monkeypatch.setattr(setup.Operations, 'up_desk', lambda *_: pytest.fail('no launch was requested'))
    return folder


def snapshot(folder):
    return {str(path.relative_to(folder)): path.read_bytes() if path.is_file() else None
            for path in folder.rglob('*')}


@pytest.mark.parametrize('config', [(), ('name',), ('email',)])
def test_missing_git_identity_refuses_before_any_office_file(project, capsys, config):
    for key in config:
        ship._git(project, 'config', '--global', f'user.{key}',
                  'Synthetic Example' if key == 'name' else 'synthetic@example.invalid')
    before = snapshot(project)
    assert cli.main(['setup', '--accept', 'yes', '--start', 'no']) == 2
    error = capsys.readouterr().err
    assert 'Git needs your name and email before setup can write files' in error
    assert 'git config --global user.name "Your Name" && git config --global user.email "you@example.com"' in error
    assert snapshot(project) == before


def test_init_also_checks_identity_before_creating_its_office(project):
    before = snapshot(project)
    with pytest.raises(ship.ShipError, match='Git needs your name and email'):
        setup.init_office(SimpleNamespace(ship=None), start=False)
    assert snapshot(project) == before


def test_declining_setup_still_needs_no_identity(project, capsys):
    before = snapshot(project)
    assert cli.main(['setup', '--accept', 'no']) == 0
    assert 'no office changes were written' in capsys.readouterr().out
    assert snapshot(project) == before


@pytest.mark.parametrize('source', ['global', 'project', 'include', 'environment'])
def test_git_resolved_identity_makes_the_initial_commit_without_reconfiguring(project, monkeypatch, source):
    name, email = 'Synthetic Example', 'synthetic@example.invalid'
    if source == 'environment':
        for role in ('AUTHOR', 'COMMITTER'):
            monkeypatch.setenv(f'GIT_{role}_NAME', name)
            monkeypatch.setenv(f'GIT_{role}_EMAIL', email)
    elif source == 'include':
        included = project.parent / 'included.gitconfig'
        included.write_text(f'[user]\nname = {name}\nemail = {email}\n')
        ship._git(project, 'config', f'includeIf.gitdir:{project}/.path', str(included))
    else:
        scope = '--global' if source == 'global' else '--local'
        ship._git(project, 'config', scope, 'user.name', name)
        ship._git(project, 'config', scope, 'user.email', email)
    before = (project / '.git' / 'config').read_bytes()
    assert cli.main(['setup', '--accept', 'yes', '--start', 'no']) == 0
    office = project / 'office'
    assert ship._git(office, 'log', '-1', '--format=%an|%ae|%cn|%ce') == f'{name}|{email}|{name}|{email}'
    assert ship._git(office, 'status', '--porcelain') == ''
    assert (project / '.git' / 'config').read_bytes() == before
    child_config = (office / '.git' / 'config').read_text()
    assert '[user]' not in child_config and '[author]' not in child_config and '[committer]' not in child_config


class CrewOperations(setup.Operations):
    def discover(self, defaults):
        return {'codex': (Check('codex', True, 'synthetic program'), 'synthetic sign-in')}

    def readiness(self, current, selected):
        return [Check('readiness', True, 'synthetic')]


def crew(project):
    answers = dict(folder=str(project / 'ships' / 'crew'), name='example', workspace=str(project),
                   programs='codex', payment={'codex': 'subscription'},
                   subscription_home={'codex': str(project / 'synthetic-sign-in')},
                   use_defaults='yes', accept='yes', start='no', check='no')
    return setup.first_step(SimpleNamespace(json=False), operations=CrewOperations(),
                            conversation=setup.Conversation(answers, interactive=False, write=lambda _: None))


def test_crew_setup_preflights_before_writing_either_ship(project):
    (project / 'ships').mkdir()
    before = snapshot(project)
    with pytest.raises(ship.ShipError, match='Git needs your name and email'):
        crew(project)
    assert snapshot(project) == before


def test_project_identity_is_used_for_both_new_repositories(project):
    (project / 'ships').mkdir()
    ship._git(project, 'config', 'user.name', 'Synthetic Local')
    ship._git(project, 'config', 'user.email', 'local@example.invalid')
    result = crew(project)
    assert any('live checks declined' in line for line in result)
    for folder in (project / 'office', project / 'ships' / 'crew'):
        assert ship._git(folder, 'log', '-1', '--format=%an|%ae') == 'Synthetic Local|local@example.invalid'
        assert ship._git(folder, 'status', '--porcelain') == ''


# -- update mode: an identity only where a commit follows ---------------------------------------------------------

def rerun(project, **changes):
    """Setup run again on the crew, in update mode, with the first run's answers and ``changes``."""
    answers = dict(folder=str(project / 'ships' / 'crew'), name='example', workspace=str(project),
                   programs='codex', payment={'codex': 'subscription'},
                   subscription_home={'codex': str(project / 'synthetic-sign-in')},
                   use_defaults='yes', accept='yes', start='no', check='no', existing='update', **changes)
    return setup.first_step(SimpleNamespace(json=False), operations=CrewOperations(),
                            conversation=setup.Conversation(answers, interactive=False, write=lambda _: None))


@pytest.fixture
def crew_without_identity(project):
    """The office and crew made with the project's own identity, which is then taken away: the throwaway HOME has
    none either, so Git can resolve no identity anywhere."""
    (project / 'ships').mkdir()
    ship._git(project, 'config', 'user.name', 'Synthetic Local')
    ship._git(project, 'config', 'user.email', 'local@example.invalid')
    crew(project)
    ship._git(project, 'config', '--unset', 'user.name')
    ship._git(project, 'config', '--unset', 'user.email')
    folder = project / 'ships' / 'crew'
    with pytest.raises(ship.ShipError, match='Git needs your name and email'):
        setup.git_identity(folder)
    return folder


def test_an_update_that_changes_nothing_needs_no_identity(crew_without_identity):
    folder = crew_without_identity
    before = (folder / 'ship.toml').read_bytes(), ship._git(folder, 'rev-parse', 'HEAD')
    result = rerun(folder.parent.parent)
    assert any('live checks declined' in line for line in result)
    assert ((folder / 'ship.toml').read_bytes(), ship._git(folder, 'rev-parse', 'HEAD')) == before


def test_an_update_that_will_commit_refuses_without_an_identity_before_writing(crew_without_identity, capsys):
    folder = crew_without_identity
    before = snapshot(folder.parent.parent)
    head = ship._git(folder, 'rev-parse', 'HEAD')
    with pytest.raises(ship.ShipError) as caught:
        rerun(folder.parent.parent, operator='Synthetic Operator')
    assert 'Git needs your name and email before setup can write files' in str(caught.value)
    assert ('git config --global user.name "Your Name" && git config --global user.email "you@example.com"'
            in str(caught.value))
    assert snapshot(folder.parent.parent) == before and ship._git(folder, 'rev-parse', 'HEAD') == head


def test_an_update_that_will_commit_commits_with_the_identity_git_resolves(crew_without_identity):
    folder = crew_without_identity
    ship._git(folder.parent.parent, 'config', '--global', 'user.name', 'Synthetic Global')
    ship._git(folder.parent.parent, 'config', '--global', 'user.email', 'global@example.invalid')
    head = ship._git(folder, 'rev-parse', 'HEAD')
    rerun(folder.parent.parent, operator='Synthetic Operator')
    assert ship.load(folder).operator == 'Synthetic Operator'
    assert ship._git(folder, 'rev-parse', 'HEAD^') == head
    assert ship._git(folder, 'log', '-1', '--format=%an|%ae') == 'Synthetic Global|global@example.invalid'


def test_a_ship_that_is_no_repository_is_refused_for_that_reason_before_any_identity(crew_without_identity):
    """Setup updates only a ship that is its own repository; a ship that is not is refused for that reason, as it
    always was, whatever the identity: the identity is never what stops it."""
    import shutil
    folder = crew_without_identity
    shutil.rmtree(folder / '.git')
    before = snapshot(folder.parent.parent)
    for changes in ({}, dict(operator='Synthetic Operator')):
        with pytest.raises(ship.ShipError, match='is not its own git repository') as caught:
            rerun(folder.parent.parent, **changes)
        assert 'Git needs your name' not in str(caught.value)
    assert snapshot(folder.parent.parent) == before


@pytest.mark.parametrize('repository', [True, False])
@pytest.mark.parametrize('effective', [True, False])
def test_commits_is_the_rule_set_values_commits_by(crew_without_identity, repository, effective):
    """ship.commits answers, without writing, what set_values then does: a commit only for an edit that changes the
    ship file of a ship that is its own repository."""
    import shutil
    folder = crew_without_identity
    project = folder.parent.parent
    ship._git(project, 'config', '--global', 'user.name', 'Synthetic Global')
    ship._git(project, 'config', '--global', 'user.email', 'global@example.invalid')
    if not repository:
        shutil.rmtree(folder / '.git')
    current = ship.load(folder)
    edits = [('ship', 'operator', 'Synthetic Operator' if effective else current.operator)]
    before = (folder / 'ship.toml').read_bytes()
    predicted = ship.commits(current, edits)
    assert (folder / 'ship.toml').read_bytes() == before, 'asking writes nothing'
    [proof] = ship.set_values(current, edits)
    assert predicted is (' · committed ' in proof) is (repository and effective), proof
