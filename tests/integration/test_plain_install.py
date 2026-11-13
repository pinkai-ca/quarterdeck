"""A wheel's own laws and complete fake drill, with its build source gone."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

import pytest

from tests.integration.test_upgrade_resources import RESOURCE_ROOTS, ROOT, archive, environment, git
from tests.testrunner import layout


def command(*argv, cwd, env, timeout=180):
    done = subprocess.run([str(arg) for arg in argv], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=timeout)
    assert done.returncode == 0, (argv, done.returncode, done.stdout, done.stderr)
    return done.stdout


def child_environment(folder, socket_root):
    """Allow only ordinary OS settings, then bind every writable path to this run."""
    env = {name: os.environ[name] for name in ('PATH', 'LANG', 'LC_ALL', 'TERM') if name in os.environ}
    for name, relative in (
        ('HOME', 'home'), ('TMPDIR', 'tmp'), ('XDG_CONFIG_HOME', 'config'),
        ('XDG_DATA_HOME', 'data'), ('XDG_CACHE_HOME', 'cache'), ('XDG_STATE_HOME', 'state'),
        ('XDG_RUNTIME_DIR', 'runtime'), ('PIP_CACHE_DIR', 'pip-cache'),
    ):
        path = folder / relative
        path.mkdir(mode=0o700, parents=True)
        env[name] = str(path)
    env.update(PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1', PIP_NO_INDEX='1',
               PIP_NO_INPUT='1', PIP_DISABLE_PIP_VERSION_CHECK='1',
               GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               TMUX_TMPDIR=str(socket_root))
    return env


INSPECT = '''
import importlib.metadata, json, pathlib, site, sys
import quarterdeck
from quarterdeck import drill, standing
prefix = pathlib.Path(sys.prefix)
distribution = importlib.metadata.distribution('quarterdeck')
files = [str(path) for path in distribution.files]
print(json.dumps(dict(prefix=str(prefix), origin=quarterdeck.__file__,
    resources=str(standing.PROJECT_DIR), drydock=str(drill.DRYDOCK),
    sys_path=sys.path, user_site=site.ENABLE_USER_SITE,
    system_site=(prefix / 'pyvenv.cfg').read_text(), files=files)))
'''


INSIDE_PREFIX = '.plain-install-inside-'


@contextmanager
def inside_checkout():
    """A folder of this test's own under the checkout's ignored .testruns, as plain pytest's .testruns/t
    lies in CI's checkout; removed on every exit, failure included. A kill that skips the removal leaves
    it ignored, never untracked, and nothing else in the checkout is touched."""
    root = layout.Root(ROOT)  # makes .testruns, private and ignoring itself, where absent; refuses a link
    try:
        folder = Path(tempfile.mkdtemp(prefix=INSIDE_PREFIX, dir=root.path))
    finally:
        root.close()
    made = folder.lstat()
    try:
        yield folder
    finally:
        now = folder.lstat()
        assert (now.st_dev, now.st_ino) == (made.st_dev, made.st_ino) and folder.is_dir() \
            and not folder.is_symlink(), f'{folder} was replaced; left for inspection'
        shutil.rmtree(folder)
        assert not folder.exists()


@pytest.fixture(params=['run-temporary', 'inside-checkout'])
def scratch(request, tmp_path):
    """The run's own temporary folder (outside the checkout under tests/testrun.py, inside it under plain
    pytest), and a folder inside the checkout in every runner, so both layouts are proven by any run."""
    if request.param == 'run-temporary':
        yield tmp_path
        return
    with inside_checkout() as folder:
        assert folder.is_relative_to(ROOT) and folder.parent == ROOT / layout.ROOT_NAME
        yield folder


def test_wheel_installed_laws_and_fake_drydock(scratch, private_socket_root, capsys):
    # HEAD is the tested target, including future fixes, never a pinned historical product.
    ref = git(ROOT, 'rev-parse', 'HEAD')
    source = scratch / 'source'
    archive(ref, source, RESOURCE_ROOTS)
    source_files = {path.relative_to(source).as_posix(): path.read_bytes()
                    for path in source.rglob('*') if path.is_file()}
    tracked = git(ROOT, 'ls-tree', '-r', '--name-only', ref).splitlines()
    outside = scratch / 'outside'
    outside.mkdir()
    env = child_environment(scratch / 'child', private_socket_root)

    # Only the build environment can see the test tool's read-only build tools.
    build_python = environment(scratch / 'build-env')
    output = scratch / 'wheels'
    output.mkdir()
    command(build_python, '-I', '-c',
            'from setuptools.build_meta import build_wheel; import sys; build_wheel(sys.argv[1])',
            output, cwd=source, env=env)
    wheel, = output.glob('*.whl')
    with zipfile.ZipFile(wheel) as built:
        wheel_files = {name: built.read(name) for name in built.namelist() if not name.endswith('/')}

    runtime = scratch / 'installed'
    command(sys.executable, '-I', '-m', 'venv', runtime, cwd=outside, env=env)
    python, qd = runtime / 'bin/python', runtime / 'bin/qd'
    command(python, '-I', '-m', 'pip', 'install', '--no-index', '--no-deps', wheel,
            cwd=outside, env=env)
    # Neither the archive nor the build environment can rescue installed commands.
    shutil.rmtree(source)
    shutil.rmtree(scratch / 'build-env')
    assert not source.exists()
    env['PATH'] = os.pathsep.join((str(runtime / 'bin'), '/usr/bin', '/bin'))
    reading = json.loads(command(python, '-I', '-c', INSPECT, cwd=outside, env=env))
    prefix = Path(reading['prefix'])
    assert prefix == runtime and Path(reading['origin']).is_relative_to(runtime)
    share = runtime / 'share/quarterdeck'
    assert Path(reading['resources']) == share
    assert Path(reading['drydock']) == share / 'ships/drydock'
    assert reading['user_site'] is False
    assert 'include-system-site-packages = false' in reading['system_site']
    # Nothing is imported from the checkout or the deleted archive. The installed prefix is the one place
    # allowed, wherever the run's scratch lies: plain pytest in CI puts it inside the checkout.
    entries = [Path(path) for path in reading['sys_path'] if path]
    assert any(entry.is_relative_to(runtime) for entry in entries), entries
    assert not any(entry.is_relative_to(source) for entry in entries), entries
    assert not any(entry.is_relative_to(ROOT) and not entry.is_relative_to(runtime)
                   for entry in entries), entries
    assert not list(runtime.rglob('*.egg-link'))
    assert not any('editable' in path.lower() for path in reading['files'])

    # Measure installed bytes and distribution entries, rather than trusting data-files alone.
    installed_resources = {path.relative_to(share).as_posix(): path.read_bytes()
                           for path in share.rglob('*') if path.is_file()}
    expected_resources = {name: data for name, data in source_files.items()
                          if any(name.startswith(root + '/') for root in RESOURCE_ROOTS)}
    assert installed_resources == expected_resources
    package = Path(reading['origin']).parent
    expected_code = {name.removeprefix('src/quarterdeck/'): data
                     for name, data in source_files.items() if name.startswith('src/quarterdeck/')}
    assert {name: (package / name).read_bytes() for name in expected_code} == expected_code
    installed_names = set(reading['files'])
    assert all('quarterdeck/' + name in installed_names for name in expected_code)
    assert all('../../../share/quarterdeck/' + name in installed_names for name in installed_resources)
    represented = set(expected_resources) | {'src/quarterdeck/' + name for name in expected_code}
    omitted = sorted(set(tracked) - represented)
    assert 'tests/testrun.py' in omitted and 'README.md' in omitted
    assert 'docs/DESIGN.md' in omitted
    assert not any(name.startswith('ships/default/') for name in omitted)
    assert not any(name.startswith(('tests/', 'docs/', 'DESIGN')) for name in installed_names)

    laws_text = command(qd, 'laws', cwd=outside, env=env)
    laws = json.loads(command(qd, '--json', 'laws', cwd=outside, env=env))
    expected_laws = {line.split('`')[1] for line in expected_resources['standing-orders/laws.md']
                     .decode().splitlines() if line.startswith('- `')}
    assert {law['id'] for law in laws['laws']} == expected_laws
    assert not laws['strays']
    assert any(law['carriers'] for law in laws['laws'])
    assert all(carrier['path'].startswith('quarterdeck/')
               for law in laws['laws'] for carrier in law['carriers'])
    assert all(identity in laws_text for identity in expected_laws)

    drill_text = command(qd, 'drill', cwd=outside, env=env, timeout=240)
    lines = drill_text.strip().splitlines()
    assert lines and lines[-1] == f'all passed of {len(lines) - 1} checks', drill_text
    assert all(line.startswith('PASS  ') for line in lines[:-1]), drill_text
    for evidence in ('round 1: every order closed · 6 of 6',
                     'round 2: every order closed · 6 of 6',
                     'caught submitted open attempt before record acceptance',
                     'in the record 1 time(s); submissions 1;',
                     "desk never typed into · box ''", 'briefed with its open order'):
        assert evidence in drill_text, drill_text
    assert not list(Path(env['TMPDIR']).glob('qd-drill-*'))
    assert not list(private_socket_root.glob('qd-pt-*'))

    # No-capture output is kept in the main tool's report, including successful evidence.
    # The focused lease uses no xdist workers so these proof lines reach that console.
    inventory = dict(layout=dict(scratch=str(scratch), inside_checkout=scratch.is_relative_to(ROOT)),
                     source_ref=ref, wheel_sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
                     wheel_members=sorted(wheel_files), installed=reading,
                     resource_files=sorted(installed_resources),
                     source_input_sha256={name: hashlib.sha256(data).hexdigest()
                                          for name, data in sorted(source_files.items())},
                     omitted_checkout_paths=omitted, laws=len(expected_laws),
                     drill_checks=len(lines) - 1, commands_exit=0)
    with capsys.disabled():
        print('PLAIN INSTALL INVENTORY ' + json.dumps(inventory, sort_keys=True), flush=True)
        print(laws_text, drill_text, sep='\n', flush=True)
