"""Ordinary packaging and one-upgrade resource regressions, using no running seats."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
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

pytest_plugins = ("tests.shared_install",)

ROOT = Path(__file__).resolve().parents[2]
# The real historical installer has no forum/cascade copy or target-resource helper.
PREDECESSOR = '5f0f54510c3f65cc05cb2921f79bcb0ec520067d'
# Single managed-resource catalog for source archives and declaration drift coverage.
RESOURCE_ROOTS = ('standing-orders', 'ships/drydock', 'ships/forum', 'ships/cascade', 'ships/default', 'ships/shuttle', 'ships/office')
HISTORICAL_RESOURCE_ROOTS = ('standing-orders', 'ships/drydock', 'ships/forum', 'ships/cascade')


def run(*command, cwd=None, failure=False, env=None):
    done = subprocess.run([str(arg) for arg in command], cwd=cwd, capture_output=True,
                          text=True, timeout=180, env={**os.environ, **(env or {}), 'PIP_NO_INDEX': '1',
                                                    'PIP_DISABLE_PIP_VERSION_CHECK': '1'})
    if failure:
        return done
    assert done.returncode == 0, (command, done.returncode, done.stdout, done.stderr)
    return done.stdout


def git(repo, *arguments):
    return run('git', '-C', repo, *arguments).strip()


def archive(ref, destination, roots):
    done = subprocess.run(['git', '-C', str(ROOT), 'archive', ref, 'pyproject.toml',
                           'README.md', 'LICENSE', 'src', *roots], capture_output=True, timeout=30)
    assert done.returncode == 0, done.stderr.decode()
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(done.stdout)) as source:
        source.extractall(destination, filter='data')


def commit(repo, message):
    git(repo, 'add', '.')
    git(repo, '-c', 'user.name=resource-test', '-c', 'user.email=resource-test@example.invalid',
        'commit', '--quiet', '-m', message)
    return git(repo, 'rev-parse', 'HEAD')


def environment(folder, *, env=None, seed=None):
    """Only the run's own child venv is written; available build tools stay read-only."""
    if seed is None:
        run(sys.executable, '-m', 'venv', '--system-site-packages', folder, env=env)
    else:
        from tests.shared_install import copy_environment
        copy_environment(seed, folder)
        config = folder / 'pyvenv.cfg'
        config.write_text(config.read_text().replace('include-system-site-packages = false',
                                                     'include-system-site-packages = true'))
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


def upgrade(python, ship, ref, original=None, failure=False):
    """Run the actually installed incumbent, leaving its upgrade function unchanged."""
    script = '''
import hashlib, json, pathlib, sys
from quarterdeck import install
from quarterdeck.ship import load
if sys.argv[3]:
    assert hashlib.sha256(pathlib.Path(install.__file__).read_bytes()).hexdigest() == sys.argv[3]
print(json.dumps(install.upgrade(load(pathlib.Path(sys.argv[1])), sys.argv[2])))
'''
    output = run(python, '-c', script, ship, ref, original or '', cwd=ship.parent, failure=failure)
    return output if failure else json.loads(output)


def contents(folder):
    return {path.relative_to(folder).as_posix(): path.read_bytes()
            for path in folder.rglob('*') if path.is_file()}


def expected(source):
    declaration = tomllib.loads((source / 'pyproject.toml').read_text())['tool']['setuptools']['data-files']
    return {name: (source / name).read_bytes() for names in declaration.values() for name in names}


def test_managed_resources_match_tracked_files_and_declaration():
    tracked = set(filter(None, git(ROOT, 'ls-files', '--cached', '-z', '--',
                                   *RESOURCE_ROOTS).split('\0')))
    assert tracked, 'Git inventory contains no managed resource files'
    declaration = tomllib.loads((ROOT / 'pyproject.toml').read_text())['tool']['setuptools']['data-files']
    declared = {name for names in declaration.values() for name in names}
    omitted = sorted(tracked - declared)
    missing = sorted(name for name in declared if not (ROOT / name).is_file())
    assert not omitted and not missing, (
        f'Tracked resources omitted from data-files: {omitted}\n'
        f'Declared data-files missing on disk: {missing}'
    )


def incumbent(python, revisions):
    install(python, revisions.repo)
    record_incumbent(revisions)


def record_incumbent(revisions):
    (revisions.ship / 'runtime/installed.json').write_text(json.dumps(dict(
        commit=revisions.target, ref=revisions.target, previous=None, at='2026-01-01T00:00:00Z')))


def failing_command(source):
    project = source / 'pyproject.toml'
    project.write_text(project.read_text().replace('qd = "quarterdeck:main"',
                                                   'qd = "quarterdeck.resource_test_failure:main"'))
    (source / 'src/quarterdeck/resource_test_failure.py').write_text('''
import sys
def main():
    if '--version' in sys.argv:
        print('synthetic proof-failure target')
        return
    print('synthetic installed proof failed', file=sys.stderr)
    raise SystemExit(2)
''')


@dataclass
class Revisions:
    legacy: Path
    original: bytes
    repo: Path
    target: str
    ship: Path
    old: str
    unsupported: str
    no_orders: str
    failed_proof: str


@pytest.fixture
def revisions(tmp_path):
    legacy = tmp_path / 'original'
    archive(PREDECESSOR, legacy, ('standing-orders', 'ships/drydock'))
    original = (legacy / 'src/quarterdeck/install.py').read_bytes()
    assert b'resources.plan' not in original
    assert b'"ships/forum"' not in original and b'"ships/cascade"' not in original
    repo = tmp_path / 'repo'
    shutil.copytree(legacy, repo)
    git(repo, 'init', '--quiet')
    (repo / '.git/info/exclude').write_text(
        'ships/resource-test/\n*.egg-info/\nbuild/\ndist/\n__pycache__/\n')
    old = commit(repo, 'Unchanged historical predecessor for installation test')
    installer = repo / 'src/quarterdeck/install.py'
    dynamic = original.replace(b'for name in ("standing-orders", "ships/drydock"):',
                               b'for name in tuple(["standing-orders", "ships/drydock"]):')
    assert dynamic != original
    installer.write_bytes(dynamic)
    unsupported = commit(repo, 'Historical installer with unsupported copy tuple shape')
    shutil.rmtree(repo / 'standing-orders')
    no_orders = commit(repo, 'Historical target with no standing orders')
    installer.write_bytes(original)
    shutil.copytree(legacy / 'standing-orders', repo / 'standing-orders')
    failing_command(repo)
    failed_proof = commit(repo, 'Historical fake command that fails installed proof')
    # Candidate is a genuine descendant of each historical rollback target.
    for name in ('src', *RESOURCE_ROOTS):
        if (repo / name).exists():
            shutil.rmtree(repo / name)
    archive('HEAD', repo, RESOURCE_ROOTS)
    target = commit(repo, 'Target resources for installation test')
    # Only upgrade-owned paths are involved; the synthetic ship has fake programs.
    ship = repo / 'ships/resource-test'
    shutil.copytree(repo / 'ships/drydock', ship)
    declaration=ship/'ship.toml'
    declaration.write_text(declaration.read_text().replace('code = "checkout"\n', ''))
    (ship / 'runtime').mkdir()
    return Revisions(legacy, original, repo, target, ship, old, unsupported, no_orders, failed_proof)


@pytest.fixture
def installed_incumbent(revisions, shared_wheel_install):
    python = environment(revisions.ship / 'runtime/venv', seed=shared_wheel_install.prefix)
    record_incumbent(revisions)
    return python


def test_real_predecessor_bootstraps_target_once(revisions, shared_legacy_install):
    legacy, original, target, ref, ship = (revisions.legacy, revisions.original, revisions.repo,
                                         revisions.target, revisions.ship)
    from tests.shared_install import copy_environment
    python = copy_environment(shared_legacy_install.prefix, ship / 'runtime/venv')
    share = ship / 'runtime/venv/share/quarterdeck'
    assert not (share / 'ships/forum/ship.toml').exists()
    proofs = upgrade(python, ship, ref, hashlib.sha256(original).hexdigest())
    # These templates and the new helper are genuinely absent from the old incumbent.
    assert (share / 'ships/forum/ship.toml').read_bytes() == (target / 'ships/forum/ship.toml').read_bytes()
    assert (share / 'ships/cascade/ship.toml').read_bytes() == (target / 'ships/cascade/ship.toml').read_bytes()
    assert contents(share) == expected(target)
    assert json.loads((ship / 'runtime/installed.json').read_text())['commit'] == ref
    assert sum(line.startswith('installed ') for line in proofs) == 1


def test_declared_target_removes_stale_templates(revisions, installed_incumbent):
    target, ship = revisions.repo, revisions.ship
    python = installed_incumbent
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


def test_real_predeclaration_rollback_removes_new_templates(revisions, installed_incumbent):
    legacy, target, ship = revisions.legacy, revisions.repo, revisions.ship
    python = installed_incumbent
    stale = ship / 'runtime/venv/share/quarterdeck/ships/stale-test/orders.md'
    stale.parent.mkdir()
    stale.write_text('Stale synthetic template.\n')
    # Use the entire original installer and resources, not a retrofitted old copy loop.
    assert git(target, 'merge-base', revisions.old, revisions.target) == revisions.old
    proofs = upgrade(python, ship, revisions.old)
    share = ship / 'runtime/venv/share/quarterdeck'
    wanted = {f'{root}/{name}': data for root in ('standing-orders', 'ships/drydock')
              for name, data in contents(legacy / root).items()}
    assert contents(share) == wanted
    assert not any('unsupported legacy' in line for line in proofs)


def test_unsupported_legacy_rollback_copies_target_baseline(revisions, installed_incumbent):
    legacy, target, ship = revisions.legacy, revisions.repo, revisions.ship
    python = installed_incumbent
    share = ship / 'runtime/venv/share/quarterdeck'
    (share / 'ships/stale-test').mkdir()
    (share / 'ships/stale-test/orders.md').write_text('Left by earlier manual copy.\n')
    assert git(target, 'merge-base', revisions.unsupported, revisions.target) == revisions.unsupported
    proofs = upgrade(python, ship, revisions.unsupported)
    assert (share / 'ships/stale-test/orders.md').read_text() == 'Left by earlier manual copy.\n'
    assert sum('unsupported legacy copy step' in line for line in proofs) == 1
    for root in ('standing-orders', 'ships/drydock'):
        assert contents(share / root) == contents(legacy / root)
    assert json.loads((ship / 'runtime/installed.json').read_text())['commit'] == revisions.unsupported


def test_no_standing_orders_reports_help_only(revisions, installed_incumbent):
    ship = revisions.ship
    python = installed_incumbent
    proofs = upgrade(python, ship, revisions.no_orders)
    assert sum('laws/cards unavailable' in line for line in proofs) == 1
    assert any('--help proof only' in line for line in proofs)
    assert not (ship / 'runtime/venv/share/quarterdeck/standing-orders/laws.md').exists()
    assert json.loads((ship / 'runtime/installed.json').read_text())['commit'] == revisions.no_orders


def test_failed_rollback_proof_restores_previous_copy_and_truthful_record(revisions, installed_incumbent):
    ship = revisions.ship
    python = installed_incumbent
    assert git(revisions.repo, 'merge-base', revisions.failed_proof, revisions.target) == revisions.failed_proof
    result = upgrade(python, ship, revisions.failed_proof, failure=True)
    assert result.returncode != 0 and 'proof command failed' in result.stderr
    assert 'recovery completed' in result.stderr and 'earlier copy restored' in result.stderr
    assert python.exists()
    assert (python.parent / 'qd').is_file()
    record = json.loads((ship / 'runtime/installed.json').read_text())
    assert record['commit'] == revisions.target and record['previous'] is None
    assert record['last_failure']['phase'] == 'proving' and record['last_failure']['reason'] == 'proof command failed'
    assert not (ship / 'runtime/requests').exists()


@pytest.mark.parametrize('prior_record', ('missing-test', 'corrupt-test'))
def test_failed_unknown_direction_keeps_original_code_without_inventing_proof(revisions, prior_record, installed_incumbent):
    ship = revisions.ship
    python = installed_incumbent
    record_path = ship / 'runtime/installed.json'
    if prior_record == 'missing-test':
        record_path.unlink()
    else:
        record_path.write_text('{not valid JSON')
    result = upgrade(python, ship, revisions.failed_proof, failure=True)
    assert result.returncode != 0 and python.exists()
    assert (python.parent / 'qd').is_file()
    if prior_record == 'corrupt-test':
        assert 'installed state unreadable' in result.stderr
        assert record_path.read_text() == '{not valid JSON'
    else:
        assert 'proof command failed' in result.stderr and 'recovery completed' in result.stderr
        record = json.loads(record_path.read_text())
        assert record['current'] is None and record['previous'] is None
        assert record['last_failure']['reason'] == 'proof command failed'
    assert not (ship / 'runtime/requests').exists()


def test_failed_forward_proof_retains_code(revisions, installed_incumbent):
    ship = revisions.ship
    python = installed_incumbent
    failing_command(revisions.repo)
    forward = commit(revisions.repo, 'New fake command that fails installed proof')
    assert git(revisions.repo, 'merge-base', revisions.target, forward) == revisions.target
    result = upgrade(python, ship, forward, failure=True)
    assert result.returncode != 0 and 'proof command failed' in result.stderr
    assert 'recovery completed' in result.stderr and python.exists()
    record = json.loads((ship / 'runtime/installed.json').read_text())
    assert record['commit'] == revisions.target and record['previous'] is None
    assert record['last_failure']['phase'] == 'proving' and record['last_failure']['reason'] == 'proof command failed'
    assert not (ship / 'runtime/requests').exists()


def test_genuine_first_install_proof_failure_removes_half_made_venv(revisions, monkeypatch):
    from quarterdeck import install as current
    from quarterdeck.ship import ShipError, load

    monkeypatch.setattr(current, 'source_repo', lambda ship: revisions.repo)
    original_run = current._run

    def with_owned_build_tools(argv, doing, timeout=None, **kwargs):
        if argv[1:3] == ['-m', 'venv']:
            environment(Path(argv[-1]))
            return subprocess.CompletedProcess(argv,0,stdout='',stderr='')
        else:
            return original_run(argv, doing, timeout=timeout, **kwargs)

    # Supply the same read-only build tools as other synthetic venvs; pip and proof are real.
    monkeypatch.setattr(current, '_run', with_owned_build_tools)
    assert not (revisions.ship / 'runtime/venv').exists()
    with pytest.raises(ShipError, match='proof command failed'):
        current.upgrade(load(revisions.ship), revisions.failed_proof)
    assert not (revisions.ship / 'runtime/venv').exists()
    failed=json.loads((revisions.ship/'runtime/installed.json').read_text())
    assert failed['current'] is None and failed['pending'] is None
    assert failed['last_failure']['reason']=='proof command failed'
    assert failed['last_failure']['step']=='laws'
    assert not (revisions.ship / 'runtime/requests').exists()


def test_doctor_marks_retained_failed_proof_as_failed(revisions):
    from quarterdeck.doctor import framework_check
    from quarterdeck.ship import load

    record = dict(commit=revisions.failed_proof, ref=revisions.failed_proof,
                  previous=revisions.target, at='2026-01-01T00:00:00Z', proof='failed', proof_command='laws')
    (revisions.ship / 'runtime/installed.json').write_text(json.dumps(record))
    assert not framework_check(load(revisions.ship)).passed


def test_wheel_and_sdist_carry_declared_resources(revisions, tmp_path):
    source = revisions.repo
    python = environment(tmp_path / 'build-venv')
    output = tmp_path / 'dist'
    output.mkdir()
    run(python, '-c', 'from setuptools.build_meta import build_sdist, build_wheel; '
        'import sys; output_dir = sys.argv[1]; build_sdist(output_dir); build_wheel(output_dir)', output, cwd=source)
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
    archive('6ed33441276232271583a1e193af89dcbb72f62c', source, HISTORICAL_RESOURCE_ROOTS)
    shutil.rmtree(source / 'ships/forum')
    files = resources.plan(source)
    wanted = {path.relative_to(source) for root in HISTORICAL_RESOURCE_ROOTS
              if (source / root).exists() for path in (source / root).rglob('*') if path.is_file()}
    assert files is not None and set(files) == wanted
