"""The install lock moved under runtime/locks; a ship still runs a copy of the code from before the move.

That earlier copy installs this code: it holds the lock at its old place, runtime/install.lock, for the whole
operation, and starts this code's service with its start token while it holds it. The new service must know that
holder as the installer that recorded the operation, and start; any other holder of the old place is still refused.

Real install path in a throwaway: an installed copy of the earlier code (8639dedc, the last commit before the move),
its own `qd sync` to this tree, with the service running. Fake programs only; no seat is raised.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from quarterdeck import servicectl, service as servicemod, ship as shipmod
from quarterdeck.tmux import Tmux

from tests.integration.test_upgrade_resources import RESOURCE_ROOTS, archive, commit, environment, git, install

pytest_plugins = ("tests.shared_install",)

EARLIER = '8639dedcd930c008d00b65a21f1e973939cf2779'  # the ship's installed copy when the move first failed to install
EARLIER_ROOTS = ('standing-orders', 'ships/drydock', 'ships/forum', 'ships/cascade', 'ships/default', 'ships/shuttle',
                 'ships/office')


def run(*argv, env, cwd, timeout=600):
    return subprocess.run([str(arg) for arg in argv], cwd=cwd, env=env, capture_output=True, text=True,
                          timeout=timeout)


@pytest.fixture
def earlier_ship(tmp_path: Path, tmux: Tmux):
    """A repository whose history is the earlier commit then this tree, and a ship in it whose installed copy is the
    earlier code, its service running."""
    repo = tmp_path / 'repo'
    archive(EARLIER, repo, EARLIER_ROOTS)
    git(repo, 'init', '--quiet')
    (repo / '.git/info/exclude').write_text('ships/lock-migration/\n*.egg-info/\nbuild/\ndist/\n__pycache__/\n')
    earlier = commit(repo, 'The earlier code, whose installer holds runtime/install.lock')
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
    archive(EARLIER, source, EARLIER_ROOTS)
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
