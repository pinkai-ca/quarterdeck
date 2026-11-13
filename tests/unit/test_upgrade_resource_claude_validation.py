"""Claude validation of target resource handling and failed-proof transitions in qd upgrade.

Everything here is synthetic: staged target trees built in a temporary folder, a fake archive,
fake pip and proof steps, and a fake service. No real ship, runtime record or native program is
read. The ordinary integration file covers real pip, the real predecessor installer, packaging
artifacts and the first-install removal; these cases cover the boundaries around them.
"""

from __future__ import annotations

import ast
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tomllib
from types import SimpleNamespace

import pytest

from quarterdeck import install, resources
from quarterdeck.doctor import framework_check
from quarterdeck.ship import ShipError, load

ROOT = Path(__file__).resolve().parents[2]
TARGET = 'c' * 40
PRIOR = 'd' * 40
UNRESOLVED = 'e' * 40
SENTINEL = 'PRIVATE-SENTINEL-test'
DECLARED_ROOTS = ('standing-orders', 'ships/drydock', 'ships/forum', 'ships/cascade')

LEGACY_INSTALLER = '''
import shutil


def upgrade(ship, ref="HEAD"):
    venv = ship.runtime / "venv"
    staging = ship.runtime / "upgrade"
    share = venv / "share" / "quarterdeck"
    for name in ("standing-orders", "ships/drydock"):
        shutil.rmtree(share / name, ignore_errors=True)
        shutil.copytree(staging / name, share / name)
    return []
'''
LEGACY_LOOP = 'for name in ("standing-orders", "ships/drydock"):'
LEGACY_REMOVE = '        shutil.rmtree(share / name, ignore_errors=True)\n'
LEGACY_COPY = '        shutil.copytree(staging / name, share / name)\n'


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def files_under(folder: Path) -> dict[str, bytes]:
    return {path.relative_to(folder).as_posix(): path.read_bytes()
            for path in sorted(folder.rglob('*')) if path.is_file() and not path.is_symlink()}


def toml_table(table: dict) -> str:
    """JSON strings and lists of plain ASCII strings are valid TOML values."""
    lines = ['[project]', 'name = "resource-validation-test"', '', '[tool.setuptools.data-files]']
    lines += [f'{json.dumps(destination)} = {json.dumps(names)}' for destination, names in table.items()]
    return '\n'.join(lines) + '\n'


def destination(name: str) -> str:
    parent = name.rsplit('/', 1)[0] if '/' in name else ''
    return 'share/quarterdeck' + (f'/{parent}' if parent else '')


def declared_target(folder: Path, names: dict[str, str]) -> Path:
    """A target whose pyproject declares exactly the given files, each with its own content."""
    table: dict[str, list[str]] = {}
    for name, text in names.items():
        write(folder / name, text)
        table.setdefault(destination(name), []).append(name)
    write(folder / 'pyproject.toml', toml_table(table))
    return folder


def legacy_target(folder: Path, installer: str | None = LEGACY_INSTALLER,
                  names: dict[str, str] | None = None) -> Path:
    """A target from before the declaration: a pyproject without data-files and an old installer."""
    write(folder / 'pyproject.toml', '[project]\nname = "resource-validation-test"\n')
    if installer is not None:
        write(folder / 'src/quarterdeck/install.py', installer)
    default = {'standing-orders/laws.md': 'target laws\n',
               'standing-orders/roles/captain.md': 'target captain card\n',
               'ships/drydock/ship.toml': 'target drydock\n'}
    for name, text in (default if names is None else names).items():
        write(folder / name, text)
    return folder


# Packaging declaration -------------------------------------------------------------------------

def test_claude_declaration_lists_exactly_the_tracked_framework_templates():
    """A template file added to a declared root but not to the list would silently never install."""
    declaration = tomllib.loads((ROOT / 'pyproject.toml').read_text())['tool']['setuptools']['data-files']
    declared = sorted(name for names in declaration.values() for name in names)
    tracked = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '-z', '--', *DECLARED_ROOTS],
                             capture_output=True, check=True).stdout.decode().split('\0')
    assert declared == sorted(name for name in tracked if name)
    assert len(set(declared)) == len(declared)
    for target, names in declaration.items():
        for name in names:
            assert target == destination(name)
            assert not name.startswith(('ships/default/', 'runtime/')) and '/runtime/' not in name
    assert resources.plan(ROOT) == tuple(sorted(Path(name) for name in declared))


INVALID_DECLARATIONS = {
    'parent-traversal': {'share/quarterdeck/../outside-test': ['../outside-test/laws.md']},
    'absolute-path': {'share/quarterdeck/standing-orders': ['/standing-orders/laws.md']},
    'runtime-path': {'share/quarterdeck/ships/drydock/runtime': ['ships/drydock/runtime/state.json']},
    'git-path': {'share/quarterdeck/ships/drydock/.git': ['ships/drydock/.git/config']},
    'hidden-file': {'share/quarterdeck/ships/drydock': ['ships/drydock/.hidden-test']},
    'cache-path': {'share/quarterdeck/standing-orders/__pycache__': ['standing-orders/__pycache__/laws.md']},
    'glob-pattern': {'share/quarterdeck/standing-orders': ['standing-orders/*.md']},
    'backslash': {'share/quarterdeck/standing-orders': ['standing-orders\\laws.md']},
    'dot-segment': {'share/quarterdeck/standing-orders': ['standing-orders/./laws.md']},
    'non-framework-root': {'share/quarterdeck/src/quarterdeck': ['src/quarterdeck/install.py']},
    'moved-destination': {'share/quarterdeck/ships': ['standing-orders/laws.md']},
    'duplicate-file': {'share/quarterdeck/standing-orders': ['standing-orders/laws.md', 'standing-orders/laws.md']},
    'empty-file-list': {'share/quarterdeck/standing-orders': []},
    'non-string-name': {'share/quarterdeck/standing-orders': [7]},
    'empty-table': {},
    'missing-file': {'share/quarterdeck/standing-orders': ['standing-orders/absent-test.md']},
}


@pytest.mark.parametrize('case', sorted(INVALID_DECLARATIONS))
def test_claude_invalid_declaration_is_refused_by_plan(tmp_path, case):
    source = tmp_path / 'target'
    for name in ('standing-orders/laws.md', 'ships/drydock/runtime/state.json', 'ships/drydock/.git/config',
                 'ships/drydock/.hidden-test', 'standing-orders/__pycache__/laws.md',
                 'src/quarterdeck/install.py'):
        write(source / name, 'synthetic resource\n')
    write(source / 'pyproject.toml', toml_table(INVALID_DECLARATIONS[case]))
    with pytest.raises(ShipError, match='invalid target resource declaration'):
        resources.plan(source)


@pytest.mark.parametrize('link', ('linked-file', 'linked-directory'))
def test_claude_declared_resource_reached_through_a_link_is_refused(tmp_path, link):
    source = tmp_path / 'target'
    real = write(tmp_path / 'elsewhere/laws.md', 'outside the target tree\n')
    if link == 'linked-file':
        (source / 'standing-orders').mkdir(parents=True)
        (source / 'standing-orders/laws.md').symlink_to(real)
    else:
        source.mkdir()
        (source / 'standing-orders').symlink_to(real.parent, target_is_directory=True)
    write(source / 'pyproject.toml',
          toml_table({'share/quarterdeck/standing-orders': ['standing-orders/laws.md']}))
    with pytest.raises(ShipError, match='invalid target resource declaration'):
        resources.plan(source)


def test_claude_malformed_declaration_error_carries_no_file_content(tmp_path):
    source = tmp_path / 'target'
    write(source / 'pyproject.toml', '[tool.setuptools.data-files]\n'
                                     f'"share/quarterdeck/standing-orders" = ["{SENTINEL}" oops\n')
    with pytest.raises(ShipError) as raised:
        resources.plan(source)
    assert SENTINEL not in str(raised.value)


# Historical copy step --------------------------------------------------------------------------

@pytest.mark.parametrize('revision', ('3d1487e96ce4a0d132bdb3d1431b84b9a6c9e205',
                                      'bc5394493a184af0e07ef25549e127a03846001f'))
def test_claude_real_historical_installers_are_read_as_supported(tmp_path, revision):
    """The first copy loop and the single-template skip; the predecessor and the tuple skip are ordinary's."""
    source = tmp_path / 'historical'
    source.mkdir()
    tracked = subprocess.run(['git', '-C', str(ROOT), 'ls-tree', '-r', '--name-only', revision, '--', *DECLARED_ROOTS],
                             capture_output=True, check=True, text=True).stdout.split()
    listed = [root for root in DECLARED_ROOTS if any(name.startswith(root + '/') for name in tracked)]
    assert listed[:2] == ['standing-orders', 'ships/drydock']
    done = subprocess.run(['git', '-C', str(ROOT), 'archive', revision, 'pyproject.toml', 'src', *listed],
                          capture_output=True, check=True)
    with tarfile.open(fileobj=io.BytesIO(done.stdout)) as archive:
        archive.extractall(source, filter='data')
    wanted = {path.relative_to(source) for root in listed
              for path in (source / root).rglob('*') if path.is_file()}
    assert resources.plan(source) == tuple(sorted(wanted))
    if (source / 'ships/forum').exists():
        shutil.rmtree(source / 'ships/forum')
        assert resources.plan(source) == tuple(sorted(path for path in wanted if path.parts[:2] != ('ships', 'forum')))


@pytest.mark.parametrize('present', (True, False))
def test_claude_synthetic_optional_skip_reads_only_its_literal_roots(tmp_path, present):
    installer = LEGACY_INSTALLER.replace(
        LEGACY_LOOP, 'for name in ("standing-orders", "ships/drydock", "ships/extra-test"):').replace(
        LEGACY_COPY, '        if name == "ships/extra-test" and not (staging / name).exists():\n'
                     '            continue\n' + LEGACY_COPY)
    source = legacy_target(tmp_path / 'target', installer)
    write(source / 'ships/unlisted-test/orders.md', 'never part of the historical copy\n')
    if present:
        write(source / 'ships/extra-test/orders.md', 'optional template\n')
    expected = ['ships/drydock/ship.toml', 'standing-orders/laws.md', 'standing-orders/roles/captain.md']
    if present:
        expected.append('ships/extra-test/orders.md')
    assert resources.plan(source) == tuple(sorted(Path(name) for name in expected))


def unsupported(case: str, source: Path) -> None:
    """Alter a supported synthetic legacy target into one shape the reader must not accept."""
    installer = source / 'src/quarterdeck/install.py'
    text = installer.read_text()
    loop = lambda roots: text.replace(LEGACY_LOOP, f'for name in {roots}:')  # noqa: E731
    if case == 'missing-installer':
        installer.unlink()
        return
    altered = {
        'syntax-error': 'def upgrade(:\n',
        'oversized-source': text + '#' * (resources.SOURCE_LIMIT + 1) + '\n',
        'too-many-nodes': text + 'BULK = [' + '0, ' * 20001 + ']\n',
        'two-upgrade-functions': text + text.split('import shutil', 1)[1],
        'second-copy-call': text.replace('    return []', '    shutil.copytree(staging / "x", share / "x")\n    return []'),
        'renamed-loop-variable': text.replace('for name in', 'for root in'),
        'extra-loop-statement': text.replace(LEGACY_COPY, '        print(name)\n' + LEGACY_COPY),
        'computed-root': loop('("standing-orders", "ships/" + "drydock")'),
        'loop-else': text.replace(LEGACY_COPY, LEGACY_COPY + '    else:\n        pass\n'),
        'skip-outside-tuple': text.replace(LEGACY_COPY, '        if name == "ships/other-test" and not (staging / name).exists():\n'
                                                       '            continue\n' + LEGACY_COPY),
        'parent-traversal-root': loop('("standing-orders", "ships/../../outside-test")'),
        'runtime-root': loop('("standing-orders", "ships/drydock/runtime")'),
        'non-framework-root': loop('("standing-orders", "src")'),
        'missing-required-root': loop('("standing-orders", "ships/drydock", "ships/absent-test")'),
        'duplicate-root': loop('("standing-orders", "standing-orders")'),
    }.get(case)
    if altered is not None:
        installer.write_text(altered)
    elif case == 'hidden-file-in-root':
        write(source / 'standing-orders/.hidden-test', 'not a framework file\n')
    elif case == 'linked-file-in-root':
        real = write(source.parent / 'elsewhere/card.md', 'outside the target tree\n')
        (source / 'standing-orders/roles/linked.md').symlink_to(real)
    elif case == 'linked-root':
        real = source.parent / 'elsewhere-drydock'
        (source / 'ships/drydock').rename(real)
        (source / 'ships/drydock').symlink_to(real, target_is_directory=True)
    else:
        raise AssertionError(case)


UNSUPPORTED = ('missing-installer', 'syntax-error', 'oversized-source', 'too-many-nodes', 'two-upgrade-functions',
               'second-copy-call', 'renamed-loop-variable', 'extra-loop-statement', 'computed-root', 'loop-else',
               'skip-outside-tuple', 'parent-traversal-root', 'runtime-root', 'non-framework-root',
               'missing-required-root', 'duplicate-root', 'hidden-file-in-root', 'linked-file-in-root', 'linked-root')


@pytest.mark.parametrize('case', UNSUPPORTED)
def test_claude_unsupported_legacy_shape_falls_back_and_never_raises(tmp_path, case):
    source = legacy_target(tmp_path / 'target')
    assert resources.plan(source) is not None  # the unaltered synthetic shape is supported
    unsupported(case, source)
    assert resources.plan(source) is None


# Fallback and synchronization ------------------------------------------------------------------

def test_claude_fallback_copies_only_target_baseline_roots(tmp_path):
    source = legacy_target(tmp_path / 'target', names={
        'standing-orders/laws.md': 'target laws\n', 'ships/drydock/ship.toml': 'target drydock\n',
        'ships/forum/ship.toml': 'target forum, not a fallback root\n'})
    share = tmp_path / 'venv/share/quarterdeck'
    write(share / 'standing-orders/laws.md', 'incumbent laws\n')
    write(share / 'standing-orders/roles/stale.md', 'incumbent card\n')
    write(share / 'ships/drydock/ship.toml', 'incumbent drydock\n')
    write(share / 'ships/extra-test/orders.md', 'left as pip left it\n')
    assert resources.fallback(source, share) is True
    assert files_under(share / 'standing-orders') == {'laws.md': b'target laws\n'}
    assert files_under(share / 'ships/drydock') == {'ship.toml': b'target drydock\n'}
    assert (share / 'ships/extra-test/orders.md').read_text() == 'left as pip left it\n'
    assert not (share / 'ships/forum').exists()


def test_claude_fallback_without_standing_orders_reports_no_laws(tmp_path):
    source = legacy_target(tmp_path / 'target', names={'ships/drydock/ship.toml': 'target drydock\n'})
    share = tmp_path / 'venv/share/quarterdeck'
    assert resources.fallback(source, share) is False
    assert files_under(share) == {'ships/drydock/ship.toml': b'target drydock\n'}


def test_claude_synchronize_replaces_only_the_managed_tree_exactly(tmp_path):
    source = declared_target(tmp_path / 'target', {'standing-orders/laws.md': 'target laws\n',
                                                   'ships/drydock/ship.toml': 'target drydock\n'})
    files = resources.plan(source)
    share = tmp_path / 'venv/share/quarterdeck'
    write(share / 'standing-orders/laws.md', 'incumbent laws\n')
    write(share / 'ships/stale-test/orders.md', 'stale template\n')
    sibling = write(tmp_path / 'venv/share/other-package-test/keep.md', 'another package\n')
    resources.synchronize(source, share, files)
    assert files_under(share) == {'ships/drydock/ship.toml': b'target drydock\n',
                                  'standing-orders/laws.md': b'target laws\n'}
    assert sibling.read_text() == 'another package\n'
    assert sorted(path.name for path in share.parent.iterdir()) == ['other-package-test', 'quarterdeck']


def test_claude_synchronize_failure_keeps_the_old_tree_whole(tmp_path):
    source = declared_target(tmp_path / 'target', {'standing-orders/laws.md': 'target laws\n',
                                                   'standing-orders/roles/captain.md': 'target card\n'})
    files = resources.plan(source)
    share = tmp_path / 'venv/share/quarterdeck'
    write(share / 'standing-orders/laws.md', 'incumbent laws\n')
    write(share / 'ships/forum/ship.toml', 'incumbent forum\n')
    before = files_under(share)
    (source / 'standing-orders/roles/captain.md').unlink()  # vanishes after planning, before copying
    with pytest.raises(ShipError, match='invalid target resource'):
        resources.synchronize(source, share, files)
    assert files_under(share) == before
    assert sorted(path.name for path in share.parent.iterdir()) == ['quarterdeck']


@pytest.mark.parametrize('linked', ('managed-tree', 'share-parent'))
def test_claude_synchronize_never_writes_through_a_link(tmp_path, linked):
    source = declared_target(tmp_path / 'target', {'standing-orders/laws.md': 'target laws\n'})
    files = resources.plan(source)
    outside = tmp_path / 'outside-test'
    write(outside / 'quarterdeck/keep.md', 'not the ship runtime\n')
    venv = tmp_path / 'venv'
    venv.mkdir()
    if linked == 'managed-tree':
        (venv / 'share').mkdir()
        (venv / 'share/quarterdeck').symlink_to(outside / 'quarterdeck', target_is_directory=True)
    else:
        (venv / 'share').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ShipError, match='link'):
        resources.synchronize(source, venv / 'share/quarterdeck', files)
    assert files_under(outside) == {'quarterdeck/keep.md': b'not the ship runtime\n'}


# Upgrade transitions ---------------------------------------------------------------------------

@pytest.fixture
def harness(drydock, tmp_path, monkeypatch):
    """install.upgrade with a staged synthetic target, fake pip, a controllable proof and a fake service."""
    ship = load(drydock)
    runtime = ship.runtime
    runtime.mkdir(parents=True, exist_ok=True)
    h = SimpleNamespace(ship=ship, runtime=runtime, venv=runtime / 'venv', record=runtime / install.RECORD,
                        target=tmp_path / 'target', calls=[], running=False, proof_error=None,
                        pip_writes_qd=True, statuses=[])
    h.share = h.venv / 'share/quarterdeck'
    declared_target(h.target, {'standing-orders/laws.md': 'target laws\n',
                               'ships/drydock/ship.toml': 'target drydock\n'})

    def archive() -> bytes:
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode='w') as bundle:
            for path in sorted(h.target.rglob('*')):
                if path.is_file() and not path.is_symlink():
                    bundle.add(path, arcname=path.relative_to(h.target).as_posix(), recursive=False)
        return data.getvalue()

    def git(repo, *args):
        if args[0] == 'rev-parse':
            return (TARGET + '\n').encode()
        assert args[0] == 'archive'
        return archive()

    def run(argv, doing, timeout=None):
        if argv[1:3] == ['-m', 'venv']:
            h.calls.append('venv')
            write(Path(argv[-1]) / 'bin/python', '')
        elif argv[1:3] == ['-m', 'pip']:
            h.calls.append('pip')
            if h.pip_writes_qd:
                qd = write(h.venv / 'bin/qd', '#!/bin/sh\necho "qd synthetic-test"\n')
                qd.chmod(0o755)
        else:
            h.calls.append(('proof', argv[-1]))
            if h.proof_error is not None:
                raise h.proof_error

    def stop(runtime):
        h.calls.append('stop')
        h.running = False
        return 'service stopped · synthetic'

    def start(ship, qd=None):
        h.calls.append('start')
        return 'service started · synthetic'

    monkeypatch.setattr(install, 'source_repo', lambda ship: tmp_path / 'no-repository-test')
    monkeypatch.setattr(install, 'git', git)
    monkeypatch.setattr(install, '_run', run)
    monkeypatch.setattr(install.servicectl, 'alive', lambda runtime: h.running)
    monkeypatch.setattr(install.servicectl, 'stop', stop)
    monkeypatch.setattr(install.servicectl, 'start', start)
    h.tmux = SimpleNamespace(has_session=lambda title: True,
                             status_line=lambda title, command: h.statuses.append(title))
    return h


def existing_venv(h, python=True):
    (h.venv / 'bin').mkdir(parents=True)
    if python:
        (h.venv / 'bin/python').touch()


def assert_failed_marker(h, previous=None, command='laws'):
    record = json.loads(h.record.read_text())
    assert record['commit'] == TARGET and record['ref'] == 'HEAD' and record['previous'] == previous
    assert record['proof'] == 'failed' and record['proof_command'] == command
    assert isinstance(record['at'], str) and record['at']
    assert 'start' not in h.calls and h.statuses == []
    assert not (h.runtime / 'requests').exists()


def test_claude_invalid_target_declaration_changes_nothing_before_install(harness):
    h = harness
    existing_venv(h)
    write(h.share / 'standing-orders/laws.md', 'incumbent laws\n')
    h.record.write_text(json.dumps(dict(commit=PRIOR, ref='old', at='2026-01-01T00:00:00+00:00', previous=None)))
    record = h.record.read_bytes()
    h.running = True
    write(h.target / 'pyproject.toml', toml_table({'share/quarterdeck/standing-orders': ['../outside-test/laws.md']}))
    with pytest.raises(ShipError, match='invalid target resource declaration'):
        install.upgrade(h.ship, 'HEAD', h.tmux)
    assert h.calls == [] and h.running
    assert h.record.read_bytes() == record
    assert files_under(h.share) == {'standing-orders/laws.md': b'incumbent laws\n'}


def test_claude_failed_proof_keeps_a_half_made_preexisting_venv(harness):
    h = harness
    existing_venv(h, python=False)  # an environment existed, even without an interpreter
    h.proof_error = ShipError('proving the installed copy answers failed: synthetic')
    with pytest.raises(ShipError, match=r'retained \(unknown\).*service not restarted'):
        install.upgrade(h.ship, 'HEAD', h.tmux)
    assert h.venv.is_dir() and (h.venv / 'bin/qd').exists()
    assert h.calls == ['venv', 'pip', ('proof', 'laws')]
    assert_failed_marker(h)


def test_claude_failed_proof_leaves_a_running_service_down_and_status_lines_unpointed(harness):
    h = harness
    existing_venv(h)
    h.record.write_text(json.dumps(dict(commit=PRIOR, ref='old', at='2026-01-01T00:00:00+00:00', previous=None)))
    h.running = True
    h.proof_error = ShipError('proving the installed copy answers failed: synthetic')
    with pytest.raises(ShipError, match='service not restarted'):
        install.upgrade(h.ship, 'HEAD', h.tmux)
    assert h.calls.count('stop') == 1 and not h.running
    assert_failed_marker(h, previous=PRIOR)


def test_claude_proof_timeout_retains_code_with_marker(harness):
    h = harness
    existing_venv(h)
    h.proof_error = ShipError('proving the installed copy answers failed: no answer within 60s')
    with pytest.raises(ShipError, match='retained'):
        install.upgrade(h.ship, 'HEAD', h.tmux)
    assert (h.venv / 'bin/python').exists()
    assert_failed_marker(h)


@pytest.mark.parametrize('command', ('missing', 'not-executable'))
def test_claude_installed_version_failure_is_marked_as_the_version_step(harness, command):
    """The marker names the step that actually failed: the version query, never a proof that did not run."""
    h = harness
    existing_venv(h)
    h.pip_writes_qd = False
    if command == 'not-executable':
        write(h.venv / 'bin/qd', '#!/bin/sh\necho "qd synthetic-test"\n').chmod(0o644)
    with pytest.raises(ShipError, match=r'retained.*--version proof failed'):
        install.upgrade(h.ship, 'HEAD', h.tmux)
    assert (h.venv / 'bin/python').exists()
    assert not any(isinstance(call, tuple) and call[0] == 'proof' for call in h.calls)
    assert_failed_marker(h, command='--version')


def test_claude_unwritable_failure_record_is_a_plain_retained_error(harness):
    """When even the failed marker cannot be written, the error says so and nothing restarts."""
    h = harness
    existing_venv(h)
    h.record.mkdir()  # a directory where the record belongs: unreadable as evidence and unwritable
    h.running = True
    h.proof_error = ShipError('proving the installed copy answers failed: synthetic')
    with pytest.raises(ShipError) as raised:
        install.upgrade(h.ship, 'HEAD', h.tmux)
    message = str(raised.value).lower()
    assert TARGET[:7] in message and 'retained' in message
    assert 'not written' in message and 'restart' in message
    assert h.record.is_dir() and not any(h.record.iterdir())
    assert (h.venv / 'bin/python').exists() and (h.venv / 'bin/qd').exists()
    assert h.calls.count('stop') == 1 and 'start' not in h.calls and not h.running
    assert h.statuses == [] and not (h.runtime / 'requests').exists()


CORRUPT_RECORDS = {
    'invalid-json': f'{{"commit": "{SENTINEL}"'.encode(),
    'json-list': json.dumps([SENTINEL]).encode(),
    'short-commit': json.dumps(dict(commit='c' * 7, note=SENTINEL)).encode(),
    'uppercase-commit': json.dumps(dict(commit='C' * 40, note=SENTINEL)).encode(),
    'non-string-commit': json.dumps(dict(commit={'value': SENTINEL})).encode(),
    'trailing-newline-commit': json.dumps(dict(commit=PRIOR + '\n', note=SENTINEL)).encode(),
    'undecodable-bytes': b'\xff\xfe' + SENTINEL.encode(),
    'deeply-nested': b'[' * 100000 + SENTINEL.encode(),
}


@pytest.mark.parametrize('case', sorted(CORRUPT_RECORDS))
def test_claude_corrupt_prior_record_is_unknown_and_never_echoed(harness, case):
    """Failure path: unknown direction, nothing of the old record in the error or the new record."""
    h = harness
    existing_venv(h)
    h.record.write_bytes(CORRUPT_RECORDS[case])
    assert install.previous_install(h.runtime) is None
    h.proof_error = ShipError('proving the installed copy answers failed: synthetic')
    with pytest.raises(ShipError, match=r'retained \(unknown\)') as raised:
        install.upgrade(h.ship, 'HEAD', h.tmux)
    assert SENTINEL not in str(raised.value)
    assert SENTINEL not in h.record.read_text()
    assert_failed_marker(h)


@pytest.mark.parametrize('case', ('invalid-json', 'deeply-nested'))
def test_claude_corrupt_prior_record_never_gates_a_successful_install(harness, case):
    h = harness
    existing_venv(h)
    h.record.write_bytes(CORRUPT_RECORDS[case])
    proofs = install.upgrade(h.ship, 'HEAD', h.tmux)
    assert 'previous installed commit unknown' in proofs[0]
    assert not any(SENTINEL in line for line in proofs)
    record = json.loads(h.record.read_text())
    assert record['commit'] == TARGET and record['previous'] is None and 'proof' not in record
    assert files_under(h.share) == {'ships/drydock/ship.toml': b'target drydock\n',
                                    'standing-orders/laws.md': b'target laws\n'}


def test_claude_existing_venv_without_record_is_unknown_not_initial(harness):
    h = harness
    existing_venv(h)
    proofs = install.upgrade(h.ship, 'HEAD', h.tmux)
    assert 'previous installed commit unknown' in proofs[0]
    assert 'the ship ran the checkout before' not in proofs[0]
    assert h.calls == ['pip', ('proof', 'laws')]
    assert json.loads(h.record.read_text())['previous'] is None
    assert (h.runtime / f'requests/upgrade-rebrief-{TARGET}.json').exists()


def test_claude_successful_upgrade_clears_a_prior_failed_marker(harness):
    h = harness
    existing_venv(h)
    h.record.write_text(json.dumps(dict(commit=PRIOR, ref='old', at='2026-01-01T00:00:00+00:00',
                                        previous=None, proof='failed', proof_command='laws')))
    proofs = install.upgrade(h.ship, 'HEAD', h.tmux)
    assert f'was {PRIOR[:7]}' in proofs[0]
    record = json.loads(h.record.read_text())
    assert set(record) == {'commit', 'ref', 'at', 'previous'}
    assert record['commit'] == TARGET and record['previous'] == PRIOR
    assert framework_check(h.ship).passed


def test_claude_first_install_failure_removes_only_its_own_environment(harness):
    h = harness
    keep = write(h.runtime / 'keep-test/state.md', 'other runtime state\n')
    h.proof_error = ShipError('proving the installed copy answers failed: synthetic')
    with pytest.raises(ShipError, match='synthetic') as raised:
        install.upgrade(h.ship, 'HEAD', h.tmux)
    assert 'retained' not in str(raised.value)
    assert not os.path.lexists(h.venv) and not h.record.exists()
    assert keep.read_text() == 'other runtime state\n'
    assert 'start' not in h.calls and not (h.runtime / 'requests').exists()


def test_claude_unsupported_legacy_target_uses_target_baseline_and_ignores_listed_traversal(harness):
    h = harness
    existing_venv(h)
    shutil.rmtree(h.target)
    legacy_target(h.target, LEGACY_INSTALLER.replace(
        LEGACY_LOOP, 'for name in ("standing-orders", "ships/drydock", "ships/../../outside-test"):'))
    guard = write(h.venv / 'outside-test/keep.md', 'never a resource destination\n')
    write(h.share / 'standing-orders/laws.md', 'incumbent laws\n')
    write(h.share / 'ships/forum/ship.toml', 'left as pip left it\n')
    proofs = install.upgrade(h.ship, 'HEAD', h.tmux)
    assert sum(line == resources.FALLBACK for line in proofs) == 1
    assert resources.NO_LAWS not in proofs
    assert ('proof', 'laws') in h.calls
    assert files_under(h.share / 'standing-orders') == {'laws.md': b'target laws\n',
                                                        'roles/captain.md': b'target captain card\n'}
    assert (h.share / 'ships/forum/ship.toml').read_text() == 'left as pip left it\n'
    assert files_under(guard.parent) == {'keep.md': b'never a resource destination\n'}


def test_claude_target_without_standing_orders_fails_help_proof_with_marker(harness):
    h = harness
    existing_venv(h)
    shutil.rmtree(h.target)
    legacy_target(h.target, None, names={'ships/drydock/ship.toml': 'target drydock\n'})
    h.proof_error = ShipError('proving the installed copy answers failed: synthetic')
    with pytest.raises(ShipError, match=r'--help proof failed'):
        install.upgrade(h.ship, 'HEAD', h.tmux)
    assert ('proof', '--help') in h.calls
    assert_failed_marker(h, command='--help')



def test_claude_resource_failure_after_pip_is_a_retained_failure(harness, monkeypatch):
    """After the first mutation nothing escapes past the record: a resource step failure is marked too."""
    h = harness
    existing_venv(h)
    h.record.write_text(json.dumps(dict(commit=PRIOR, ref='old', at='2026-01-01T00:00:00+00:00', previous=None)))
    h.running = True

    def failing(source, share, files):
        raise ShipError('synthetic resource step failure')

    monkeypatch.setattr(install.resources, 'synchronize', failing)
    with pytest.raises(ShipError, match='retained'):
        install.upgrade(h.ship, 'HEAD', h.tmux)
    assert (h.venv / 'bin/python').exists() and 'pip' in h.calls
    assert not any(isinstance(call, tuple) and call[0] == 'proof' for call in h.calls)
    assert not h.running
    assert_failed_marker(h, previous=PRIOR, command='resources')


@pytest.mark.parametrize('linked', ('venv', 'venv-share', 'managed-tree'))
def test_claude_linked_environment_is_refused_before_any_mutation(harness, tmp_path, linked):
    """A link would let the upgrade write outside the project folder: refused before stop, pip or record."""
    h = harness
    outside = tmp_path / 'outside-test'
    write(outside / 'bin/python', '')
    write(outside / 'share/quarterdeck/standing-orders/laws.md', 'not the ship runtime\n')
    if linked == 'venv':
        h.venv.symlink_to(outside, target_is_directory=True)
    elif linked == 'venv-share':
        existing_venv(h)
        h.venv.joinpath('share').symlink_to(outside / 'share', target_is_directory=True)
    else:
        existing_venv(h)
        (h.venv / 'share').mkdir()
        h.share.symlink_to(outside / 'share/quarterdeck', target_is_directory=True)
    h.record.write_text(json.dumps(dict(commit=PRIOR, ref='old', at='2026-01-01T00:00:00+00:00', previous=None)))
    record = h.record.read_bytes()
    before = files_under(outside)
    h.running = True
    with pytest.raises(ShipError):
        install.upgrade(h.ship, 'HEAD', h.tmux)
    assert h.calls == [] and h.running
    assert h.record.read_bytes() == record
    assert files_under(outside) == before
    assert h.statuses == [] and not (h.runtime / 'requests').exists()

# Direction is wording only ---------------------------------------------------------------------

@pytest.fixture
def history(tmp_path):
    repo = tmp_path / 'history'
    repo.mkdir()

    def git(*args):
        return subprocess.run(['git', '-C', str(repo), '-c', 'user.name=direction-test',
                               '-c', 'user.email=direction-test@example.invalid', *args],
                              capture_output=True, check=True, text=True).stdout.strip()

    git('init', '--quiet')
    commits = {}
    for name in ('first', 'second', 'third'):
        git('commit', '--quiet', '--allow-empty', '-m', f'{name} synthetic commit')
        commits[name] = git('rev-parse', 'HEAD')
    git('checkout', '--quiet', '-b', 'side-test', commits['first'])
    git('commit', '--quiet', '--allow-empty', '-m', 'divergent synthetic commit')
    commits['divergent'] = git('rev-parse', 'HEAD')
    return repo, commits


def test_claude_direction_reads_history_without_deciding_anything(history):
    repo, c = history
    assert install.install_direction(repo, c['second'], None) == 'unknown'
    assert install.install_direction(repo, c['second'], {'commit': c['second']}) == 'same commit'
    assert install.install_direction(repo, c['first'], {'commit': c['third']}) == 'rollback'
    assert install.install_direction(repo, c['third'], {'commit': c['first']}) == 'forward'
    assert install.install_direction(repo, c['divergent'], {'commit': c['third']}) == 'unknown'
    assert install.install_direction(repo, c['divergent'], {'commit': c['third'], 'previous': c['divergent']}) == 'rollback'
    assert install.install_direction(repo, c['second'], {'commit': UNRESOLVED}) == 'unknown'
    assert install.install_direction(repo, UNRESOLVED, {'commit': c['second']}) == 'unknown'


@pytest.mark.parametrize('commit', ('a' * 40, 'b' * 64))
def test_claude_previous_install_accepts_full_hex_commits(tmp_path, commit):
    (tmp_path / install.RECORD).write_text(json.dumps(dict(commit=commit, at='2026-01-01T00:00:00+00:00')))
    assert install.previous_install(tmp_path)['commit'] == commit


# Doctor reads, never repairs -------------------------------------------------------------------

def test_claude_doctor_fails_only_the_failed_marker_and_writes_nothing(drydock):
    ship = load(drydock)
    ship.runtime.mkdir(parents=True, exist_ok=True)
    record = ship.runtime / install.RECORD
    base = dict(commit=TARGET, ref='HEAD', at='2026-01-01T00:00:00+00:00', previous=PRIOR)
    record.write_text(json.dumps(dict(base, proof='failed', proof_command='--help')))
    before = files_under(ship.runtime)
    check = framework_check(ship)
    assert not check.passed
    assert TARGET[:7] in check.detail and '--help proof failed' in check.detail
    assert files_under(ship.runtime) == before
    record.write_text(json.dumps(dict(base, proof='failed')))
    check = framework_check(ship)
    assert not check.passed and 'installed proof failed' in check.detail
    record.write_text(json.dumps(dict(base, proof='unrecognized-test')))
    assert framework_check(ship).passed
    record.write_text(json.dumps(base))
    assert framework_check(ship).passed


def test_claude_only_upgrade_writes_the_share_tree():
    """Read commands never synchronize or repair: the share writers have one caller, qd upgrade."""
    callers = []
    for module in sorted((ROOT / 'src/quarterdeck').rglob('*.py')):
        tree = ast.parse(module.read_text())
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in ('synchronize', 'fallback')
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == 'resources'):
                    callers.append((module.relative_to(ROOT).as_posix(), function.name, node.func.attr))
    assert sorted(set(callers)) == [('src/quarterdeck/install.py', 'upgrade', 'fallback'),
                                    ('src/quarterdeck/install.py', 'upgrade', 'synchronize')]
