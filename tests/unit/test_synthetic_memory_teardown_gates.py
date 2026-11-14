"""Gates for the synthetic memory teardown: it removes nothing but the test's own derived root.

Every root here is a real directory under a private base, the test's own TMPDIR candidate, never /dev/shm itself;
the teardown also looks at /dev/shm and /tmp by name and finds nothing of these projects there. Two things are
stood in for, each named where it is done: the mount check reports tmpfs for the private base alone, since the
base may sit on disk (GitHub's runner has no large tmpfs) and the mount is not what these gates prove; and a root
of another owner is shown to the teardown as such through its lstat, since making one needs privilege. The
opening, the identities, the modes, the links and the removal are the real ones.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import conftest
from tests.conftest import SyntheticMemoryRoots
from tests.testrunner import layout, scratch


@pytest.fixture
def base(tmp_path, monkeypatch):
    """The test's own TMPDIR candidate, admitted as working storage; every other base is read as it is."""
    assert os.environ['TMPDIR'] == str(tmp_path) and Path(tempfile.gettempdir()) == tmp_path
    assert tmp_path in scratch.candidates()
    real, own = scratch.filesystem, layout.identity(os.stat(tmp_path))
    monkeypatch.setattr(scratch, 'filesystem', lambda fd: 'tmpfs' if layout.identity(os.fstat(fd)) == own else real(fd))
    yield tmp_path
    for name in os.listdir(tmp_path):  # this test's own roots, whatever a gate left standing
        if name.startswith(f'qd-{os.getuid()}-'):
            path = tmp_path / name
            if path.is_symlink():
                path.unlink()
            else:
                path.chmod(0o700)
                shutil.rmtree(path)


def project(tmp_path: Path, name: str = 'p') -> Path:
    folder = tmp_path / name
    (folder / '.testruns').mkdir(parents=True)
    return folder


def root(base: Path, folder: Path, mode: int = 0o700) -> Path:
    path = base / scratch.project_name(folder)
    path.mkdir(mode=0o700)
    (path / 'runs').mkdir()
    (path / 'runs' / 'kept-product.txt').write_text('synthetic product')
    path.chmod(mode)
    return path


def held(cleanup: SyntheticMemoryRoots) -> str:
    with pytest.raises(pytest.fail.Exception) as failed:
        cleanup.finish()
    return str(failed.value)


def intact(path: Path) -> bool:
    return (path / 'runs' / 'kept-product.txt').read_text() == 'synthetic product'


def nothing_shared(*folders: Path) -> None:
    for folder in folders:
        for shared in (Path('/dev/shm'), Path('/tmp')):
            assert not os.path.lexists(shared / scratch.project_name(folder))


def test_the_own_root_is_removed_and_its_project_kept(tmp_path, base):
    """The control: the test's own private root is removed through the real opening and removal."""
    p = project(tmp_path)
    own = root(base, p)
    checks = set()
    SyntheticMemoryRoots(tmp_path, checks).finish()
    assert not os.path.lexists(own) and str(own) in checks and (p / '.testruns').is_dir()
    nothing_shared(p)


def test_a_root_of_another_owner_at_the_derived_name_is_left_alone_and_named(tmp_path, base, monkeypatch):
    p = project(tmp_path)
    other = root(base, p)
    real = Path.lstat

    def lstat(self):  # stand-in: the teardown is shown this one root as another user's
        info = real(self)
        if self == other:
            fields = list(info)
            fields[stat.ST_UID] = os.getuid() + 1
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(Path, 'lstat', lstat)
    message = held(SyntheticMemoryRoots(tmp_path, set()))
    assert str(other) in message and 'not our private plain directory' in message
    assert other.is_dir() and intact(other)
    nothing_shared(p)


def test_a_symlinked_root_at_the_derived_name_is_never_followed_or_removed(tmp_path, base):
    p = project(tmp_path)
    target = tmp_path / 'elsewhere'
    target.mkdir(mode=0o700)
    (target / 'runs').mkdir()
    (target / 'runs' / 'kept-product.txt').write_text('synthetic product')
    link = base / scratch.project_name(p)
    link.symlink_to(target, target_is_directory=True)
    message = held(SyntheticMemoryRoots(tmp_path, set()))
    assert str(link) in message
    assert link.is_symlink() and os.readlink(link) == str(target)
    assert target.is_dir() and intact(target) and stat.S_IMODE(target.stat().st_mode) == 0o700
    nothing_shared(p)


@pytest.mark.parametrize('when', ['before the root is opened', 'after the root is opened'])
def test_a_root_replaced_between_derive_and_remove_is_not_removed(tmp_path, base, monkeypatch, when):
    """The derived root is swapped for another directory of the same owner and mode: the descriptor and the
    identity hold, so neither the replacement nor the original is removed."""
    p = project(tmp_path)
    original = root(base, p)
    aside = tmp_path / 'original-moved-aside'
    real = scratch.memory_root

    def swap():
        original.rename(aside)
        original.mkdir(mode=0o700)
        (original / 'replacement.txt').write_text('not the test root')

    def memory_root(folder, where, *, create=True, name=None):
        if when == 'before the root is opened':
            swap()
        opened = real(folder, where, create=create, name=name)
        if when == 'after the root is opened':
            swap()
        return opened

    monkeypatch.setattr(scratch, 'memory_root', memory_root)
    message = held(SyntheticMemoryRoots(tmp_path, set()))
    assert str(original) in message and ('changed before teardown' in message or 'replaced' in message)
    assert (original / 'replacement.txt').read_text() == 'not the test root'
    assert intact(aside)
    nothing_shared(p)


@pytest.mark.parametrize('mode', [0o720, 0o770, 0o702, 0o707, 0o777, 0o750])
def test_a_root_that_is_not_private_is_refused(tmp_path, base, mode):
    """Group- or other-writable, or readable by others at all: only mode 0700 is the test's own root."""
    p = project(tmp_path)
    shared = root(base, p, mode)
    message = held(SyntheticMemoryRoots(tmp_path, set()))
    assert str(shared) in message
    assert stat.S_IMODE(shared.stat().st_mode) == mode and intact(shared)
    nothing_shared(p)


def test_a_root_of_another_sessions_project_under_the_same_base_is_left_alone(tmp_path, base):
    """Another session's project lies outside this test; its root shares the base and the uid prefix."""
    p = project(tmp_path)
    own = root(base, p)
    elsewhere = tmp_path.parent / f'{tmp_path.name}-another-session' / 'p'
    theirs = root(base, elsewhere)  # its project folder is never made: only its derived name is known
    checks = set()
    SyntheticMemoryRoots(tmp_path, checks).finish()
    assert not os.path.lexists(own)
    assert theirs.is_dir() and intact(theirs) and str(theirs) not in checks
    nothing_shared(p, elsewhere)


class Reporter:
    def __init__(self):
        self.lines = []

    def write_line(self, line, **markup):
        self.lines.append(line)


def finish_session(checks: set) -> SimpleNamespace:
    """The controller's end-of-session hook, on a stand-in session holding this session's checks."""
    reporter = Reporter()
    config = SimpleNamespace(_qd_memory_roots_before={}, _qd_synthetic_memory_checks=checks,
                             pluginmanager=SimpleNamespace(get_plugin=lambda name: reporter))
    session = SimpleNamespace(config=config, exitstatus=pytest.ExitCode.OK)
    conftest.pytest_sessionfinish(session, pytest.ExitCode.OK)
    return SimpleNamespace(session=session, lines=reporter.lines)


def test_the_end_check_fails_naming_a_leftover_root_of_the_sessions_project_and_removes_nothing(tmp_path, base):
    p, second = project(tmp_path), project(tmp_path, 'second-project')
    left = root(base, p, 0o720)  # teardown refuses it, so it stays
    gone = root(base, second)
    elsewhere = tmp_path.parent / f'{tmp_path.name}-another-session' / 'p'
    theirs = root(base, elsewhere)
    checks = set()
    held(SyntheticMemoryRoots(tmp_path, checks))
    assert str(left) in checks and str(gone) in checks and not os.path.lexists(gone)
    end = finish_session(checks)
    assert end.session.exitstatus == pytest.ExitCode.USAGE_ERROR
    refusals = [line for line in end.lines if line.startswith('pytest: refused: ')]
    assert len(refusals) == 1 and str(left) in refusals[0] and 'nothing removed' in refusals[0]
    assert str(gone) not in refusals[0] and str(theirs) not in refusals[0]
    assert stat.S_IMODE(left.stat().st_mode) == 0o720 and intact(left)
    assert theirs.is_dir() and intact(theirs)
    nothing_shared(p, second, elsewhere)


def test_the_end_check_passes_when_every_checked_root_is_gone(tmp_path, base):
    p = project(tmp_path)
    root(base, p)
    checks = set()
    SyntheticMemoryRoots(tmp_path, checks).finish()
    end = finish_session(checks)
    assert end.session.exitstatus == pytest.ExitCode.OK
    assert not [line for line in end.lines if line.startswith('pytest: refused: ')]
