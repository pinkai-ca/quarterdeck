"""Shared fixtures: ship folders in temporary directories and a private tmux server."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.testrunner import layout, resources

OPENBLAS_AT_IMPORT = os.environ.get('OPENBLAS_NUM_THREADS')
ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
TEST_TICK = 0.1  # seconds: every test ship's declared [ship] tick, so the service passes quickly (production: 1.0)
SOCKET_LIMIT = 107   # bytes of a Unix socket path, without its terminating zero
SOCKET_MARGIN = 12   # kept free below the limit on the deepest socket path the suite generates
SESSION_PREFIX = 'u-'  # the session's container under the anchor: short, so the margin holds


def socket_parent():
    """The fixed short anchor ``<project>/.testruns/s``, the project being the parent of the Git common
    directory: one place for the checkout, every worktree and every run checkout of tests/testrun.py."""
    try:
        common = Path(subprocess.check_output(
            ['git', '-C', str(ROOT), 'rev-parse', '--path-format=absolute', '--git-common-dir'],
            text=True, stderr=subprocess.PIPE).strip())
        if common.name != '.git' or common.is_symlink() or not common.is_dir():
            raise OSError(f'Git common directory {common} is not a plain .git folder')
        root = layout.Root(common.parent)  # verified, private, ignoring itself, proven ignored by Git
        try:
            os.close(root.child(layout.SOCKETS))
        finally:
            root.close()
    except (OSError, subprocess.CalledProcessError, layout.Refused) as error:
        raise pytest.UsageError(f'cannot verify the project-owned socket anchor for {ROOT}: {error}') from error
    anchor = common.parent / layout.ROOT_NAME / layout.SOCKETS
    if anchor.resolve() != anchor:
        raise pytest.UsageError(f'cannot verify the project-owned socket anchor {anchor}: reached through a link')
    return anchor


def plain_temp():
    """TMPDIR of a plain pytest run: ``<project>/.testruns/t``, verified like the socket anchor."""
    anchor = socket_parent()
    root = layout.Root(anchor.parent.parent)
    try:
        os.close(root.child(layout.PLAIN_TEMP))
    finally:
        root.close()
    return anchor.parent / layout.PLAIN_TEMP


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    """Pytest run directly puts its temporary products, basetemp included, under .testruns/t, where
    pyproject's tmp_path_retention_count keeps only the last three sessions. A run of tests/testrun.py has
    already given the run its own TMPDIR, HOME and caches in its scratch, and is left as it is."""
    try:
        config._qd_memory_limit = resources.apply_from_environment()
    except (ValueError, RuntimeError) as error:
        raise pytest.UsageError(str(error)) from error
    # Native tmux must not infer the caller's live server or pane.
    os.environ.pop('TMUX', None)
    os.environ.pop('TMUX_PANE', None)
    if os.environ.get('TESTRUNS_RUN'):
        return
    os.environ['TMPDIR'] = str(plain_temp())
    tempfile.tempdir = None  # gettempdir() reads TMPDIR again


def pytest_report_header(config):
    startup = (f'OPENBLAS_NUM_THREADS={OPENBLAS_AT_IMPORT!r} observed at conftest import; '
               'earlier plugin-import timing is unverified')
    if OPENBLAS_AT_IMPORT != '1':
        startup += '; required: set OPENBLAS_NUM_THREADS=1 before starting pytest; setting it now is too late'
    return [f'test memory limit: RLIMIT_AS soft=hard={config._qd_memory_limit} bytes (children inherit)', startup]


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    if call.excinfo is not None and call.excinfo.errisinstance(MemoryError):
        call.excinfo.value.add_note(f'test memory limit: RLIMIT_AS {item.config._qd_memory_limit} bytes; '
                                  'allocation refused within this test process')
    yield


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
    sample = parent / (SESSION_PREFIX + '12345678')
    generated = [sample / 'qs-12345678/qd-pt-12345678/s-replacement']  # the deepest socket the suite makes
    legacy = [sample / f'tmux-{os.getuid()}/qd-test-9999999999-abcdef-replacement',
              sample / f'tmux-{os.getuid()}/qd-synthetic-attribution-no-server']
    deepest = max(len(os.fsencode(path)) for path in generated)
    longest = max(len(os.fsencode(path)) for path in legacy)
    if deepest + SOCKET_MARGIN > SOCKET_LIMIT or longest > SOCKET_LIMIT:
        raise pytest.UsageError(f'test socket anchor {parent} gives sockets of {deepest} bytes (named probes '
                                f'{longest}); the native limit is {SOCKET_LIMIT} and the suite keeps a margin '
                                f'of {SOCKET_MARGIN}; the project path is too long for its .testruns/s anchor')
    with socket_container(parent, SESSION_PREFIX) as folder:
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
def synthetic_disk_room(monkeypatch):
    """Opt in when disk pressure is outside the test's subject."""
    from quarterdeck import watch

    monkeypatch.setattr(watch, 'disks', lambda paths: [])


@pytest.fixture
def drydock(tmp_path: Path) -> Path:
    """A copy of ships/drydock in a temporary folder, not a repository."""
    from quarterdeck import ship as shipmod

    folder = tmp_path / "ships" / "drydock"
    shutil.copytree(ROOT / "ships" / "drydock", folder)
    file = folder / "ship.toml"
    file.write_text(shipmod.edit(file.read_text(), "ship", "tick", TEST_TICK))  # the copy only; ships/ is unchanged
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

    server = Tmux.private(socket_parent())
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
