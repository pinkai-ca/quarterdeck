"""Ordinary packaging and one-upgrade resource regressions, using no running seats."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
# The real historical installer has no forum/cascade copy or target-resource helper.
PREDECESSOR = '5f0f54510c3f65cc05cb2921f79bcb0ec520067d'
RESOURCE_ROOTS = ('standing-orders', 'ships/drydock', 'ships/forum', 'ships/cascade')


def run(*command, cwd=None):
    done = subprocess.run([str(arg) for arg in command], cwd=cwd, capture_output=True,
                          text=True, timeout=180, env=dict(os.environ, PIP_NO_INDEX='1',
                                                        PIP_DISABLE_PIP_VERSION_CHECK='1'))
    assert done.returncode == 0, (command, done.returncode, done.stdout, done.stderr)
    return done.stdout


def git(repo, *arguments):
    return run('git', '-C', repo, *arguments).strip()


def archive(ref, destination, roots):
    done = subprocess.run(['git', '-C', str(ROOT), 'archive', ref, 'pyproject.toml',
                           'README.md', 'LICENSE', 'src', *roots], capture_output=True, timeout=30)
    assert done.returncode == 0, done.stderr.decode()
    destination.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(done.stdout)) as source:
        source.extractall(destination, filter='data')


def commit(repo, message):
    git(repo, 'add', '.')
    git(repo, '-c', 'user.name=resource-test', '-c', 'user.email=resource-test@example.invalid',
        'commit', '--quiet', '-m', message)
    return git(repo, 'rev-parse', 'HEAD')


def environment(folder):
    """Only the run's own child venv is written; available build tools stay read-only."""
    run(sys.executable, '-m', 'venv', '--system-site-packages', folder)
    site = folder / 'lib' / f'python{sys.version_info.major}.{sys.version_info.minor}' / 'site-packages'
    paths = set()
    for name in ('setuptools', 'wheel'):
        spec = importlib.util.find_spec(name)
        assert spec is not None and spec.origin, f'{name} must be available in the test tool environment'
        paths.add(str(Path(spec.origin).parent.parent))
    (site / 'resource-build-tools.pth').write_text(''.join(path + '\n' for path in sorted(paths)))
    return folder / 'bin/python'


def install(python, source):
    run(python, '-m', 'pip', 'install', '--quiet', '--no-deps', '--no-build-isolation', source)


def upgrade(python, ship, ref, original=None):
    """Run the actually installed incumbent, leaving its upgrade function unchanged."""
    script = '''
import hashlib, json, pathlib, sys
from quarterdeck import install
from quarterdeck.ship import load
if sys.argv[3]:
    assert hashlib.sha256(pathlib.Path(install.__file__).read_bytes()).hexdigest() == sys.argv[3]
print(json.dumps(install.upgrade(load(pathlib.Path(sys.argv[1])), sys.argv[2])))
'''
    output = run(python, '-c', script, ship, ref, original or '', cwd=ship.parent)
    return json.loads(output)


def contents(folder):
    return {path.relative_to(folder).as_posix(): path.read_bytes()
            for path in folder.rglob('*') if path.is_file()}


def expected(source):
    declaration = tomllib.loads((source / 'pyproject.toml').read_text())['tool']['setuptools']['data-files']
    return {name: (source / name).read_bytes() for names in declaration.values() for name in names}


def historical_target(legacy, target):
    """Restore the archived real predecessor, retaining only the synthetic ship/repository."""
    for name in ('pyproject.toml', 'README.md', 'LICENSE'):
        shutil.copy2(legacy / name, target / name)
    shutil.rmtree(target / 'src')
    shutil.copytree(legacy / 'src', target / 'src')
    for root in RESOURCE_ROOTS:
        shutil.rmtree(target / root)
        if (legacy / root).exists():
            shutil.copytree(legacy / root, target / root)


@pytest.fixture
def revisions(tmp_path):
    legacy = tmp_path / 'original'
    archive(PREDECESSOR, legacy, ('standing-orders', 'ships/drydock'))
    original = (legacy / 'src/quarterdeck/install.py').read_bytes()
    assert b'resources.plan' not in original
    assert b'"ships/forum"' not in original and b'"ships/cascade"' not in original
    repo = tmp_path / 'repo'
    archive('HEAD', repo, RESOURCE_ROOTS)
    git(repo, 'init', '--quiet')
    (repo / '.git/info/exclude').write_text(
        'ships/resource-test/\n*.egg-info/\nbuild/\ndist/\n__pycache__/\n')
    target = commit(repo, 'Target resources for installation test')
    # Only upgrade-owned paths are involved; the synthetic ship has fake programs.
    ship = repo / 'ships/resource-test'
    shutil.copytree(repo / 'ships/drydock', ship)
    (ship / 'runtime').mkdir()
    return legacy, original, repo, target, ship


def test_real_predecessor_bootstraps_target_once(revisions):
    legacy, original, target, ref, ship = revisions
    python = environment(ship / 'runtime/venv')
    install(python, legacy)
    share = ship / 'runtime/venv/share/quarterdeck'
    assert not (share / 'ships/forum/ship.toml').exists()
    proofs = upgrade(python, ship, ref, hashlib.sha256(original).hexdigest())
    # These templates and the new helper are genuinely absent from the old incumbent.
    assert (share / 'ships/forum/ship.toml').read_bytes() == (target / 'ships/forum/ship.toml').read_bytes()
    assert (share / 'ships/cascade/ship.toml').read_bytes() == (target / 'ships/cascade/ship.toml').read_bytes()
    assert contents(share) == expected(target)
    assert json.loads((ship / 'runtime/installed.json').read_text())['commit'] == ref
    assert sum(line.startswith('installed ') for line in proofs) == 1


def test_declared_target_removes_stale_templates(revisions):
    _, _, target, _, ship = revisions
    python = environment(ship / 'runtime/venv')
    install(python, target)
    # A newer declared template precedes rollback to the original target declaration.
    newer = target / 'ships/extra-test/orders.md'
    newer.parent.mkdir()
    newer.write_text('Synthetic extra template.\n')
    project = target / 'pyproject.toml'
    original_config = project.read_text()
    project.write_text(original_config.replace('[tool.pytest.ini_options]',
        '"share/quarterdeck/ships/extra-test" = ["ships/extra-test/orders.md"]\n\n[tool.pytest.ini_options]'))
    new_ref = commit(target, 'Add one declared template for rollback test')
    upgrade(python, ship, new_ref)
    share = ship / 'runtime/venv/share/quarterdeck'
    assert (share / 'ships/extra-test/orders.md').read_text() == 'Synthetic extra template.\n'
    # An unregistered stale file also tests exact synchronization, independently of pip uninstall.
    (share / 'ships/stale-test').mkdir()
    (share / 'ships/stale-test/orders.md').write_text('Stale synthetic template.\n')
    old_ref = git(target, 'rev-parse', 'HEAD^')
    upgrade(python, ship, old_ref)
    wanted = expected(target)
    wanted.pop('ships/extra-test/orders.md')
    assert contents(share) == wanted


def test_real_predeclaration_rollback_removes_new_templates(revisions):
    legacy, _, target, _, ship = revisions
    python = environment(ship / 'runtime/venv')
    install(python, target)
    stale = ship / 'runtime/venv/share/quarterdeck/ships/stale-test/orders.md'
    stale.parent.mkdir()
    stale.write_text('Stale synthetic template.\n')
    # Use the entire original installer and resources, not a retrofitted old copy loop.
    historical_target(legacy, target)
    rollback = commit(target, 'Restore historical resource contract for rollback test')
    proofs = upgrade(python, ship, rollback)
    share = ship / 'runtime/venv/share/quarterdeck'
    wanted = {f'{root}/{name}': data for root in ('standing-orders', 'ships/drydock')
              for name, data in contents(legacy / root).items()}
    assert contents(share) == wanted
    assert not any('unsupported legacy' in line for line in proofs)


def test_unsupported_legacy_rollback_keeps_pip_result(revisions):
    legacy, original, target, _, ship = revisions
    python = environment(ship / 'runtime/venv')
    install(python, target)
    # Only make the real old tuple dynamic; the rest of the old installer stays original.
    old_source = original.replace(b'for name in ("standing-orders", "ships/drydock"):',
                                  b'for name in tuple(["standing-orders", "ships/drydock"]):')
    assert old_source != original
    historical_target(legacy, target)
    (target / 'src/quarterdeck/install.py').write_bytes(old_source)
    rollback = commit(target, 'Use unsupported historical loop shape for fallback test')
    share = ship / 'runtime/venv/share/quarterdeck'
    (share / 'ships/stale-test').mkdir()
    (share / 'ships/stale-test/orders.md').write_text('Left by earlier manual copy.\n')
    proofs = upgrade(python, ship, rollback)
    assert (share / 'ships/stale-test/orders.md').read_text() == 'Left by earlier manual copy.\n'
    assert sum('unsupported legacy copy step' in line for line in proofs) == 1
    assert json.loads((ship / 'runtime/installed.json').read_text())['commit'] == rollback


def test_wheel_and_sdist_carry_declared_resources(revisions, tmp_path):
    _, _, source, _, _ = revisions
    python = environment(tmp_path / 'build-venv')
    output = tmp_path / 'dist'
    output.mkdir()
    run(python, '-c', 'from setuptools.build_meta import build_sdist, build_wheel; '
        'import sys; build_sdist(sys.argv[1]); build_wheel(sys.argv[1])', output, cwd=source)
    wanted = expected(source)
    wheel, = output.glob('*.whl')
    with zipfile.ZipFile(wheel) as built:
        actual = {name.split('.data/data/share/quarterdeck/', 1)[1]: built.read(name)
                  for name in built.namelist() if '.data/data/share/quarterdeck/' in name}
    assert actual == wanted
    sdist, = output.glob('*.tar.gz')
    unpacked = tmp_path / 'sdist'
    unpacked.mkdir()
    with tarfile.open(sdist) as built:
        built.extractall(unpacked, filter='data')
    extracted, = unpacked.iterdir()
    assert expected(extracted) == wanted
    # Rebuilding from the sdist proves its resource sources were actually carried.
    rebuilt = tmp_path / 'rebuilt'
    rebuilt.mkdir()
    run(python, '-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation',
        '--wheel-dir', rebuilt, extracted)
    from_sdist, = rebuilt.glob('*.whl')
    with zipfile.ZipFile(from_sdist) as built:
        actual = {name.split('.data/data/share/quarterdeck/', 1)[1]: built.read(name)
                  for name in built.namelist() if '.data/data/share/quarterdeck/' in name}
    assert actual == wanted


def test_historical_optional_template_skip(tmp_path):
    from quarterdeck import resources

    source = tmp_path / 'historical'
    archive('6ed33441276232271583a1e193af89dcbb72f62c', source, RESOURCE_ROOTS)
    shutil.rmtree(source / 'ships/forum')
    files = resources.plan(source)
    wanted = {path.relative_to(source) for root in RESOURCE_ROOTS
              if (source / root).exists() for path in (source / root).rglob('*') if path.is_file()}
    assert files is not None and set(files) == wanted
