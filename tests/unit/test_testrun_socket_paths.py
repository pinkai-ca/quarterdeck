"""Socket path admission uses the same byte budget as the suite's fixture."""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import conftest
from tests.testrunner import cli, run, socket_paths
from tests.testrunner.layout import ROOT_NAME, RUN_SOCKETS, Refused


def test_shared_budget_matches_the_fixture_and_deepest_native_paths():
    anchor = Path('/synthetic/.testruns/r')
    sample = anchor / (conftest.SESSION_PREFIX + '12345678')
    generated = sample / 'qs-12345678/qd-pt-12345678/s-replacement'
    probes = [sample / f'tmux-{os.getuid()}/qd-test-9999999999-abcdef-replacement',
              sample / f'tmux-{os.getuid()}/qd-synthetic-attribution-no-server']
    assert socket_paths.lengths(anchor) == (
        len(os.fsencode(generated)), max(len(os.fsencode(path)) for path in probes))
    assert conftest.SOCKET_LIMIT == socket_paths.SOCKET_LIMIT == 107
    assert conftest.SOCKET_MARGIN == socket_paths.SOCKET_MARGIN == 12


@pytest.mark.parametrize('uid', [0, 1000, 4294967295])
def test_project_limit_is_the_last_byte_that_fits_every_endpoint(uid, monkeypatch):
    monkeypatch.setattr(socket_paths.os, 'getuid', lambda: uid)
    limit = socket_paths.project_limit()
    project = Path('/' + 'p' * (limit - 1))
    socket_paths.check(project / ROOT_NAME / RUN_SOCKETS)
    with pytest.raises(Refused, match=f'longest checkout/project path is {limit} bytes'):
        socket_paths.check(Path(str(project) + 'p') / ROOT_NAME / RUN_SOCKETS)


def test_non_ascii_path_is_measured_in_filesystem_bytes():
    limit = socket_paths.project_limit()
    ascii_project = Path('/' + 'p' * (limit - 1))
    socket_paths.check(ascii_project / ROOT_NAME / RUN_SOCKETS)
    same_characters = Path('/é' + 'p' * (limit - 2))
    assert len(str(same_characters)) == len(str(ascii_project))
    with pytest.raises(Refused, match='Unix socket path limit is 107 bytes'):
        socket_paths.check(same_characters / ROOT_NAME / RUN_SOCKETS)


def declare_suite(top):
    helper = top / 'tests/testrunner/socket_paths.py'
    helper.parent.mkdir(parents=True)
    helper.write_text('raise AssertionError("admission must not import target code")\n')


def test_other_projects_without_this_socket_fixture_keep_their_existing_admission(tmp_path):
    socket_paths.preflight(tmp_path, tmp_path / ('long' * 40))


def test_linked_worktree_uses_the_common_project_not_its_long_top(tmp_path):
    declare_suite(tmp_path)
    socket_paths.preflight(tmp_path, Path('/p'))


@pytest.mark.parametrize('lane', ['--fast', '--whole', 'tests/test_example.py'])
def test_long_project_refused_before_any_slot_setup_or_test(lane, tmp_path, monkeypatch, capsys):
    declare_suite(tmp_path)
    (tmp_path / 'tests/unit').mkdir()
    target = SimpleNamespace(top=tmp_path, project=tmp_path, branch='change', main_checkout=True)
    monkeypatch.setattr(run.gitops, 'resolve', lambda path: target)
    monkeypatch.setattr(run.gitops, 'dirty', lambda path: [])
    monkeypatch.setattr(run, 'check_target', lambda top, item: item)

    def no_setup(*args, **kwargs):
        pytest.fail('long path must be refused before slots, storage or tests')

    monkeypatch.setattr(run, 'Root', no_setup)
    monkeypatch.setattr(run, 'Slot', no_setup)
    monkeypatch.setattr(run.storage, 'prepare', no_setup)
    monkeypatch.setattr(run.Runner, 'execute', no_setup)
    assert cli.main(['run', lane], []) == 2
    output = capsys.readouterr()
    assert 'refused:' in output.err
    assert 'native Unix socket path limit is 107 bytes' in output.err
    assert f'longest checkout/project path is {socket_paths.project_limit()} bytes' in output.err
    assert str(tmp_path / ROOT_NAME / RUN_SOCKETS) in output.err
    assert not (tmp_path / ROOT_NAME).exists()


def test_normal_project_reaches_the_unchanged_slot_setup(tmp_path, monkeypatch):
    declare_suite(tmp_path)
    target = SimpleNamespace(top=tmp_path, project=Path('/p'), branch='change', main_checkout=False)
    monkeypatch.setattr(run.gitops, 'resolve', lambda path: target)
    monkeypatch.setattr(run.gitops, 'dirty', lambda path: [])
    monkeypatch.setattr(run, 'check_target', lambda top, item: item)
    reached = []

    def root(project):
        reached.append(project)
        raise RuntimeError('ordinary setup reached')

    monkeypatch.setattr(run, 'Root', root)
    with pytest.raises(RuntimeError, match='ordinary setup reached'):
        run.Runner(cli.parser().parse_args(['run']), []).main()
    assert reached == [Path('/p')]
