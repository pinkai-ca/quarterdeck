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


def run(jail: sandbox.Jail, probe: str, *args: str, path: str) -> dict:
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
        seen = run(jail, probe, str(workspace), path=path)
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
    seen = run(jail, "import json, os; print(json.dumps(sorted(os.listdir('/mnt'))))", path=path)
    assert seen == [], seen


@pytest.fixture
def other_ship(tmp_path: Path):
    """A project holding another ship whose runtime has a stored post and a live listening socket."""
    project = tmp_path / 'project'
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
    seen = run(jail, probe, str(other / 'runtime'), str(project), path=path)
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
