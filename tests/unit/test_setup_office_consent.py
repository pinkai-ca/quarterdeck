"""Office-only consent uses fake operations and private temporary declarations."""
import json
import os
from types import SimpleNamespace

import pytest

from quarterdeck import cli, setup, standing


@pytest.fixture
def office(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    resources = tmp_path / 'resources'
    template = resources / 'ships' / 'office'
    (template / 'roles').mkdir(parents=True)
    (template / 'ship.toml').write_text('''[ship]
name = "office"
kind = "office"
operator = "Synthetic operator"
code = "checkout"
[pools.synthetic]
provider = "synthetic"
[sessions.office]
title = "Office"
human = true
[seats.desk]
session = "office"
role = "desk"
harness = "fake"
pool = "synthetic"
model = "fake-1"
''')
    for name in ('orders.md', 'README.md', 'roles/desk.md'):
        (template / name).write_text('Synthetic office.\n')
    monkeypatch.setattr(standing, 'project_dir', lambda: resources)
    monkeypatch.setattr(setup.Operations, 'project_folder', lambda _: project)
    launched = []
    monkeypatch.setattr(setup.Operations, 'up_desk', lambda _, ship: launched.append(ship) or 'synthetic desk up')
    monkeypatch.setattr(setup.Operations, 'access', lambda _, ship: 'synthetic desk access')
    for key in ('QD_SHIP', 'QD_SEAT', 'GIT_DIR', 'GIT_WORK_TREE'):
        monkeypatch.delenv(key, raising=False)
    for key, value in {'GIT_AUTHOR_NAME': 'Synthetic Example', 'GIT_AUTHOR_EMAIL': 'example@invalid.test',
                       'GIT_COMMITTER_NAME': 'Synthetic Example', 'GIT_COMMITTER_EMAIL': 'example@invalid.test',
                       'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}.items():
        monkeypatch.setenv(key, value)
    return SimpleNamespace(project=project, folder=project/'office', launched=launched, tmp=tmp_path)


@pytest.mark.parametrize('answers', [{}, {'start': 'no'}, {'start': 'yes'}])
def test_missing_accept_prints_proposal_and_exits_two_without_writes(office, capsys, answers):
    path = office.tmp / 'answers.json'
    path.write_text(json.dumps(answers))
    assert cli.main(['setup', '--answers', str(path)]) == 2
    captured = capsys.readouterr()
    assert 'Office proposal (not written):' in captured.out
    assert str(office.folder) in captured.out
    assert 'Desk' in captured.out and 'fake-1' in captured.out
    assert 'missing answer accept' in captured.err
    assert list(office.project.iterdir()) == []
    assert office.launched == []


def test_nonterminal_without_an_answers_file_also_requires_accept(office, monkeypatch, capsys):
    monkeypatch.setattr(setup.sys.stdin, 'isatty', lambda: False)
    assert cli.main(['setup']) == 2
    captured = capsys.readouterr()
    assert 'Office proposal (not written):' in captured.out
    assert 'missing answer accept' in captured.err
    assert list(office.project.iterdir()) == [] and office.launched == []


@pytest.mark.parametrize('extra', [[], ['--start', 'no'], ['--start', 'yes']])
def test_declining_accept_never_needs_start_or_writes(office, capsys, extra):
    assert cli.main(['setup', '--accept', 'no', *extra]) == 0
    assert 'no office changes were written' in capsys.readouterr().out
    assert list(office.project.iterdir()) == [] and office.launched == []


def test_accept_yes_without_start_exits_two_before_writing(office, capsys):
    assert cli.main(['setup', '--accept', 'yes']) == 2
    captured = capsys.readouterr()
    assert 'Office proposal (not written):' in captured.out
    assert 'missing answer start' in captured.err
    assert list(office.project.iterdir()) == [] and office.launched == []


@pytest.mark.parametrize('start', ['no', 'yes'])
def test_only_explicit_accept_and_start_can_create_and_optionally_launch(office, capsys, start):
    assert cli.main(['setup', '--accept', 'yes', '--start', start]) == 0
    captured = capsys.readouterr()
    assert 'Office proposal (not written):' in captured.out
    assert office.folder.joinpath('ship.toml').is_file()
    assert office.folder.joinpath('.git').is_dir()
    assert len(office.launched) == (start == 'yes')
    if start == 'no':
        assert 'desk not started' in captured.out
    else:
        assert office.launched[0].seat('desk').harness == 'fake'
        assert 'synthetic desk up' in captured.out


@pytest.mark.parametrize('start,created', [('no', True), ('', False)])
def test_interactive_start_has_no_implicit_yes(office, start, created):
    answers = iter(['no', 'yes', start])  # office only, accept explicitly, then start
    prompts, lines = [], []
    def read(prompt):
        prompts.append(prompt)
        return next(answers)
    conversation = setup.Conversation({}, interactive=True, read=read, write=lines.append)
    args = cli.build_parser().parse_args(['setup'])
    if created:
        setup.first_step(args, conversation=conversation)
        assert office.folder.is_dir()
    else:
        with pytest.raises(setup.ShipError, match='missing answer start'):
            setup.first_step(args, conversation=conversation)
        assert list(office.project.iterdir()) == []
    assert 'Start the office' in prompts[-1] and '[yes]' not in prompts[-1]
    assert any('Office proposal' in line for line in lines)
    assert office.launched == []
