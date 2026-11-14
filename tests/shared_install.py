"""Worker-local, immutable wheel/install fixtures; mutable tests clone their own prefix."""
from dataclasses import dataclass
import base64
import csv
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from types import MappingProxyType
import zipfile

import pytest


@dataclass(frozen=True)
class Proof:
    returncode: int
    stdout: str
    stderr: str


def command(*argv, cwd, env):
    done = subprocess.run([str(arg) for arg in argv], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=180)
    assert done.returncode == 0, (argv, done.returncode, done.stdout, done.stderr)
    return Proof(done.returncode, done.stdout, done.stderr)


def child_environment(folder):
    env = {name: os.environ[name] for name in ('PATH', 'LANG', 'LC_ALL', 'TERM') if name in os.environ}
    for name in ('HOME', 'TMPDIR', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME',
                 'XDG_STATE_HOME', 'XDG_RUNTIME_DIR', 'PIP_CACHE_DIR'):
        path = folder / name.lower()
        path.mkdir(mode=0o700, parents=True)
        env[name] = str(path)
    env.update(PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1', PIP_NO_INDEX='1',
               PIP_NO_INPUT='1', PIP_DISABLE_PIP_VERSION_CHECK='1',
               GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
    return env


def permissions(folder, writable):
    for path in [folder, *folder.rglob('*')]:
        if not path.is_symlink():
            mode = stat.S_IMODE(path.stat().st_mode)
            path.chmod(mode | stat.S_IWUSR if writable else mode & ~0o222)


def fingerprint(folder):
    return {str(p.relative_to(folder)): (('link', os.readlink(p)) if p.is_symlink()
            else ('file', hashlib.sha256(p.read_bytes()).hexdigest()))
            for p in folder.rglob('*') if p.is_symlink() or p.is_file()}


def copy_environment(seed, destination):
    """Copy an installed prefix, preserving symlinks and rebinding generated entry scripts to the copy."""
    assert not destination.exists()
    shutil.copytree(seed, destination, symlinks=True)
    permissions(destination, True)
    old, new = os.fsencode(seed), os.fsencode(destination)
    rewritten = set()
    for script in (destination / 'bin').iterdir():
        if script.is_file() and not script.is_symlink():
            data = script.read_bytes()
            if old in data:
                script.write_bytes(data.replace(old, new))
                rewritten.add(script.resolve())
    # Console scripts have hashes in their distribution RECORD; keep the relocated copy valid.
    for record in destination.glob('lib/python*/site-packages/*.dist-info/RECORD'):
        with record.open(newline='') as handle:
            rows = list(csv.reader(handle))
        changed = False
        for row in rows:
            target = (record.parent.parent / row[0]).resolve()
            if target in rewritten:
                data = target.read_bytes()
                row[1] = 'sha256=' + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()
                row[2] = str(len(data))
                changed = True
        if changed:
            with record.open('w', newline='') as handle:
                csv.writer(handle).writerows(rows)
    return destination / 'bin/python'


@dataclass(frozen=True)
class Wheel:
    path: Path
    source: Path
    build_environment: Path
    source_files: object
    members: object
    tracked: tuple
    ref: str
    proof: Proof


@dataclass(frozen=True)
class Installed:
    prefix: Path
    proof: Proof
    wheel: Wheel | None = None


@pytest.fixture(scope='session')
def shared_wheel(tmp_path_factory):
    from tests.integration.test_upgrade_resources import ROOT, RESOURCE_ROOTS, archive, environment, git
    folder = tmp_path_factory.mktemp('shared-wheel')
    env = child_environment(folder / 'child')
    ref = git(ROOT, 'rev-parse', 'HEAD')
    source, build = folder / 'source', folder / 'build-env'
    archive(ref, source, RESOURCE_ROOTS)
    inputs = {p.relative_to(source).as_posix(): p.read_bytes() for p in source.rglob('*') if p.is_file()}
    python = environment(build, env=env)
    output = folder / 'wheels'
    output.mkdir()
    proof = command(python, '-I', '-c',
                    'from setuptools.build_meta import build_wheel; import sys; build_wheel(sys.argv[1])',
                    output, cwd=source, env=env)
    wheel, = output.glob('*.whl')
    with zipfile.ZipFile(wheel) as built:
        members = {name: built.read(name) for name in built.namelist() if not name.endswith('/')}
    shutil.rmtree(source)
    shutil.rmtree(build)
    assert not source.exists() and not build.exists()
    saved = fingerprint(output)
    permissions(output, False)
    try:
        yield Wheel(wheel, source, build, MappingProxyType(inputs), MappingProxyType(members),
                    tuple(git(ROOT, 'ls-tree', '-r', '--name-only', ref).splitlines()), ref, proof)
    finally:
        try:
            assert fingerprint(output) == saved, 'shared wheel changed'
        finally:
            permissions(output, True)


@pytest.fixture(scope='session')
def shared_wheel_install(tmp_path_factory, shared_wheel):
    folder = tmp_path_factory.mktemp('shared-wheel-install')
    env = child_environment(folder / 'child')
    prefix = folder / 'installed'
    command(sys.executable, '-I', '-m', 'venv', prefix, cwd=folder, env=env)
    proof = command(prefix / 'bin/python', '-I', '-m', 'pip', 'install', '--no-index', '--no-deps',
                    shared_wheel.path, cwd=folder, env=env)
    saved = fingerprint(prefix)
    permissions(prefix, False)
    try:
        yield Installed(prefix, proof, shared_wheel)
    finally:
        try:
            assert fingerprint(prefix) == saved, 'shared wheel install changed'
        finally:
            permissions(prefix, True)


@pytest.fixture(scope='session')
def shared_legacy_install(tmp_path_factory):
    from tests.integration.test_upgrade_resources import PREDECESSOR, archive, environment
    folder = tmp_path_factory.mktemp('shared-legacy-install')
    env = child_environment(folder / 'child')
    source, prefix = folder / 'source', folder / 'installed'
    archive(PREDECESSOR, source, ('standing-orders', 'ships/drydock'))
    python = environment(prefix, env=env)
    proof = command(python, '-m', 'pip', 'install', '--quiet', '--no-deps', '--no-build-isolation',
                    source, cwd=folder, env=env)
    shutil.rmtree(source)
    saved = fingerprint(prefix)
    permissions(prefix, False)
    try:
        yield Installed(prefix, proof)
    finally:
        try:
            assert fingerprint(prefix) == saved, 'shared historical install changed'
        finally:
            permissions(prefix, True)
