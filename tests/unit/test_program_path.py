"""The framework command folder and ordinary program lookup, using synthetic executables."""

from pathlib import Path
import os
import shutil
import subprocess
from types import SimpleNamespace

from quarterdeck import install, seat


def executable(folder: Path, name: str, text: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(f'#!/bin/sh\nprintf "%s\\n" "{text}"\n')
    path.chmod(0o700)
    return path


def test_program_lookup_keeps_the_machine_path_without_the_framework_virtualenv(tmp_path, monkeypatch):
    prefix = tmp_path / 'framework'
    machine = tmp_path / 'machine/bin'
    monkeypatch.setattr(seat.sys, 'prefix', str(prefix))
    monkeypatch.setattr(seat.sys, 'base_prefix', '/usr')
    monkeypatch.setenv('PATH', os.pathsep.join([str(prefix / 'bin'), str(machine), '/usr/bin']))
    assert seat.program_path().split(os.pathsep) == [str(machine), '/usr/bin']


def test_framework_commands_run_the_copy_and_ordinary_tools_run_the_machine(tmp_path, monkeypatch):
    ship = SimpleNamespace(runtime=tmp_path / 'ship/runtime')
    installed = ship.runtime / 'venv/bin'
    machine = tmp_path / 'machine/bin'
    qd = executable(installed, 'qd', 'synthetic installed qd')
    for name in ('python', 'python3', 'pip', 'pip3'):
        executable(installed, name, 'synthetic framework interpreter')
        executable(machine, name, f'synthetic machine {name}')
    monkeypatch.setenv('PATH', os.pathsep.join([str(installed), str(machine), '/usr/bin']))
    path = seat.prepare_program_path(ship, bin_dir=installed)
    assert Path(shutil.which('qd', path=path)).resolve() == qd
    assert subprocess.check_output(['qd'], env={'PATH': path}, text=True) == 'synthetic installed qd\n'
    for name in ('python', 'python3', 'pip', 'pip3'):
        assert shutil.which(name, path=path) == str(machine / name)
    assert {p.name for p in (ship.runtime / 'bin').iterdir()} == {'qd'}
    before = (ship.runtime / 'bin/qd').lstat()
    assert seat.prepare_program_path(ship, bin_dir=installed) == path
    assert (ship.runtime / 'bin/qd').lstat().st_ino == before.st_ino
    monkeypatch.setenv('PATH', path)
    assert seat.prepare_program_path(ship, bin_dir=installed) == path


def test_synthetic_launches_also_get_the_fake_command(tmp_path, monkeypatch):
    ship = SimpleNamespace(runtime=tmp_path / 'ship/runtime')
    installed = ship.runtime / 'venv/bin'
    executable(installed, 'qd', 'synthetic installed qd')
    fake = executable(installed, 'qd-fakeharness', 'synthetic fake harness')
    path = seat.prepare_program_path(ship, bin_dir=installed, synthetic=True)
    assert Path(shutil.which('qd-fakeharness', path=path)).resolve() == fake
    assert {p.name for p in (ship.runtime / 'bin').iterdir()} == {'qd', 'qd-fakeharness'}


def test_activation_prepares_the_command_folder_before_service_start(tmp_path, monkeypatch):
    ship = SimpleNamespace(runtime=tmp_path / 'ship/runtime', folder=tmp_path / 'ship', trusted=tmp_path / 'ship')
    qd = executable(ship.runtime / 'venv/bin', 'qd', 'synthetic installed qd')
    monkeypatch.setattr(install, '_phase', lambda ship, data, phase: data['pending'].update(phase=phase))
    monkeypatch.setattr(install, '_cleanup', lambda ship, data: None)
    monkeypatch.setattr(install, 'queue_rebrief', lambda *args: (None, None))
    monkeypatch.setattr(install, 'rebrief_result', lambda *args: 'synthetic rebrief complete')

    def started(ship, **kwargs):
        assert (ship.runtime / 'bin/qd').resolve() == qd
        return 'synthetic service started'

    monkeypatch.setattr(install.servicectl, 'start', started)
    data = {'pending': {'old_current': None, 'snapshot': {}, 'was_running': True, 'token': 'synthetic-start'}}
    fact = {'commit': 'a' * 40}
    proofs = install._activate(ship, data, fact, None)
    assert proofs == ['synthetic service started', 'synthetic rebrief complete']
    assert data['pending']['activated']
