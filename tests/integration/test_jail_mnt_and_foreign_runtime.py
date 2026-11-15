"""A jail shows nothing of /mnt but what a seat is given, and nothing of another ship's runtime.

Real bubblewrap, stand-ins only: a Python probe is the jailed program. Another ship is a folder with a ship.toml and a
runtime holding a stored post and a live listening socket, as a running ship's tmux server is.
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from quarterdeck import sandbox, seat as seatmod, ship as shipmod
from quarterdeck.ship import ShipError

from .test_sandbox import namespaces_allowed

pytestmark = pytest.mark.skipif(not namespaces_allowed(), reason="bubblewrap is not installed, or cannot make a namespace here (inside a jail)")

HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
QD = Path(sys.executable).parent / 'qd'


def run_probe(jail: sandbox.Jail, probe: str, *args: str, path: str) -> dict:
    env = {key: value for key, value in os.environ.items() if not key.startswith(('QD_', 'TMUX'))}
    env['PATH'] = path
    result = subprocess.run(sandbox.command(jail, [sys.executable, '-I', '-c', probe, *args]), env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def own_seat(ship: shipmod.Ship) -> Path:
    own = ship.runtime / 'seats' / 'worker-1'
    (own / 'home').mkdir(parents=True, exist_ok=True)
    return own


@pytest.mark.skipif(not os.access('/mnt/scratch', os.W_OK), reason='no writable /mnt/scratch to hold a workspace')
def test_mnt_shows_only_the_workspace_bound_back_under_it(drydock: Path):
    ship = shipmod.load(drydock)
    shipmod.fence(ship)
    own = own_seat(ship)
    workspace = Path(tempfile.mkdtemp(prefix='qd-jail-mnt-', dir='/mnt/scratch'))
    try:
        path = os.pathsep.join([str(Path(sys.executable).parent), '/usr/bin', '/bin'])
        jail = sandbox.plan(ship, 'worker-1', own, own / 'home', workspace, sys.executable, path, home=HOME)
        probe = r"""
import json, os, sys
workspace = sys.argv[1]
open(os.path.join(workspace, 'written'), 'w').write('ok')
print(json.dumps(dict(mnt=sorted(os.listdir('/mnt')), scratch=sorted(os.listdir('/mnt/scratch')))))
"""
        seen = run_probe(jail, probe, str(workspace), path=path)
        assert seen['mnt'] == ['scratch'] and seen['scratch'] == [workspace.name], seen
        assert (workspace / 'written').read_text() == 'ok', 'the workspace under /mnt is still writable'
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def test_mnt_is_empty_inside_when_nothing_under_it_is_given(drydock: Path, tmp_path: Path):
    if not Path('/mnt').is_dir():
        pytest.skip('no /mnt here')
    ship = shipmod.load(drydock)
    shipmod.fence(ship)
    own = own_seat(ship)
    (tmp_path / 'work').mkdir()
    path = os.pathsep.join([str(Path(sys.executable).parent), '/usr/bin', '/bin'])
    jail = sandbox.plan(ship, 'worker-1', own, own / 'home', tmp_path / 'work', sys.executable, path, home=HOME)
    seen = run_probe(jail, "import json, os; print(json.dumps(sorted(os.listdir('/mnt'))))", path=path)
    assert seen == [], seen


@pytest.fixture
def other_ship(tmp_path: Path):
    """A project holding another ship whose runtime has a stored post and a live listening socket.

    Under a short folder in /dev/shm: a unix socket's path is limited to about a hundred bytes, and a folder there
    that a PATH entry names is bound back into the jail, as a folder under the home is.
    """
    short = Path(tempfile.mkdtemp(prefix='qdfr-', dir='/dev/shm'))
    project = short / 'project'
    other = project / 'ships' / 'other'
    (other / 'runtime' / 'post' / 'store').mkdir(parents=True)
    (other / 'runtime' / 'bin').mkdir()
    (other / 'runtime' / 'tmux').mkdir()
    (other / 'ship.toml').write_text('[ship]\nname = "other"\n')
    (other / 'runtime' / 'post' / 'store' / 'secret.json').write_text('{"body": "another ship\'s post"}')
    (other / 'runtime' / 'bin' / 'qd').symlink_to(QD)
    (project / 'bin').mkdir()
    (project / 'README').write_text('the project itself')
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(other / 'runtime' / 'tmux' / 'server'))
    server.listen(4)
    yield project, other
    server.close()
    shutil.rmtree(short, ignore_errors=True)


def test_another_ships_runtime_is_never_in_the_jail_and_qd_is_this_ships_own(drydock: Path, tmp_path: Path, other_ship):
    project, other = other_ship
    ship = shipmod.load(drydock)
    shipmod.fence(ship)
    own = own_seat(ship)
    (tmp_path / 'work').mkdir()
    # As a seat's launch builds it: this ship's own runtime/bin first, then the shell's PATH, here led by another
    # ship's runtime/bin, and a project bin whose folder holds that ship.
    shell = os.pathsep.join([str(other / 'runtime' / 'bin'), str(project / 'bin'), str(Path(sys.executable).parent),
                             '/usr/bin', '/bin'])
    os.environ['PATH'], saved = shell, os.environ['PATH']
    try:
        path = seatmod.prepare_program_path(ship, synthetic=True)
    finally:
        os.environ['PATH'] = saved
    assert path.split(os.pathsep)[0] == str(ship.runtime / 'bin')
    jail = sandbox.plan(ship, 'worker-1', own, own / 'home', tmp_path / 'work', sys.executable, path, home=HOME)
    assert not any(f == other / 'runtime' or (other / 'runtime') in f.parents for f in jail.read_only), jail.read_only
    probe = r"""
import json, os, shutil, socket, sys
runtime, project = sys.argv[1], sys.argv[2]
def reaches(path):
    probe = socket.socket(socket.AF_UNIX)
    try:
        probe.connect(path)
        return True
    except OSError:
        return False
    finally:
        probe.close()
print(json.dumps(dict(
    runtime=sorted(os.listdir(runtime)) if os.path.isdir(runtime) else None,
    secret=os.path.exists(os.path.join(runtime, 'post', 'store', 'secret.json')),
    socket=reaches(os.path.join(runtime, 'tmux', 'server')),
    project=os.path.exists(os.path.join(project, 'README')),
    qd=os.path.realpath(shutil.which('qd') or ''),
    first=shutil.which('qd'),
)))
"""
    seen = run_probe(jail, probe, str(other / 'runtime'), str(project), path=path)
    assert seen['runtime'] in (None, []), f"another ship's runtime shows inside: {seen['runtime']}"
    assert seen['secret'] is False and seen['socket'] is False
    assert seen['first'] == str(ship.runtime / 'bin' / 'qd'), 'the seat resolves its own ship\'s qd first'


def test_a_seat_whose_qd_lives_in_another_ships_runtime_is_refused(drydock: Path, tmp_path: Path, other_ship):
    project, other = other_ship
    venv = other / 'runtime' / 'venv' / 'bin'
    venv.mkdir(parents=True)
    shutil.copy2(QD, venv / 'qd')  # a real file inside the other ship's runtime, as an installed copy is
    ship = shipmod.load(drydock)
    shipmod.fence(ship)
    own = own_seat(ship)
    (tmp_path / 'work').mkdir()
    path = os.pathsep.join([str(venv), '/usr/bin', '/bin'])
    with pytest.raises(ShipError, match="inside another ship's runtime"):
        sandbox.plan(ship, 'worker-1', own, own / 'home', tmp_path / 'work', sys.executable, path, home=HOME)


def test_a_listening_socket_under_a_bound_folders_test_runs_is_unreachable(drydock: Path, tmp_path: Path):
    """A test run's live tmux server listens under a checkout's .testruns; a path socket ignores the jail's network.
    A folder bound into the jail that holds .testruns shows it empty."""
    short = Path(tempfile.mkdtemp(prefix='qdtr-', dir='/dev/shm'))
    checkout = short / 'checkout'
    run = checkout / '.testruns' / 'r' / 'u-1'
    run.mkdir(parents=True)
    (checkout / 'bin').mkdir()
    (checkout / 'bin' / 'stand-in').write_text('#!/bin/sh\n')  # a program installed in the checkout: its prefix is bound
    (checkout / 'bin' / 'stand-in').chmod(0o755)
    (checkout / 'README').write_text('the checkout itself')
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(run / 's'))
    server.listen(4)
    try:
        ship = shipmod.load(drydock)
        shipmod.fence(ship)
        own = own_seat(ship)
        (tmp_path / 'work').mkdir()
        path = os.pathsep.join([str(checkout / 'bin'), str(Path(sys.executable).parent), '/usr/bin', '/bin'])
        jail = sandbox.plan(ship, 'worker-1', own, own / 'home', tmp_path / 'work', 'stand-in', path, home=HOME)
        assert checkout in jail.read_only, 'the checkout is bound back, as a program installed in it is'
        assert checkout / '.testruns' in jail.foreign_runtimes
        probe = r"""
import json, os, socket, sys
runs, checkout = sys.argv[1], sys.argv[2]
probe = socket.socket(socket.AF_UNIX)
try:
    probe.connect(os.path.join(runs, 'r', 'u-1', 's'))
    reached = True
except OSError:
    reached = False
print(json.dumps(dict(reached=reached, runs=sorted(os.listdir(runs)) if os.path.isdir(runs) else None,
                      checkout=os.path.exists(os.path.join(checkout, 'README')))))
"""
        seen = run_probe(jail, probe, str(checkout / '.testruns'), str(checkout), path=path)
        assert seen['reached'] is False and seen['runs'] in (None, []), seen
        assert seen['checkout'] is True, 'the bound folder itself is still there'
    finally:
        server.close()
        shutil.rmtree(short, ignore_errors=True)


def test_a_listening_socket_in_any_folder_bound_from_a_home_refuses_inside(drydock: Path, tmp_path: Path):
    """Not only .testruns: a test server's socket anywhere in a folder bound back from the home is masked."""
    short = Path(tempfile.mkdtemp(prefix='qdsm-', dir='/dev/shm'))
    home = short / 'home'
    tools = home / 'tools'
    (tools / 'bin').mkdir(parents=True)
    (tools / 'bin' / 'stand-in').write_text('#!/bin/sh\n')
    (tools / 'bin' / 'stand-in').chmod(0o755)
    (tools / 'servers').mkdir()
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(tools / 'servers' / 's'))
    server.listen(4)
    try:
        ship = shipmod.load(drydock)
        shipmod.fence(ship)
        own = own_seat(ship)
        (tmp_path / 'work').mkdir()
        path = os.pathsep.join([str(tools / 'bin'), str(Path(sys.executable).parent), '/usr/bin', '/bin'])
        jail = sandbox.plan(ship, 'worker-1', own, own / 'home', tmp_path / 'work', 'stand-in', path, home=home)
        assert tools in jail.read_only, "the program's folder under the home is bound back"
        probe = r"""
import json, os, socket, sys
probe = socket.socket(socket.AF_UNIX)
try:
    probe.connect(sys.argv[1])
    reached = True
except OSError:
    reached = False
print(json.dumps(dict(reached=reached, there=os.path.exists(sys.argv[1]))))
"""
        seen = run_probe(jail, probe, str(tools / 'servers' / 's'), path=path)
        assert seen == dict(reached=False, there=True), seen
    finally:
        server.close()
        shutil.rmtree(short, ignore_errors=True)


def test_a_socket_in_a_folder_bound_through_a_link_refuses_inside(drydock: Path, tmp_path: Path):
    """bwrap binds a link's target at the link's path: the socket shows there inside, and is masked there."""
    short = Path(tempfile.mkdtemp(prefix='qdml-', dir='/dev/shm'))
    real = short / 'real-tools'
    (real / 'bin').mkdir(parents=True)
    (real / 'bin' / 'stand-in').write_text('#!/bin/sh\n')
    (real / 'bin' / 'stand-in').chmod(0o755)
    (real / 'servers').mkdir()
    home = short / 'home'
    home.mkdir()
    (home / 'tools').symlink_to(real)  # the program's folder, reached through a link under the home
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(real / 'servers' / 's'))
    server.listen(4)
    try:
        ship = shipmod.load(drydock)
        shipmod.fence(ship)
        own = own_seat(ship)
        (tmp_path / 'work').mkdir()
        path = os.pathsep.join([str(home / 'tools' / 'bin'), str(Path(sys.executable).parent), '/usr/bin', '/bin'])
        jail = sandbox.plan(ship, 'worker-1', own, own / 'home', tmp_path / 'work', 'stand-in', path, home=home)
        assert home / 'tools' in jail.read_only, 'bound by the link'
        probe = r"""
import json, os, socket, sys
probe = socket.socket(socket.AF_UNIX)
try:
    probe.connect(sys.argv[1])
    reached = True
except OSError:
    reached = False
print(json.dumps(dict(reached=reached, there=os.path.exists(sys.argv[1]))))
"""
        seen = run_probe(jail, probe, str(home / 'tools' / 'servers' / 's'), path=path)
        assert seen == dict(reached=False, there=True), seen
    finally:
        server.close()
        shutil.rmtree(short, ignore_errors=True)


def test_a_jailed_seat_whose_workspace_is_a_project_holding_an_office_and_ships_starts(drydock: Path, tmp_path: Path):
    """The workspace is the project folder itself, with office/ and two other ships beside this one: the fleet fence
    already leaves their runtimes absent, so no cover of its own may be laid over one (bwrap could not make its mount
    point in the read-only fence). This ship's own runtime shows only its allowlist."""
    project = tmp_path / 'project'
    own_folder = project / 'ships' / 'own'
    shutil.copytree(drydock, own_folder)
    for name in ('alpha', 'beta'):
        other = project / 'ships' / name
        (other / 'runtime' / 'post' / 'store').mkdir(parents=True)
        (other / 'ship.toml').write_text(f'[ship]\nname = "{name}"\n')
        (other / 'runtime' / 'post' / 'store' / 'secret.json').write_text('another ship\'s post')
    office = project / 'office'
    (office / 'runtime' / 'post').mkdir(parents=True)
    (office / 'ship.toml').write_text('[ship]\nname = "office"\nkind = "office"\n')
    (office / 'runtime' / 'post' / 'secret.json').write_text("the office's post")
    ship = shipmod.load(own_folder)
    shipmod.fence(ship)
    (ship.runtime / 'ledger.sqlite').write_text('not for a jail')
    own = own_seat(ship)
    path = os.pathsep.join([str(Path(sys.executable).parent), '/usr/bin', '/bin'])
    jail = sandbox.plan(ship, 'worker-1', own, own / 'home', project, sys.executable, path, home=HOME)
    probe = r"""
import json, os, sys
project, runtime = sys.argv[1], sys.argv[2]
def listing(path):
    return sorted(os.listdir(path)) if os.path.isdir(path) else None
print(json.dumps(dict(
    alpha=listing(os.path.join(project, 'ships', 'alpha', 'runtime')),
    beta=listing(os.path.join(project, 'ships', 'beta', 'runtime')),
    office=listing(os.path.join(project, 'office', 'runtime')),
    own=listing(runtime),
)))
"""
    seen = run_probe(jail, probe, str(project), str(ship.runtime), path=path)
    assert seen['alpha'] is None and seen['beta'] is None and seen['office'] is None, seen
    allowed = {*sandbox.RUNTIME_SHOWN, 'seats', 'tmux', 'locks', 'trusted', 'post', 'check', 'brief'}
    assert set(seen['own']) <= allowed and 'ledger.sqlite' not in seen['own'], seen['own']


@pytest.mark.parametrize('entry', ['ships/alpha', 'office', 'ships'])
def test_a_path_entry_inside_the_fleet_is_never_bound_and_no_other_runtime_shows(drydock: Path, tmp_path: Path, entry):
    """A folder inside the fleet other than this ship's own is never bound back: the fence alone shows the fleet,
    whatever the order of mounts at the same path."""
    project = tmp_path / 'project'
    own_folder = project / 'ships' / 'own'
    shutil.copytree(drydock, own_folder)
    alpha = project / 'ships' / 'alpha'
    (alpha / 'runtime' / 'post' / 'store').mkdir(parents=True)
    (alpha / 'ship.toml').write_text('[ship]\nname = "alpha"\n')
    (alpha / 'runtime' / 'post' / 'store' / 'secret.json').write_text('another ship\'s post')
    office = project / 'office'
    (office / 'runtime' / 'post').mkdir(parents=True)
    (office / 'ship.toml').write_text('[ship]\nname = "office"\nkind = "office"\n')
    (office / 'runtime' / 'post' / 'secret.json').write_text("the office's post")
    ship = shipmod.load(own_folder)
    shipmod.fence(ship)
    own = own_seat(ship)
    (tmp_path / 'work').mkdir()
    path = os.pathsep.join([str(project / entry), str(Path(sys.executable).parent), '/usr/bin', '/bin'])
    jail = sandbox.plan(ship, 'worker-1', own, own / 'home', tmp_path / 'work', sys.executable, path, home=HOME)
    fleet = (project / 'ships', project / 'office')
    assert not any(f == root or f.is_relative_to(root) for f in jail.read_only for root in fleet), jail.read_only
    probe = r"""
import json, os, sys
project = sys.argv[1]
def listing(path):
    return sorted(os.listdir(path)) if os.path.isdir(path) else None
print(json.dumps(dict(alpha=listing(os.path.join(project, 'ships', 'alpha', 'runtime')),
                      office=listing(os.path.join(project, 'office', 'runtime')))))
"""
    seen = run_probe(jail, probe, str(project), path=path)
    assert seen == dict(alpha=None, office=None), seen
