"""Shared fixtures: ship folders in temporary directories and a private tmux server."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def socket_parent():
    """Use the framework's ignored work/, including when these tests run in a linked worktree."""
    try:
        common = Path(subprocess.check_output(
            ['git', '-C', str(ROOT), 'rev-parse', '--path-format=absolute', '--git-common-dir'],
            text=True, stderr=subprocess.PIPE).strip()).resolve()
        project = common.parent
        ignored = subprocess.run(['git', '-C', str(project), 'check-ignore', '--quiet', '--',
                                  'work/.qd-test-sockets'], capture_output=True).returncode == 0
    except (OSError, subprocess.CalledProcessError) as error:
        raise pytest.UsageError(f'cannot verify framework socket anchor for {ROOT}') from error
    anchor = project / 'work'
    if (common.name != '.git' or not common.is_dir() or not ignored or anchor.resolve() != anchor
            or not (ROOT == project or ROOT.is_relative_to(project / 'work'))):
        raise pytest.UsageError(f'cannot verify framework ignored work/ socket anchor for {ROOT}')
    return anchor


@contextmanager
def socket_container(parent, prefix):
    """Own this exact empty container through setup failure; never erase a peer or replacement."""
    folder = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    failure = None
    try:
        yield folder
    except BaseException as error:
        failure = error
        raise
    finally:
        try:
            actual, original = folder.lstat(), os.fstat(descriptor)
            if (actual.st_dev, actual.st_ino) != (original.st_dev, original.st_ino):
                raise OSError('container identity changed')
            folder.rmdir()
        except OSError as error:
            detail = f'test socket container retained: {folder}: {error}'
            if failure is not None:
                failure.add_note(detail)
            else:
                pytest.fail(detail)
        finally:
            os.close(descriptor)


@pytest.fixture(scope='session', autouse=True)
def private_socket_root():
    """Even legacy named no-server probes must stay outside the user's socket directory."""
    previous = os.environ.get('TMUX_TMPDIR')
    parent = Path(previous).expanduser().resolve() if previous else socket_parent()
    parent.mkdir(parents=True, exist_ok=True)
    sample = parent / 'qd-suite-12345678'
    paths = [sample / 'qs-12345678/qd-pt-12345678/s-replacement',
             sample / f'tmux-{os.getuid()}/qd-test-9999999999-abcdef-replacement',
             sample / f'tmux-{os.getuid()}/qd-synthetic-attribution-no-server']
    longest = max(len(os.fsencode(path)) for path in paths)
    if longest >= 108:
        raise pytest.UsageError(f'test socket anchor {parent} needs {longest} bytes; native limit is 107; '
                                'choose a shorter project-owned TMUX_TMPDIR')
    with socket_container(parent, 'qd-suite-') as folder:
        os.environ['TMUX_TMPDIR'] = str(folder)
        try:
            yield folder
        finally:
            if previous is None:
                os.environ.pop('TMUX_TMPDIR', None)
            else:
                os.environ['TMUX_TMPDIR'] = previous
            native = folder / f'tmux-{os.getuid()}'
            try:
                native.rmdir()  # only an empty directory in this fresh owned test root
            except FileNotFoundError:
                pass


@pytest.fixture
def drydock(tmp_path: Path) -> Path:
    """A copy of ships/drydock in a temporary folder, not a repository."""
    folder = tmp_path / "ships" / "drydock"
    shutil.copytree(ROOT / "ships" / "drydock", folder)
    return folder


@pytest.fixture
def enterprise_ship(tmp_path: Path) -> Path:
    """The design's example ship, as its own repository with one commit."""
    folder = tmp_path / "ships" / "enterprise"
    folder.mkdir(parents=True)
    shutil.copy(FIXTURES / "ships" / "enterprise.toml", folder / "ship.toml")
    (folder / "orders.md").write_text("# Ship's orders: enterprise\n")
    git(folder, "init", "--quiet")
    git(folder, "config", "user.name", "test")
    git(folder, "config", "user.email", "t@example.org")
    git(folder, "add", ".")
    git(folder, "commit", "--quiet", "-m", "declare the ship")
    return folder


@pytest.fixture
def tmux():
    """A private tmux server for one test, never the user's."""
    from quarterdeck.tmux import Tmux

    server = Tmux.private(ROOT / 'work')
    try:
        yield server
    finally:
        if not server.close():
            pytest.fail(f'private tmux cleanup held: {server.held}')


def git(folder: Path, *args: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="test", GIT_AUTHOR_EMAIL="t@example.org",
               GIT_COMMITTER_NAME="test", GIT_COMMITTER_EMAIL="t@example.org")
    done = subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True, env=env, check=True)
    return done.stdout.strip()


@pytest.fixture(autouse=True)
def no_real_antigravity_usage(monkeypatch):
    """Service tests may read synthetic Google usage; never spend the operator's sign-in."""
    from datetime import datetime, timezone
    from quarterdeck.harness import antigravity
    from quarterdeck.harness.antigravity_quota import unknown

    monkeypatch.setattr(antigravity, "fetch_usage", lambda: unknown("synthetic usage unavailable", datetime.now(timezone.utc)))
