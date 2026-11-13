"""A normal launched process finds qd through the ship's command folder."""

from pathlib import Path
import os
import shlex
import time

from quarterdeck import seat as seatmod
from quarterdeck.ship import load


def test_a_launched_process_uses_the_copy_for_qd_and_machine_programs(drydock, tmux, tmp_path, monkeypatch):
    ship = load(drydock)
    installed = ship.runtime / 'venv/bin'
    machine = tmp_path / 'machine/bin'
    for folder, names in ((installed, ('qd', 'python', 'python3', 'pip', 'pip3')),
                          (machine, ('python', 'python3', 'pip', 'pip3'))):
        folder.mkdir(parents=True, exist_ok=True)
        for name in names:
            program = folder / name
            program.write_text('#!/bin/sh\nprintf "synthetic command\\n"\n')
            program.chmod(0o700)
    monkeypatch.setattr(seatmod, 'qd_bin', lambda: installed)
    monkeypatch.setenv('PATH', os.pathsep.join([str(installed), str(machine), '/usr/bin', '/bin']))
    seat = seatmod.Seat(ship, ship.seats['captain'], tmux)
    seat.folder.mkdir(parents=True)
    output = tmp_path / 'resolved-programs'
    script = 'for program in qd python python3 pip pip3; do command -v "$program"; done; qd'
    seat._open_window(['/bin/sh', '-c', f'({script}) > {shlex.quote(str(output))}'], {}, seat._path())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if output.is_file() and output.read_text().endswith('synthetic command\n'):
            break
        time.sleep(0.01)
    assert output.read_text().splitlines() == [str(ship.runtime / 'bin/qd'),
                                             *[str(machine / name) for name in ('python', 'python3', 'pip', 'pip3')],
                                             'synthetic command']
    assert (ship.runtime / 'bin/qd').resolve() == installed / 'qd'
