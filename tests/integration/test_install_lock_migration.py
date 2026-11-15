"""The install lock moved under runtime/locks; a ship still runs a copy of the code from before the move.

That earlier copy installs this code: it holds the lock at its old place, runtime/install.lock, for the whole
operation, and starts this code's service with its start token while it holds it. The new service must know that
holder as the installer that recorded the operation, and start; any other holder of the old place is still refused.

Real install path in a throwaway: an installed copy of the earlier code (8639dedc, the last commit before the move),
its own `qd sync` to this tree, with the service running. Fake programs only; no seat is raised.

The heartbeat moved too, into runtime/heartbeat/, after the lock had moved. Every installer from before that move, the
lock's earlier place or not, confirms a new service by its beat at runtime/service.heartbeat. So the same sync is run
from the copy just before the heartbeat's move (35cbf5af), whose installer holds its lock at this code's place: the
case the ship's own sync of the move met and the 8639dedc copy did not, since only a holder of the lock's earlier place
made the service beat there too. It starts with the beat a rolled-back sync of the move left in the folder.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from quarterdeck import servicectl, service as servicemod, ship as shipmod
from quarterdeck.tmux import Tmux

from tests.integration.test_upgrade_resources import RESOURCE_ROOTS, archive, commit, environment, git, install

pytest_plugins = ("tests.shared_install",)

EARLIER = '8639dedcd930c008d00b65a21f1e973939cf2779'  # the ship's installed copy when the move first failed to install
# The last commit before the heartbeat's move: its installer holds the lock under runtime/locks, and waits for a new
# service's beat only at runtime/service.heartbeat. The ship's installed copy when that move first failed to sync was
# 0f20b40c, the same in both; this is main as the move was built on.
EARLIER_HEARTBEAT = '35cbf5af9dfa3997d47310a57643f110b108d8d4'
EARLIER_ROOTS = ('standing-orders', 'ships/drydock', 'ships/forum', 'ships/cascade', 'ships/default', 'ships/shuttle',
                 'ships/office')


def run(*argv, env, cwd, timeout=600):
    return subprocess.run([str(arg) for arg in argv], cwd=cwd, env=env, capture_output=True, text=True,
                          timeout=timeout)


@pytest.fixture(params=[EARLIER, EARLIER_HEARTBEAT], ids=['before-the-lock-move', 'before-the-heartbeat-move'])
def earlier_ship(request, tmp_path: Path, tmux: Tmux):
    """A repository whose history is the earlier commit then this tree, and a ship in it whose installed copy is the
    earlier code, its service running."""
    earlier_commit = request.param
    repo = tmp_path / 'repo'
    archive(earlier_commit, repo, EARLIER_ROOTS)
    git(repo, 'init', '--quiet')
    (repo / '.git/info/exclude').write_text('ships/lock-migration/\n*.egg-info/\nbuild/\ndist/\n__pycache__/\n')
    earlier = commit(repo, 'The earlier code, whose installer predates the move')
    for name in ('src', *RESOURCE_ROOTS):
        if (repo / name).exists():
            shutil.rmtree(repo / name)
    archive('HEAD', repo, RESOURCE_ROOTS)
    target = commit(repo, 'This code, whose install lock lies under runtime/locks')
    ship = repo / 'ships/lock-migration'
    shutil.copytree(repo / 'ships/drydock', ship)
    declaration = ship / 'ship.toml'
    declaration.write_text(declaration.read_text().replace('code = "checkout"\n', ''))
    (ship / 'runtime').mkdir()

    env = {key: value for key, value in os.environ.items() if not key.startswith(('QD_', 'TMUX'))}
    env.update(QD_TMUX_SOCKET=tmux.socket, PIP_NO_INDEX='1', PIP_DISABLE_PIP_VERSION_CHECK='1')
    # The earlier code, installed outside the ship, makes the ship's first copy at the earlier commit.
    source = tmp_path / 'earlier-source'
    archive(earlier_commit, source, EARLIER_ROOTS)
    seed = environment(tmp_path / 'earlier-seed', env=env)
    install(seed, source)
    first = run(seed.parent / 'qd', '--ship', ship, 'sync', earlier, env=env, cwd=repo)
    assert first.returncode == 0, (first.stdout, first.stderr)
    assert json.loads((ship / 'runtime/installed.json').read_text())['commit'] == earlier
    started = run(ship / 'runtime/venv/bin/python', '-c',
                  'import sys; from pathlib import Path; from quarterdeck import servicectl, ship; '
                  'print(servicectl.start(ship.load(Path(sys.argv[1])), qd=Path(sys.argv[2])))',
                  ship, ship / 'runtime/venv/bin/qd', env=env, cwd=repo, timeout=120)
    assert started.returncode == 0, (started.stdout, started.stderr)
    assert servicectl.alive(ship / 'runtime'), 'the earlier code\'s service runs: the sync must restart it'
    # What the ship's rolled-back sync of the heartbeat's move left: a dead service's beat in the folder, which the
    # earlier code neither writes nor clears.
    stale = ship / 'runtime/heartbeat/service.heartbeat'
    stale.parent.mkdir()
    stale.write_text('4194301 2026-11-15T19:04:09.000000+00:00\n')
    try:
        yield ship, repo, env, earlier, target
    finally:
        servicemod.stop(ship / 'runtime')


def test_the_earlier_installed_copy_syncs_to_this_code_with_its_service_running(earlier_ship):
    ship, repo, env, earlier, target = earlier_ship
    runtime = ship / 'runtime'
    done = run(runtime / 'venv/bin/qd', '--ship', ship, 'sync', target, env=env, cwd=repo)
    record = json.loads((runtime / 'installed.json').read_text())
    log = (runtime / 'log/service.out').read_text() if (runtime / 'log/service.out').exists() else ''
    logs = {str(path.relative_to(runtime)): path.read_text(errors='replace')[-1500:]
            for path in sorted(runtime.glob('log/**/*')) if path.is_file()}
    if done.returncode:
        pytest.fail('\n'.join([done.stdout, done.stderr, json.dumps(record.get('last_failure')),
                               *(f'--- {name}\n{text}' for name, text in logs.items())]), pytrace=False)
    assert record['commit'] == target and not record.get('pending'), record
    identity = servicectl.service_identity(runtime)
    assert identity is not None and identity['copy']['commit'] == target, identity
    assert 'earlier code' not in log, log[-2000:]
    # The new service beats in both places, and the stale beat it found in the folder is gone.
    for place in (servicectl.heartbeat_path(runtime), servicectl.legacy_heartbeat_path(runtime)):
        assert place.read_text().split()[0] == str(identity['pid']), place
    # The earlier installer has exited: the service lets the earlier place of its lock go on its next pass.
    from tests.integration.waiting import wait_for
    assert wait_for(lambda: not (runtime / 'service.lock').exists(), seconds=60), 'the earlier service lock stays'
    assert servicectl.alive(runtime)
    # The earlier lock is the installer's own until it finishes; the next operation of this code retires it.
    loaded = shipmod.load(ship)
    from quarterdeck import install
    with install.install_lock(loaded, blocking=False):
        pass
    assert not (runtime / install.LEGACY_INSTALL_LOCK).exists()


OLD_JAIL_PROBE = r"""
import json, sys
from pathlib import Path
from quarterdeck import servicectl
runtime = Path(sys.argv[1])
beat = servicectl.read_heartbeat(runtime)
print(json.dumps(dict(path=str(servicectl.heartbeat_path(runtime).relative_to(runtime)), pid=beat and beat[0],
                      locked=servicectl.locked(runtime))))
"""


@pytest.mark.parametrize('earlier_ship', [EARLIER_HEARTBEAT], ids=['before-the-heartbeat-move'], indirect=True)
def test_after_a_sync_back_to_the_pre_move_copy_its_beat_is_read_and_its_jail_sees_it(earlier_ship):
    """Back on the copy from before the heartbeat's move, the service beats only at runtime/service.heartbeat; a beat a
    newer service left in runtime/heartbeat/ is never read over it, and a jail of that code still sees the beat."""
    from tests.integration.test_sandbox import namespaces_allowed
    from tests.integration.waiting import wait_for

    ship, repo, env, earlier, target = earlier_ship
    runtime = ship / 'runtime'
    forward = run(runtime / 'venv/bin/qd', '--ship', ship, 'sync', target, env=env, cwd=repo)
    assert forward.returncode == 0, (forward.stdout, forward.stderr)
    back = run(runtime / 'venv/bin/qd', '--ship', ship, 'sync', '--back', env=env, cwd=repo)
    assert back.returncode == 0, (back.stdout, back.stderr)
    assert json.loads((runtime / 'installed.json').read_text())['commit'] == earlier
    identity = servicectl.service_identity(runtime)
    assert identity is not None and identity['copy']['commit'] == earlier, identity
    pid = identity['pid']
    # As a sync that rolled back leaves it: a newer service's last beat in the folder, which this code never touches.
    folder = servicectl.heartbeat_path(runtime)
    folder.parent.mkdir(exist_ok=True)
    folder.write_text(f'4194301 {(datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()}\n')
    assert wait_for(lambda: servicectl.legacy_heartbeat_path(runtime).read_text().split()[0] == str(pid)
                    and (servicectl.read_heartbeat(runtime) or (0,))[0] == pid, seconds=30), \
        (servicectl.read_heartbeat(runtime), folder.read_text())
    assert servicectl.locked(runtime) and not servicectl.stale(runtime)
    if not namespaces_allowed():
        pytest.skip('bubblewrap cannot make a namespace here')  # the reading above stands; the jail needs bwrap
    # A jail of the restored code, launched by that code, reads the service's beat at the old place.
    jailed = run(runtime / 'venv/bin/python', '-c', '''
import subprocess, sys
from pathlib import Path
from quarterdeck import sandbox, ship as shipmod
ship = shipmod.load(Path(sys.argv[1]))
shipmod.fence(ship)
own = ship.runtime / 'seats' / 'worker-1'
(own / 'home').mkdir(parents=True, exist_ok=True)
work = Path(sys.argv[2])
work.mkdir(exist_ok=True)
python = sys.executable
jail = sandbox.plan(ship, 'worker-1', own, own / 'home', work, python, '/usr/bin:/bin', home=Path.home())
done = subprocess.run(sandbox.command(jail, [python, '-I', '-c', sys.argv[3], str(ship.runtime)]),
                      capture_output=True, text=True, timeout=60)
print(done.stdout.strip().splitlines()[-1] if done.returncode == 0 else 'failed ' + done.stderr[-1500:])
''', ship, ship.parent.parent.parent / 'jail-work', OLD_JAIL_PROBE, env=env, cwd=repo, timeout=120)
    assert jailed.returncode == 0, (jailed.stdout, jailed.stderr)
    seen = json.loads(jailed.stdout.strip().splitlines()[-1])
    assert seen == dict(path='service.heartbeat', pid=pid, locked=True), seen
