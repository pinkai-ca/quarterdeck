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
