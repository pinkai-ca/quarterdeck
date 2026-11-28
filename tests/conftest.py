"""Shared fixtures: ship folders in temporary directories and a private tmux server."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.testrunner import layout, owned, resources, sessions, scratch, servers, socket_paths, socket_roots
from tests.testrunner.socket_paths import SOCKET_LIMIT, SOCKET_MARGIN, SESSION_PREFIX

OPENBLAS_AT_IMPORT = os.environ.get('OPENBLAS_NUM_THREADS')
ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
MARK_PATIENCE = 5.0  # seconds another controller may take between creating its mark and writing it
TEST_TICK = 0.1  # seconds: every test ship's declared [ship] tick, so the service passes quickly (production: 1.0)


@pytest.fixture
def retirement_clock(monkeypatch):
    """Keep retirement work counts independent of the time taken by filesystem operations."""
    from quarterdeck import post_retirement

    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(post_retirement, 'time', SimpleNamespace(monotonic=lambda: clock.now, time=time.time))
    return clock


def socket_project():
    """The canonical Git common project, shared by all its linked worktrees."""
    common = Path(subprocess.check_output(
        ['git', '-C', str(ROOT), 'rev-parse', '--path-format=absolute', '--git-common-dir'],
        text=True, stderr=subprocess.PIPE).strip())
    if common.name != '.git' or common.is_symlink() or not common.is_dir():
        raise OSError(f'Git common directory {common} is not a plain .git folder')
    if common.parent.lstat().st_uid != os.getuid():
        raise OSError(f'Git common project {common.parent} is not this user\'s directory')
    if common.parent.resolve() != common.parent:
        raise OSError(f'Git common project {common.parent} is reached through a link')
    return common.parent


def socket_parent():
    """A short project-owned socket root; inside a jail use its declared private anchor."""
    if os.environ.get('TESTRUNS_JAILED') == '1' and os.environ.get('TESTRUNS_SOCKETS'):
        anchor = Path(os.environ['TESTRUNS_SOCKETS'])
        anchor.mkdir(mode=0o700, parents=True, exist_ok=True)
        return anchor
    try:
        return socket_roots.ensure(socket_project(), layout.SOCKETS)
    except (OSError, subprocess.CalledProcessError, layout.Refused) as error:
        raise pytest.UsageError(f'cannot verify the project-owned socket anchor for {ROOT}: {error}') from error


def plain_temp(*, controller=True):
    """TMPDIR of plain pytest: owned memory storage, or the verified disk fallback .testruns/t. The controller marks it
    as the test tool's before any worker starts; a worker only reads the mark."""
    project = socket_project()
    memory = scratch.plain(project)
    if memory is not None:
        if controller:
            own_plain_temp(memory, os.open(memory, layout.DIRECTORY))
        return memory
    root = layout.Root(project)
    folder = project / layout.ROOT_NAME / layout.PLAIN_TEMP
    try:
        descriptor = root.child(layout.PLAIN_TEMP)
        if controller:
            own_plain_temp(folder, descriptor)
        else:
            os.close(descriptor)
    finally:
        root.close()
    return folder


def own_plain_temp(folder, descriptor):
    """Mark a plain run's folder once, as a tool run's scratch is marked: a ship inside a marked folder never opens the
    account's key file, whatever its environment (keyfile.path). A valid mark stays; anything else there ends the
    session, since an unmarked folder would read as a real ship's."""
    from quarterdeck import keyfile

    try:
        try:
            owned.mark(descriptor)
        except FileExistsError:
            deadline = time.monotonic() + MARK_PATIENCE  # another run's controller between creating and writing it
            while not keyfile.marked(Path(folder)) and time.monotonic() < deadline:
                time.sleep(0.01)
    finally:
        os.close(descriptor)
    if not keyfile.marked(Path(folder)):
        raise pytest.UsageError(f'{folder}: its {owned.MARKER} is not a valid owner mark; remove that file and run again')


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    """Direct pytest uses owned memory storage, retaining at most three failed tmp_path sessions. A run of tests/testrun.py has
    already given the run its own TMPDIR, HOME and caches in its scratch, and is left as it is."""
    try:
        config._qd_memory_limit = resources.apply_from_environment()
    except (ValueError, RuntimeError) as error:
        raise pytest.UsageError(str(error)) from error
    from quarterdeck import keyfile
    os.environ[keyfile.TEST_CONFIG] = '/nonexistent/quarterdeck-test-config'  # never the account's key file
    # Native tmux must not infer the caller's live server or pane.
    os.environ.pop('TMUX', None)
    os.environ.pop('TMUX_PANE', None)
    config._qd_synthetic_memory_checks = set()
    if os.environ.get('TESTRUNS_RUN'):
        return
    folder = plain_temp(controller=not hasattr(config, 'workerinput'))
    os.environ['TMPDIR'] = str(folder)
    config._qd_scratch_path = folder
    extra = [Path(config.option.basetemp).absolute()] if config.option.basetemp else []
    extra = [path for path in extra if scratch.in_memory(path)]
    config._qd_scratch_monitor = (scratch.Monitor(folder, extra=extra)
                                  if scratch.in_memory(folder) else None)
    tempfile.tempdir = None  # gettempdir() reads TMPDIR again


def memory_roots():
    """Read the shared-memory root names and identities; the census never removes anything."""
    prefix = f'qd-{os.getuid()}-'
    found = {}
    with os.scandir('/dev/shm') as entries:
        for entry in entries:
            if entry.name.startswith(prefix):
                try:
                    found[entry.name] = layout.identity(entry.stat(follow_symlinks=False))
                except FileNotFoundError:
                    pass  # a concurrent test finished its own teardown
    return found


def pytest_sessionstart(session):
    config = session.config
    if not hasattr(config, 'workerinput'):
        config._qd_memory_roots_before = memory_roots()
    monitor = getattr(config, '_qd_scratch_monitor', None)
    if monitor is not None and not hasattr(config, 'workerinput'):
        config._qd_scratch_observer = scratch.PlainMonitor(monitor.paths[0], monitor.paths[1:])


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    if hasattr(session.config, 'workerinput'):
        session.config.workeroutput['qd_synthetic_memory_checks'] = sorted(
            session.config._qd_synthetic_memory_checks)
        return
    observer = getattr(session.config, '_qd_scratch_observer', None)
    refusals = []
    if observer is not None:
        try:
            refusal = observer.finish()['refusal']
        except (layout.Refused, OSError, ValueError, subprocess.TimeoutExpired) as error:
            refusal = str(error)
        if refusal:
            refusals.append(refusal)
    before = getattr(session.config, '_qd_memory_roots_before', None)
    if before is not None:
        try:
            after = memory_roots()
            reporter = session.config.pluginmanager.get_plugin('terminalreporter')
            if reporter:
                reporter.write_line(f'pytest shared-memory roots: before={len(before)}, after={len(after)}; '
                                    'synthetic projects checked separately; census removes nothing')
            remaining = []
            for value in sorted(session.config._qd_synthetic_memory_checks):
                path = Path(value)
                try:
                    path.lstat()
                except FileNotFoundError:
                    continue
                remaining.append(value)
            if remaining:
                refusals.append('synthetic project memory roots remain after the suite: '
                                + ', '.join(remaining) + '; reported only, nothing removed')
        except OSError as error:
            refusals.append(f'cannot complete shared-memory root census: {error}')
    if refusals:
        session.exitstatus = pytest.ExitCode.USAGE_ERROR
        reporter = session.config.pluginmanager.get_plugin('terminalreporter')
        if reporter:
            for refusal in refusals:
                reporter.write_line('pytest: refused: ' + refusal, red=True)


def pytest_report_header(config):
    startup = (f'OPENBLAS_NUM_THREADS={OPENBLAS_AT_IMPORT!r} observed at conftest import; '
               'earlier plugin-import timing is unverified')
    if OPENBLAS_AT_IMPORT != '1':
        startup += '; required: set OPENBLAS_NUM_THREADS=1 before starting pytest; setting it now is too late'
    working = f'pytest working files: {getattr(config, "_qd_scratch_path", os.environ.get("TMPDIR"))}'
    return [working, f'test memory limit: RLIMIT_AS soft=hard={config._qd_memory_limit} bytes (children inherit)', startup]


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node, error):
    # xdist only receives workeroutput when the worker reaches session finish.
    output = getattr(node, 'workeroutput', None)
    if output is not None:
        node.config._qd_synthetic_memory_checks.update(output.get('qd_synthetic_memory_checks', []))


class SyntheticMemoryRoots:
    """Derived roots of this test's temporary projects, never roots found by a shared-folder scan."""

    def __init__(self, tmp_path, checks):
        self.tmp_path = tmp_path
        self.checks = checks
        self.bases = set(scratch.candidates()) | {tmp_path}

    def projects(self):
        projects, local_roots = set(), []
        primary = self.tmp_path / 'p'
        if primary.is_dir() and not primary.is_symlink():
            projects.add(primary)
        for folder, dirs, files in os.walk(self.tmp_path, followlinks=False):
            path = Path(folder)
            if '.testruns' in dirs:
                projects.add(path)
            if 'pyvenv.cfg' in files:
                dirs[:] = []  # installed environments contain no separate synthetic projects
                continue
            for name in dirs:
                if name.startswith(f'qd-{os.getuid()}-'):
                    local_roots.append(path / name)
            dirs[:] = [name for name in dirs if name not in ('.git', '.testruns')
                       and not name.startswith(f'qd-{os.getuid()}-')]
        names = {scratch.project_name(project) for project in projects}
        for root in local_roots:
            if root.name in names:
                self.bases.add(root.parent)
        return projects

    def open_root(self, project, base):
        """Open the derived root by name, never following a link; a shared base must be tmpfs or ramfs.

        A base inside this test's own tmp_path stands in for memory. Where it is memory it opens through the
        tool's own scratch.memory_root, as every base does. Where the basetemp is on disk, as on a runner
        without a large tmpfs, it opens the same way without asking the mount, since the test made it there.
        Every other check of the teardown is the same for both."""
        if not base.is_relative_to(self.tmp_path):
            # A nested CI-layout controller can create real memory scratch even when its parent models disk only.
            return scratch.memory_root(project, base, create=False, physical=True)
        if not base.is_absolute() or base.resolve() != base:
            raise layout.Refused(f'{base}: memory storage base is not an absolute plain path')
        fd = os.open(base, layout.DIRECTORY)
        try:
            if not scratch.filesystem(fd):
                path = base / scratch.project_name(project)
                return path, layout.open_child(fd, path.name, path, create=False, device=os.fstat(fd).st_dev)
        finally:
            os.close(fd)
        return scratch.memory_root(project, base, create=False)

    def finish(self):
        failures = []
        for project in sorted(self.projects()):
            name = scratch.project_name(project)
            for base in sorted(self.bases):
                path = base / name
                self.checks.add(str(path))
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    continue
                try:
                    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                            or stat.S_IMODE(info.st_mode) != 0o700):
                        raise layout.Refused(f'{path}: synthetic memory root is not our private plain directory')
                    where, descriptor = self.open_root(project, base)
                    try:
                        expected = layout.identity(os.fstat(descriptor))
                        if layout.identity(info) != expected:
                            raise layout.Refused(f'{path}: synthetic memory root changed before teardown')
                        parent = os.open(base, layout.DIRECTORY)
                        try:
                            owned.remove(parent, name, expected, where)
                        finally:
                            os.close(parent)
                    finally:
                        os.close(descriptor)
                except (OSError, layout.Refused) as error:
                    failures.append(str(error))
        if failures:
            pytest.fail('synthetic memory cleanup held: ' + '; '.join(failures))


@pytest.fixture(autouse=True)
def synthetic_memory_roots(tmp_path, request):
    roots = SyntheticMemoryRoots(tmp_path, request.config._qd_synthetic_memory_checks)
    yield roots
    roots.finish()


@pytest.fixture
def monkeypatch(synthetic_memory_roots):
    """Undo test patches before exact-root teardown uses filesystem and scratch functions."""
    with pytest.MonkeyPatch.context() as patch:
        yield patch


@pytest.fixture(autouse=True)
def per_test_tmpdir(tmp_path, monkeypatch):
    """Every child starts with temporary files inside this test's lifetime, including tool runs."""
    monkeypatch.setenv('TMPDIR', str(tmp_path))
    monkeypatch.setattr(tempfile, 'tempdir', None)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    if call.excinfo is not None and call.excinfo.errisinstance(MemoryError):
        call.excinfo.value.add_note(f'test memory limit: RLIMIT_AS {item.config._qd_memory_limit} bytes; '
                                  'allocation refused within this test process')
    yield


def socket_container_entries(descriptor):
    """Failure diagnostics from the held directory: names and lstat metadata, never contents."""
    now = time.time_ns()
    labels = {stat.S_IFDIR: 'directory', stat.S_IFREG: 'regular', stat.S_IFSOCK: 'socket',
              stat.S_IFLNK: 'symlink', stat.S_IFIFO: 'fifo', stat.S_IFCHR: 'character', stat.S_IFBLK: 'block'}
    remaining = []
    with os.scandir(descriptor) as entries:
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
                remaining.append(dict(name=entry.name, lstat_type=labels.get(stat.S_IFMT(info.st_mode), 'unknown'),
                                      mtime_age_seconds=round((now - info.st_mtime_ns) / 1_000_000_000, 6)))
            except OSError as error:
                remaining.append(dict(name=entry.name, lstat_type='unavailable', mtime_age_seconds=None,
                                      error=type(error).__name__))
    return sorted(remaining, key=lambda entry: entry['name'])


@contextmanager
def socket_container(parent, prefix, *, record=False):
    """Own this exact empty container through setup failure; never erase a peer or replacement."""
    folder = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    failure = None
    token = None
    try:
        token = sessions.register(parent, folder, descriptor) if record else None
        yield folder
    except BaseException as error:
        failure = error
        raise
    finally:
        try:
            actual, original = folder.lstat(), os.fstat(descriptor)
            if (actual.st_dev, actual.st_ino) != (original.st_dev, original.st_ino):
                raise OSError('container identity changed')
            if token is not None:
                sessions.unmark_empty(descriptor, token)
            folder.rmdir()
        except OSError as error:
            detail = f'test socket container retained: {folder}: {error}'
            try:
                detail += f'; remaining entries: {socket_container_entries(descriptor)!r}'
            except OSError as unavailable:
                detail += f'; remaining entry metadata unavailable: {type(unavailable).__name__}'
            if failure is not None:
                failure.add_note(detail)
            else:
                pytest.fail(detail)
        finally:
            os.close(descriptor)


@pytest.fixture(scope='session', autouse=True)
def record_private_servers(private_socket_root):
    """Persist creator-verified births for interrupted-run cleanup, without changing production code."""
    from quarterdeck.tmux_socket import OwnedSocket

    original = OwnedSocket.record

    def record(owner, held, query):
        original(owner, held, query)
        servers.record(owner)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(OwnedSocket, 'record', record)
        yield


@pytest.fixture(scope='session', autouse=True)
def private_socket_root():
    """Even legacy named no-server probes must stay outside the user's socket directory."""
    previous = os.environ.get('TMUX_TMPDIR')
    parent = Path(previous).expanduser().resolve() if previous else socket_parent()
    parent.mkdir(parents=True, exist_ok=True)
    try:
        socket_paths.check(parent)
    except layout.Refused as error:
        raise pytest.UsageError(str(error)) from error
    with socket_container(parent, SESSION_PREFIX, record=sessions.automatic(parent)) as folder:
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


@pytest.fixture(autouse=True)
def no_default_tmux_server(monkeypatch):
    """A ship's search for tmux's default server, where it ran before it had its own, finds none in a test:
    the person's own server, in /tmp, is never read. A test of that search names its own private stand-in."""
    from quarterdeck import tmux as tmuxmod

    monkeypatch.setattr(tmuxmod, '_default_sockets', lambda: [])


class KeyFile:
    """A scratch key file, the one this test's ships read (keyfile.TEST_CONFIG)."""

    def __init__(self, path):
        self.path = path

    def write(self, **keys):
        """Keys by name, an underscore in the argument standing for the name's hyphen."""
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        self.path.write_text(''.join(f'{name.replace("_", "-")} = "{value}"\n' for name, value in keys.items()))
        self.path.chmod(0o600)
        return self.path


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    from quarterdeck import keyfile

    config = tmp_path / 'scratch-config'
    monkeypatch.setenv(keyfile.TEST_CONFIG, str(config))
    return KeyFile(config / 'quarterdeck' / 'keys.toml')


@pytest.fixture
def synthetic_disk_room(monkeypatch):
    """Opt in when disk pressure is outside the test's subject."""
    from quarterdeck import watch

    monkeypatch.setattr(watch, 'disks', lambda paths: [])


def synthetic_grok(folder: Path, monkeypatch) -> Path:
    """A synthetic ``grok`` file, never executed, alone on the PATH a seat's program is looked for on: a grok
    launch names the one program it selects, so a test of its arguments must not depend on the machine's grok."""
    from quarterdeck import seat as seat_module

    bin_dir = folder / 'synthetic-grok-bin'
    bin_dir.mkdir(exist_ok=True)
    program = bin_dir / 'grok'
    program.write_text('Synthetic grok program; never executed.\n')
    program.chmod(0o700)
    monkeypatch.setattr(seat_module, 'program_path', lambda: str(bin_dir))
    return program


@pytest.fixture
def synthetic_grok_program(tmp_path, monkeypatch) -> Path:
    return synthetic_grok(tmp_path, monkeypatch)


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
