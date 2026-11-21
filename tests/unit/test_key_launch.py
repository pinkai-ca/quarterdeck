"""The launcher: a key reaches its program with no command line holding it, and a sign-in leaves nothing behind."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys

import pytest

from quarterdeck import key_launch, keyfile, seat_files
from quarterdeck.harness.key_routes import ROUTES
from quarterdeck.ship import Pool

KEY = 'sk-synthetic-Q7wM2xC9zT4lP8rV3nH6'
TAIL = KEY[-8:]
SOURCE = str(Path(keyfile.__file__).parents[1])


def script(path: Path, body: str) -> Path:
    path.write_text('#!/bin/sh\n' + body)
    path.chmod(0o755)
    return path


def launcher(args, **options):
    code = f'import sys; sys.path.insert(0, {SOURCE!r}); from quarterdeck.key_launch import main; sys.exit(main(sys.argv[1:]))'
    return subprocess.run([sys.executable, '-I', '-c', code, *args], capture_output=True, text=True, timeout=60, **options)


def test_the_launch_words_hold_the_keys_name_and_never_the_key(key_file, tmp_path):
    key_file.write(anthropic=KEY)
    pool = Pool('paid', 'anthropic', api_key='keys:anthropic', ship_folder=tmp_path)
    for name, route in ROUTES.items():
        words = key_launch.command(key_file.path, pool, route, 'program', ['program', '--flag'], home=tmp_path,
                                   private=tmp_path, jailed=bool(route.variable))
        assert KEY not in ' '.join(words) and TAIL not in ' '.join(words), name
        assert words[:4] == [sys.executable, '-I', '-m', key_launch.MODULE]
        assert key_launch.inner(words) == ['program', '--flag']


def test_inner_leaves_an_ordinary_launch_as_it_is():
    assert key_launch.inner(['program', '-I', '-m', 'x', '--', 'y']) == ['program', '-I', '-m', 'x', '--', 'y']


def test_an_unjailed_program_gets_the_one_variable_and_no_argument_holds_it(key_file, tmp_path):
    key_file.write(anthropic=KEY)
    out = tmp_path / 'seen'
    program = script(tmp_path / 'program', f'printf "%s\\n" "$QD_PROGRAM_KEY" > {out}\nprintf "%s\\n" "$*" >> {out}\n')
    done = launcher(['--file', str(key_file.path), '--name', 'anthropic', '--variable', 'QD_PROGRAM_KEY', '--',
                     str(program), 'one'])
    assert done.returncode == 0 and done.stdout == '' and done.stderr == ''
    assert out.read_text().splitlines() == [KEY, 'one']


@pytest.mark.parametrize('state', ['missing-name', 'missing-file', 'not-toml'])
def test_a_key_it_cannot_read_stops_the_launch_in_one_line(key_file, tmp_path, state):
    if state != 'missing-file':
        key_file.write(**({'other': KEY} if state == 'missing-name' else {'anthropic': KEY}))
    if state == 'not-toml':
        key_file.path.write_text(f'anthropic = "{KEY}\n')
    ran = tmp_path / 'ran'
    program = script(tmp_path / 'program', f': > {ran}\n')
    done = launcher(['--file', str(key_file.path), '--name', 'anthropic', '--variable', 'V', '--', str(program)])
    assert done.returncode == key_launch.EXIT_REFUSED and not ran.exists()
    assert len(done.stderr.splitlines()) == 1 and KEY not in done.stderr and TAIL not in done.stderr


def test_the_jail_words_put_the_key_in_a_pipe_just_after_clearenv():
    words = key_launch.jail_words(['bwrap', '--ro-bind', '/', '/', '--clearenv', '--setenv', 'A', 'B', '--', 'x'], 'V', KEY)
    assert KEY not in words and words[4:7] == ['--clearenv', '--args', words[6]]
    descriptor = int(words[6])
    try:
        assert os.get_inheritable(descriptor)
        assert os.read(descriptor, 4096) == b'--setenv\0V\0' + KEY.encode() + b'\0'
        assert os.read(descriptor, 1) == b'', 'its write end is closed, so bwrap reads to its end'
    finally:
        os.close(descriptor)


def test_the_jail_words_need_a_jail():
    with pytest.raises(ValueError):
        key_launch.jail_words(['program'], 'V', KEY)


# -- a program's own key login -----------------------------------------------------------------------------------------

@pytest.fixture
def login(tmp_path, key_file):
    key_file.write(openai=KEY)
    home = tmp_path / 'runtime' / 'seats' / 'w' / 'home'
    home.mkdir(parents=True, mode=0o700)
    private = tmp_path / 'trusted'
    private.mkdir(mode=0o700)
    ran = tmp_path / 'ran'
    program = script(ran.with_name('run-program'), f': > {ran}\n')

    def run(body):
        signin = script(tmp_path / 'signin', body)
        route = ROUTES['codex']
        return launcher(['--file', str(key_file.path), '--name', 'openai', '--login-program', str(signin),
                         '--login-home', route.login_home, '--login-file', route.login_file, '--home', str(home),
                         '--private', str(private), *[f'--login-word={word}' for word in route.login], '--', str(program)])
    return type('Login', (), dict(run=staticmethod(run), home=home, private=private, ran=ran))


GOOD = 'read key; printf \'{"auth_mode":"apikey","OPENAI_API_KEY":"%s"}\' "$key" > "$CODEX_HOME/auth.json"\n'


def test_a_sign_in_takes_the_key_on_stdin_and_its_file_lands_private(login):
    done = login.run(GOOD)
    assert done.returncode == 0 and login.ran.exists() and KEY not in done.stdout + done.stderr
    written = login.home / 'auth.json'
    assert json.loads(written.read_text())['OPENAI_API_KEY'] == KEY
    assert stat.S_IMODE(written.stat().st_mode) == 0o600 and not written.is_symlink()
    assert list(login.private.iterdir()) == [], 'the private folder is gone'


def test_a_link_planted_at_the_sign_in_file_is_replaced_and_never_followed(login, tmp_path):
    target = tmp_path / 'operator-file'
    target.write_text('untouched')
    (login.home / 'auth.json').symlink_to(target)
    assert login.run(GOOD).returncode == 0
    assert target.read_text() == 'untouched' and not (login.home / 'auth.json').is_symlink()


@pytest.mark.parametrize('body', [f'echo "bad key {KEY} {TAIL}"\necho "$PPID {TAIL}" >&2\nexit 3\n',
                                  'read key; exit 0\n',
                                  'read key; ln -s /etc/passwd "$CODEX_HOME/auth.json"\n'],
                         ids=['refused', 'wrote-nothing', 'wrote-a-link'])
def test_a_failed_sign_in_starts_nothing_prints_nothing_of_the_key_and_leaves_no_folder(login, body):
    done = login.run(body)
    assert done.returncode == key_launch.EXIT_REFUSED and not login.ran.exists()
    assert KEY not in done.stdout + done.stderr and TAIL not in done.stdout + done.stderr
    assert not (login.home / 'auth.json').exists() and list(login.private.iterdir()) == []


def test_a_program_that_cannot_start_leaves_no_folder(login, tmp_path, key_file):
    route = ROUTES['codex']
    done = launcher(['--file', str(key_file.path), '--name', 'openai', '--login-program', str(tmp_path / 'absent'),
                     '--login-home', route.login_home, '--login-file', route.login_file, '--home', str(login.home),
                     '--private', str(login.private), '--login-word=login', '--', 'true'])
    assert done.returncode == key_launch.EXIT_REFUSED and list(login.private.iterdir()) == []


def test_a_folder_left_by_a_killed_launch_is_removed_by_the_next(login):
    left = login.private / f'{key_launch.LOGIN_PREFIX}left'
    left.mkdir()
    (left / 'auth.json').write_text('left behind')
    assert login.run(GOOD).returncode == 0
    assert list(login.private.iterdir()) == []


@pytest.mark.parametrize('signum', [signal.SIGTERM, signal.SIGHUP])
def test_a_signal_during_the_sign_in_removes_the_folder(login, tmp_path, key_file, signum):
    started, hold = tmp_path / 'started', tmp_path / 'hold'
    os.mkfifo(hold)
    signin = script(tmp_path / 'signin', f': > {started}\nexec cat {hold}\n')
    route = ROUTES['codex']
    code = f'import sys; sys.path.insert(0, {SOURCE!r}); from quarterdeck.key_launch import main; sys.exit(main(sys.argv[1:]))'
    process = subprocess.Popen([sys.executable, '-I', '-c', code, '--file', str(key_file.path), '--name', 'openai',
                                '--login-program', str(signin), '--login-home', route.login_home,
                                '--login-file', route.login_file, '--home', str(login.home), '--private', str(login.private),
                                '--login-word=login', '--', 'true'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        watcher = subprocess.run(['sh', '-c', f'until [ -e {started} ]; do :; done'], timeout=30)
        assert watcher.returncode == 0
        assert len(list(login.private.iterdir())) == 1
        process.send_signal(signum)
        process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    assert process.returncode == 128 + signum and list(login.private.iterdir()) == []
