"""Ordinary lifecycle cleanup of roots made by a synthetic project that has ended."""
import json

from tests.testrunner import cli, socket_roots


def test_a_gone_synthetic_project_leaves_no_socket_root(tmp_path, monkeypatch):
    monkeypatch.setattr(socket_roots, 'SYSTEM_TMP', tmp_path)
    project = tmp_path / 'synthetic-project'
    project.mkdir()
    first = socket_roots.ensure(project, 'r')
    second = socket_roots.ensure(project, 's')
    where = first.parent
    assert json.loads((where / socket_roots.PROJECT).read_text())['project'] == str(project)
    project.rmdir()
    assert socket_roots.sweep([where]) == {'removed': [str(where)], 'absent': [], 'kept': []}
    assert not first.exists() and not second.exists() and not where.exists()
    assert socket_roots.sweep([where]) == {'removed': [], 'absent': [str(where)], 'kept': []}


def test_explicit_cleanup_command_records_what_went(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(socket_roots, 'SYSTEM_TMP', tmp_path)
    project = tmp_path / 'ended-project'
    project.mkdir()
    where = socket_roots.ensure(project, 'r').parent
    project.rmdir()
    assert cli.main(['clean-sockets', '--root', str(where), '--expect-project', str(project)], []) == 0
    found = json.loads(capsys.readouterr().out)
    assert found == {'removed': [str(where)], 'absent': [], 'kept': []}
    assert not where.exists()
