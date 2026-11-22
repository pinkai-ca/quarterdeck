"""Nightly checks rewrite copied workspace declarations without starting programs."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import daily_live, install
from quarterdeck.ship import ShipError, load


@pytest.mark.parametrize('workspace_kind', ['project', 'ship', 'relative'])
def test_every_copied_seat_works_inside_the_throwaway(tmp_path, monkeypatch, workspace_kind):
    project = tmp_path / 'project'
    project.mkdir()
    origin_folder = tmp_path / 'origin'
    origin_folder.mkdir()
    workspace = {'project': str(project), 'ship': str(origin_folder), 'relative': '../project'}[workspace_kind]
    declaration = f'''[ship]
name = "synthetic"
workspace = {json.dumps(workspace)}
code = "checkout"

[pools.synthetic]
provider = "synthetic"

[sessions.synthetic]

[seats.default]
session = "synthetic"
harness = "fake"
pool = "synthetic"

[seats.project]
session = "synthetic"
harness = "fake"
pool = "synthetic"
cwd = {json.dumps(str(project))}

[seats.ship]
session = "synthetic"
harness = "fake"
pool = "synthetic"
cwd = {json.dumps(str(origin_folder))}

[seats.relative]
session = "synthetic"
harness = "fake"
pool = "synthetic"
cwd = "../project"
'''
    (origin_folder / 'ship.toml').write_text(declaration)
    origin = load(origin_folder)
    scratch = tmp_path / 'scratch'
    seen = []
    workspaces = []

    def check_copy(folder):
        copied = load(Path(folder))
        assert copied.folder != origin.folder
        assert copied.workspace.is_dir()
        assert copied.workspace.is_relative_to(copied.folder.parent)
        assert copied.workspace == copied.folder.parent / 'workspace'
        assert not copied.workspace.is_relative_to(copied.folder)
        assert copied.workspace != copied.folder
        assert set(copied.seats) == set(origin.seats)
        for seat in copied.seats.values():
            cwd = copied.cwd_of(seat)
            assert cwd.is_dir() and cwd.is_relative_to(copied.folder.parent)
            assert not cwd.is_relative_to(copied.folder)
            assert cwd not in (project, origin.folder)
            assert seat.cwd == copied.workspace
        assert copied.workspace not in (project, origin.folder)
        assert copied.seat('default').harness == 'fake'
        seen.append(copied.folder)
        workspaces.append(copied.workspace)

    def private(parent):
        # Workspace preparation precedes even opening the private server.
        check_copy(parent / origin.folder.name)
        return SimpleNamespace(socket='/synthetic/private/socket', close=lambda: True,
                               new_session=lambda name: 'bootstrap', kill_session_id=lambda identity: None,
                               run=lambda *args: '80x24\n')

    def run(argv, **kwargs):
        assert argv[-3:] == ['check', 'fake', '--minimal']
        check_copy(argv[argv.index('--ship') + 1])
        assert kwargs['env']['QD_SHIP'] == str(seen[-1])
        return SimpleNamespace(returncode=0, stdout='synthetic check', stderr='')

    monkeypatch.setattr(daily_live, 'open_private', private)
    monkeypatch.setattr(daily_live.subprocess, 'run', run)
    result = daily_live.invoke(origin, 'fake', scratch, '/synthetic/qd')
    assert result.exit_code == 0 and result.retained is None
    assert len(seen) == 2 and seen[0] == seen[1]
    assert not seen[0].exists() and not Path(result.place).exists()
    assert not any(workspace.exists() for workspace in workspaces)
    assert origin.file.read_text() == declaration
    assert sorted(origin.folder.iterdir()) == [origin.file]
    assert list(project.iterdir()) == []


def test_default_disk_root_is_external_and_explicit_scratch_is_respected(tmp_path, monkeypatch):
    monkeypatch.setattr(daily_live, 'memory_root', lambda: None)
    monkeypatch.setattr(daily_live, 'SYSTEM_TMP', tmp_path / 'system-tmp')
    explicit = tmp_path / 'explicit'
    assert daily_live.ship_root(None) == daily_live.SYSTEM_TMP
    assert daily_live.ship_root(explicit) == explicit


def test_install_preparation_failure_removes_the_partial_throwaway(tmp_path, monkeypatch):
    folder = tmp_path / 'origin'
    folder.mkdir()
    (folder / 'ship.toml').write_text('[ship]\nname = "origin"\n')
    origin = load(folder)

    def fail(original, copied):
        assert original is origin
        staging = copied.runtime / 'venv'
        staging.mkdir(parents=True)
        (staging / 'partial').touch()
        raise ShipError('synthetic copy proof failure')

    monkeypatch.setattr(install, 'stage_disposable_copy', fail)
    monkeypatch.setattr(daily_live, 'open_private', lambda _: pytest.fail('started before copy was proved'))
    result = daily_live.invoke(origin, 'fake', tmp_path / 'scratch', '/unused/checkout-qd')

    assert result.exit_code is None
    assert result.output == 'check did not finish: synthetic copy proof failure'
    assert result.retained is None and result.socket is None
    assert not Path(result.place).exists()
    assert not origin.runtime.exists()
