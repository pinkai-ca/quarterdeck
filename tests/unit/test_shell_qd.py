"""Shell startup files may put an operator's qd ahead of the seat's command folder."""

import re
import shlex
import subprocess

import pytest

from quarterdeck import seat, standing
from quarterdeck.ship import load


@pytest.fixture
def shell_home(drydock, tmp_path, monkeypatch):
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
    return ship, home, wrong, prepared


@pytest.mark.parametrize('mode', ['-lc', '-ic'])
@pytest.mark.parametrize('seat_name', ['captain', 'desk'])
def test_shell_startup_keeps_the_briefs_qd(shell_home, mode, seat_name):
    ship, home, wrong, prepared = shell_home
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


@pytest.mark.parametrize('mode', ['-lc', '-ic'])
@pytest.mark.parametrize('rc_function', [False, True])
def test_launch_exports_qd_through_shell_startup_and_child_shells(shell_home, tmp_path, mode, rc_function):
    ship, home, wrong, prepared = shell_home
    if rc_function:
        for name in ('.profile', '.bashrc'):
            with (home / name).open('a') as stream:
                stream.write('qd() { printf "rc qd\\n"; }\n')
    spec = ship.seat('captain')
    launched = seat.Seat(ship, spec, None)
    launched._launch_tmpdir = tmp_path / 'private-tmux'
    brief = standing.brief(ship, spec).text
    absolute = re.search(r'Your qd command is `([^`]+)`', brief).group(1)
    script = (f'command -v qd\nqd --version\ncommand -v {absolute}\n{absolute} --version\n'
              f'/bin/bash {mode} {shlex.quote("qd --version")}')
    words = launched._launch_words(['/bin/bash', mode, script], {}, prepared)
    launch_file = tmp_path / 'launch.sh'
    launch_file.write_text(seat.launch_script(spec.name, words))
    env = {'HOME': str(home), 'PATH': prepared, 'QD_SHIP': str(ship.folder), 'QD_SEAT': spec.name}
    result = subprocess.run(['/bin/sh', str(launch_file)], env=env, cwd=home,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    bare = 'rc qd' if rc_function else 'ship qd --version'
    assert result.stdout.splitlines() == ['qd', bare, str(ship.runtime / 'bin/qd'), 'ship qd --version', bare]
