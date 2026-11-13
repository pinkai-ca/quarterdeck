"""Shell startup files may put an operator's qd ahead of the seat's command folder."""

import re
import shlex
import subprocess

import pytest

from quarterdeck import seat, standing
from quarterdeck.ship import load


@pytest.mark.parametrize('mode', ['-lc', '-ic'])
@pytest.mark.parametrize('seat_name', ['captain', 'desk'])
def test_shell_startup_keeps_the_briefs_qd(drydock, tmp_path, monkeypatch, mode, seat_name):
    folder = drydock.rename(drydock.with_name("ship with spaces and 'quote'"))
    ship = load(folder)
    installed = ship.runtime / 'venv/bin'
    installed.mkdir(parents=True)
    own = installed / 'qd'
    own.write_text('#!/bin/sh\nprintf "ship qd %s\\n" "$*"\n')
    own.chmod(0o700)
    home = tmp_path / 'synthetic-home'
    other = home / 'operator/bin'
    other.mkdir(parents=True)
    wrong = other / 'qd'
    wrong.write_text('#!/bin/sh\nprintf "wrong qd\\n"\n')
    wrong.chmod(0o700)
    for name in ('.profile', '.bashrc'):
        (home / name).write_text(f'export PATH={shlex.quote(str(other))}:"$PATH"\n')
    monkeypatch.setenv('PATH', '/usr/bin:/bin')
    prepared = seat.prepare_program_path(ship, bin_dir=installed)
    spec = ship.seat(seat_name)
    brief = standing.brief(ship, spec).text
    command = re.search(r'Your qd command is `([^`]+)`', brief).group(1)
    assert shlex.split(command) == [str(ship.runtime / 'bin/qd')]
    for example in ('post report', 'post ack', 'post order', 'post question', 'post answer', 'watch', 'status', 'brief'):
        assert f'`{command} {example} ' in brief or f'`{command} {example}`' in brief
    if seat_name == 'desk':
        assert f'`{command} inbox --wait`' in brief
    env = {'HOME': str(home), 'PATH': prepared, 'QD_SHIP': str(ship.folder), 'QD_SEAT': seat_name}
    script = f'command -v qd\nqd --version\ncommand -v {command}\n{command} --version'
    result = subprocess.run(['/bin/bash', mode, script],
                            env=env, cwd=home, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [str(wrong), 'wrong qd', str(ship.runtime / 'bin/qd'), 'ship qd --version']
