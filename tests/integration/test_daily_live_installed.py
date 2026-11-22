"""Run the nightly check through the installed command with a local terminal stand-in."""

from pathlib import Path
import shutil
import subprocess

import pytest

from quarterdeck import copy_state, daily_live, install
from quarterdeck.ship import load
from tests.conftest import FIXTURES, git
from tests.integration.test_upgrade_resources import RESOURCE_ROOTS, archive, commit, environment
from tests.integration.test_upgrade_resources import install as install_package
from tests.shared_install import fingerprint

FIXTURE_MODULE = 'src/quarterdeck/harness/nightly_check_fake.py'


def synthetic_revisions(repo, source):
    """Expose the internal check on the existing fake adapter in this fixture only."""
    archive('HEAD', repo, RESOURCE_ROOTS)
    shutil.copyfile(FIXTURES / 'nightly_check_fake.py', repo / FIXTURE_MODULE)
    cli = repo / 'src' / 'quarterdeck' / 'cli.py'
    choices = 'choices=[n for n in harnesses.NAMES if not harnesses.get(n).synthetic]'
    text = cli.read_text()
    assert text.count(choices) == 1
    cli.write_text(text.replace(choices, 'choices=harnesses.NAMES'))
    entry = repo / 'src' / 'quarterdeck' / 'fakeharness.py'
    entry.write_text(entry.read_text().replace('from .harness.simulator import main',
                                              'from .harness.nightly_check_fake import main'))
    git(repo, 'init', '-b', 'main')
    installed = commit(repo, 'installed revision')
    shutil.copytree(repo, source, ignore=shutil.ignore_patterns('.git'))

    package = repo / 'src' / 'quarterdeck' / '__init__.py'
    package.write_text(package.read_text() + '\n# Checkout is ahead of the installed copy.\n')
    ahead = commit(repo, 'newer checkout')
    assert git(repo, 'rev-parse', f'{installed}:{FIXTURE_MODULE}') == git(repo, 'rev-parse', f'{ahead}:{FIXTURE_MODULE}')
    return installed, ahead


def test_invoke_uses_the_proved_install_to_launch_in_a_jail(tmp_path, monkeypatch):
    if not shutil.which('bwrap') or subprocess.run(
            ['bwrap', '--ro-bind', '/', '/', 'true'], capture_output=True, timeout=20).returncode:
        pytest.skip('bubblewrap namespaces are unavailable')

    repo, source = tmp_path / 'framework', tmp_path / 'install-source'
    installed_commit, ahead = synthetic_revisions(repo, source)
    worktrees = git(repo, 'worktree', 'list', '--porcelain')
    monkeypatch.setattr(install, 'source_repo', lambda origin: repo)

    folder = tmp_path / 'origin'
    folder.mkdir()
    (folder / 'ship.toml').write_text('''[ship]
name = "origin"
tick = 0.1

[pools.local]
provider = "synthetic"

[sessions.checks]

[seats.jailed]
session = "checks"
harness = "fake"
pool = "local"
sandbox = true
''')
    origin = load(folder)
    origin.runtime.mkdir()
    python = environment(origin.runtime / 'venv')
    install_package(python, source)
    fact = dict(commit=installed_commit, ref='main', at='2026-11-21T00:00:00+00:00', have_laws=True)
    install._proof(origin, fact)
    install._save(origin.runtime, dict(format=copy_state.FORMAT, current=fact))
    before = fingerprint(origin.runtime)
    child_env = daily_live._child_env
    selections = []

    def observe_selection(copied, socket):
        selections.append((copied, copy_state.select(copied)))
        return child_env(copied, socket)

    monkeypatch.setattr(daily_live, '_child_env', observe_selection)
    monkeypatch.setattr(daily_live, 'memory_root', lambda: None)
    origin_qd = copy_state.select(origin.folder).qd
    result = daily_live.invoke(origin, 'fake', daily_live.SYSTEM_TMP, str(origin_qd))

    assert result.exit_code == 0
    (copied, selected), = selections
    assert selected.qd == copied / 'runtime' / 'venv' / 'bin' / 'qd'
    assert selected.fact['commit'] == installed_commit != ahead
    assert selected.fact['package_digest'] == fact['package_digest']
    assert selected.fact['instance'] != fact['instance']
    assert 'task: the report closed the order' in result.output
    assert 'minimal: launch, task and ended turn proved' in result.output
    assert result.retained is None
    assert Path(result.place).parent == daily_live.SYSTEM_TMP
    assert not Path(result.place).exists()
    assert not Path(result.socket).parent.exists()
    assert fingerprint(origin.runtime) == before
    assert git(repo, 'rev-parse', 'HEAD') == ahead
    assert git(repo, 'worktree', 'list', '--porcelain') == worktrees
