"""A ship's installed copy of the code that moved the heartbeat syncs to this code, and back.

The heartbeat moved into runtime/heartbeat/ at 11ca0ea6, which beat at runtime/service.heartbeat too, because every
installer before it waits for a new service's beat there. This code beats in the folder alone, so it is installed
only over 11ca0ea6 or later, whose installer reads the folder; an installer from before the move (8639dedc, before
the lock's move, or 35cbf5af, just before the heartbeat's) would time out at target_ready, and no ship runs one.

Real install path in a throwaway: an installed copy of 11ca0ea6, its own `qd sync` to this tree with the service
running, and this code's `qd sync --back` to that copy, each through target_ready to a running service. Fake programs
only; no seat is raised.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from quarterdeck import servicectl, service as servicemod
from quarterdeck.tmux import Tmux

from tests.integration.test_upgrade_resources import RESOURCE_ROOTS, archive, commit, environment, git, install

pytest_plugins = ("tests.shared_install",)

# The copy that moved the heartbeat into its folder, the first that beats there and reads it, and the last that also
# beats at runtime/service.heartbeat.
EARLIER = '11ca0ea6'
EARLIER_ROOTS = ('standing-orders', 'ships/drydock', 'ships/forum', 'ships/cascade', 'ships/default', 'ships/shuttle',
                 'ships/office')


def run(*argv, env, cwd, timeout=600):
    return subprocess.run([str(arg) for arg in argv], cwd=cwd, env=env, capture_output=True, text=True,
                          timeout=timeout)


@pytest.fixture
def earlier_ship(tmp_path: Path, tmux: Tmux):
    """A repository whose history is the earlier commit then this tree, and a ship in it whose installed copy is the
    earlier code, its service running."""
    earlier_commit = EARLIER
    repo = tmp_path / 'repo'
    archive(earlier_commit, repo, EARLIER_ROOTS)
    git(repo, 'init', '--quiet')
    (repo / '.git/info/exclude').write_text('ships/lock-migration/\n*.egg-info/\nbuild/\ndist/\n__pycache__/\n')
    earlier = commit(repo, 'The earlier code, which beats at both places')
    for name in ('src', *RESOURCE_ROOTS):
        if (repo / name).exists():
            shutil.rmtree(repo / name)
    archive('HEAD', repo, RESOURCE_ROOTS)
    target = commit(repo, 'This code, which beats in the folder alone')
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
    from tests.integration.waiting import wait_for
    assert wait_for(lambda: (ship / 'runtime/service.heartbeat').exists(), seconds=30), 'the earlier code beats there too'
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
    # The new service beats in its folder alone: the earlier place, which the earlier service beat too, stays empty.
    import time
    pid = identity['pid']
    assert servicectl.heartbeat_path(runtime).read_text().split()[0] == str(pid)
    assert (servicectl.read_heartbeat(runtime) or (0,))[0] == pid
    first = servicectl.heartbeat_path(runtime).read_text()
    time.sleep(5)  # past two beats
    assert servicectl.heartbeat_path(runtime).read_text() != first, 'it keeps beating'
    assert not (runtime / servicectl.EARLIER_HEARTBEAT).exists(), 'no beat at runtime/service.heartbeat'
    assert servicectl.alive(runtime)


OLD_JAIL_PROBE = r"""
import json, sys
from pathlib import Path
from quarterdeck import servicectl
runtime = Path(sys.argv[1])
beat = servicectl.read_heartbeat(runtime)
print(json.dumps(dict(path=str(servicectl.heartbeat_path(runtime).relative_to(runtime)), pid=beat and beat[0],
                      locked=servicectl.locked(runtime))))
"""


def test_this_codes_sync_back_to_the_earlier_copy_reaches_its_service_and_its_jail_sees_it(earlier_ship):
    """This code's installer syncs back to 11ca0ea6 through target_ready, confirming its service by the folder's beat;
    that service beats in the folder again, this code reads it, and a jail of that code sees it."""
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
    folder = servicectl.heartbeat_path(runtime)
    assert wait_for(lambda: (servicectl.read_heartbeat(runtime) or (0,))[0] == pid
                    and servicectl.read_heartbeat(runtime)[1] < 5, seconds=30), \
        (servicectl.read_heartbeat(runtime), folder.read_text() if folder.exists() else None)
    assert servicectl.locked(runtime) and not servicectl.stale(runtime)
    if not namespaces_allowed():
        pytest.skip('bubblewrap cannot make a namespace here')  # the reading above stands; the jail needs bwrap
    # A jail of the restored code, launched by that code, reads the service's beat in the folder.
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
    assert seen == dict(path='heartbeat/service.heartbeat', pid=pid, locked=True), seen
