"""Ordinary storage selection, measurement and recovery, with synthetic projects and small files."""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.testrunner import clean, gitops, owned, scratch
from tests.testrunner.layout import RESULTS, RUNS, Refused, Root, identity, new_run_id


@pytest.fixture
def project(tmp_path, monkeypatch):
    project = tmp_path / 'synthetic-project'
    project.mkdir()
    gitops.git(project, 'init', '-q')
    (project / '.gitignore').write_text('.testruns/\n')
    memory = tmp_path / 'memory'
    memory.mkdir()
    # Selection and admission both see only this stand-in memory folder, never the machine's real memory.
    monkeypatch.setattr(scratch, 'choose', lambda: (memory, 'tmpfs'))
    monkeypatch.setattr(scratch, 'choices', lambda bound=scratch.BOUND: iter([(memory, 'tmpfs')]))
    monkeypatch.setattr(scratch, 'filesystem', lambda fd: 'tmpfs')
    device, measure = memory.stat().st_dev, os.fstatvfs

    def room(fd):  # ample room on the stand-in's device, measured truthfully elsewhere
        if os.fstat(fd).st_dev == device:
            return SimpleNamespace(f_bavail=64 * 1024 ** 3, f_frsize=1, f_flag=0)
        return measure(fd)
    monkeypatch.setattr(os, 'fstatvfs', room)
    root = Root(project)
    try:
        yield root
    finally:
        root.close()


def test_candidates_follow_requested_order(monkeypatch, tmp_path):
    assert scratch.candidates({'TMPDIR': str(tmp_path)}) == [tmp_path, Path('/dev/shm'), Path('/tmp')]
    assert scratch.candidates({'TMPDIR': '/tmp'}) == [Path('/tmp'), Path('/dev/shm')]


def test_filesystem_type_comes_from_statfs(tmp_path):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert scratch.filesystem(fd) in (None, 'tmpfs', 'ramfs')
    finally:
        os.close(fd)


def test_selection_uses_first_memory_candidate_with_space(monkeypatch, tmp_path):
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    monkeypatch.setattr(scratch, 'candidates', lambda: [first, second])
    calls = iter((None, 'tmpfs'))
    monkeypatch.setattr(scratch, 'filesystem', lambda fd: next(calls))
    assert scratch.choose(bound=1) == (second, 'tmpfs')


def test_no_usable_memory_is_a_disk_fallback(project, monkeypatch, capsys):
    monkeypatch.setattr(scratch, 'choose', lambda: None)
    monkeypatch.setattr(scratch, 'choices', lambda bound=scratch.BOUND: iter(()))
    run = new_run_id()
    result = scratch.prepare(project, run)
    assert result['kind'] == 'disk'
    assert 'room for a 4 GiB reservation plus the 8 GiB margin' in result['reason']
    assert scratch.path(project, run) == project.path / RUNS / run
    assert scratch.record(project, run) is None
    assert 'working files on disk' in capsys.readouterr().out


def test_space_lost_at_recheck_falls_back_without_a_reservation(project, monkeypatch):
    monkeypatch.setattr(os, 'fstatvfs', lambda fd: SimpleNamespace(f_bavail=0, f_frsize=4096))
    run = new_run_id()
    assert scratch.prepare(project, run)['kind'] == 'disk'
    assert scratch.pending(project) == []


def make_run(root):
    run = new_run_id()
    selected = scratch.prepare(root, run)
    fd = scratch.child(root, run)
    state = {'run': run, 'change': 'synthetic-change', 'scratch': identity(os.fstat(fd)),
             'owner': owned.mark(fd)}
    clean.record(root, run, state)
    os.close(fd)
    return run, selected, state


def test_memory_scratch_is_separate_from_durable_results_and_retires(project):
    run, selected, state = make_run(project)
    folder = scratch.path(project, run)
    assert folder == Path(selected['path'])
    assert not folder.is_relative_to(project.path)
    assert (project.path / RESULTS / run / scratch.RECORD).is_file()
    assert scratch.record(project, run)['root'] == folder.parent.parent.name
    (folder / 'working-file').write_text('synthetic working files')
    parent = scratch.parent(project, run)
    try:
        outcome = clean.retire_run(project, parent, run, state)
    finally:
        os.close(parent)
    assert outcome['outcome'] == 'complete'
    assert not folder.exists()
    assert clean.discard_records(project, run, state, outcome)['outcome'] == 'complete'
    assert scratch.pending(project) == []
    assert (project.path / RESULTS / run).is_dir()


def test_legacy_disk_scratch_still_retires(project):
    run = new_run_id()
    fd = project.child(RUNS, run)
    info = os.fstat(fd)
    state = {'scratch': [info.st_dev, info.st_ino, info.st_uid], 'owner': owned.mark(fd)}
    os.close(fd)
    parent = project.child(RUNS)
    try:
        assert clean.retire_run(project, parent, run, state)['outcome'] == 'complete'
    finally:
        os.close(parent)
    assert not (project.path / RUNS / run).exists()


def test_measurement_counts_hard_linked_contents_once(tmp_path):
    first = tmp_path / 'first'
    first.write_bytes(b'small synthetic contents')
    os.link(first, tmp_path / 'second')
    measured = scratch.usage(tmp_path)
    assert measured['files'] == 1
    assert measured['bytes'] >= first.stat().st_size


def test_monitor_records_peak_and_refuses_after_small_synthetic_bound(tmp_path):
    monitor = scratch.Monitor(tmp_path, bound=1)
    monitor.check(force=True)
    (tmp_path / 'working-file').write_bytes(b'example')
    with pytest.raises(Refused, match='bound'):
        monitor.check(force=True)
    assert monitor.exceeded
    assert monitor.peak['files'] == 1


def test_plain_temp_is_project_owned_and_reused_by_direct_workers(project, monkeypatch):
    folder = scratch.plain(project.project)
    assert folder.name == 't'
    assert folder.parent.name == scratch.project_name(project.project)
    monkeypatch.setenv('TMPDIR', str(folder))
    assert scratch.plain(project.project) == folder


def test_explicit_basetemp_outside_automatic_tmpdir_is_included(tmp_path):
    automatic, explicit = tmp_path / 'automatic', tmp_path / 'explicit'
    automatic.mkdir()
    explicit.mkdir()
    (explicit / 'working-file').write_text('synthetic contents')
    monitor = scratch.Monitor(automatic, extra=[explicit])
    monitor.check(force=True)
    assert monitor.peak['files'] == 1
    assert monitor.peak['bytes'] > 0


def test_plain_observer_finishes_without_leaving_a_process(tmp_path):
    observer = scratch.PlainMonitor(tmp_path)
    report = observer.finish()
    assert report['refusal'] is None
    assert observer.process.poll() == 0


def test_recovery_retires_records_after_reboot_removes_memory_scratch(project, monkeypatch):
    monkeypatch.setattr(clean.procs, 'boot_id', lambda: 'synthetic-before-reboot')
    run, selected, state = make_run(project)
    parent = scratch.parent(project, run)
    try:
        assert owned.retire(parent, run, state['scratch'], Path(selected['path']), state['owner'])['outcome'] == 'complete'
    finally:
        os.close(parent)
    Path(selected['path']).parent.rmdir()
    Path(selected['path']).parent.parent.rmdir()
    monkeypatch.setattr(clean.procs, 'boot_id', lambda: 'synthetic-after-reboot')
    lines = clean.recover(project, 'synthetic-change')
    assert any('absent memory scratch retired' in line for line in lines)
    assert scratch.pending(project) == []
    assert (project.path / RESULTS / run / 'result.json').is_file()


PLAIN_EXAMPLE = """import os
from pathlib import Path

from tests.conftest import socket_project
from tests.testrunner import layout, scratch


def test_ordinary_temp(tmp_path):
    temporary = Path(os.environ['TMPDIR'])
    assert temporary == tmp_path
    assert temporary.is_dir()
    assert any(parent.name == layout.PLAIN_TEMP for parent in tmp_path.parents)
    if os.environ['EXPECT_STORAGE'] == 'memory':
        assert scratch.in_memory(tmp_path)
    else:
        assert tmp_path.is_relative_to(socket_project() / layout.ROOT_NAME / layout.PLAIN_TEMP)
"""

FORCE_DISK = """import tests.testrunner.scratch as scratch

scratch.choose = lambda *args, **kwargs: None
"""


def plain_pytest(tmp_path, expect, plugins=()):
    """Direct pytest of one example under the project's conftest, as a contributor runs it. The inner run
    keeps the socket anchor this session validated; the separate project temp root is
    independent of that fixed-length socket anchor."""
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[2]
    (tmp_path / 'test_direct.py').write_text(PLAIN_EXAMPLE)
    (tmp_path / 'force_disk.py').write_text(FORCE_DISK)
    env = {key: value for key, value in os.environ.items()
           if key not in ('TESTRUNS_RUN', 'TESTRUN_EVIDENCE', 'TESTRUN_SESSION_RECORDS',
                          'TMUX', 'TMUX_PANE', 'QD_TMUX_SOCKET')}
    path = [str(root), str(tmp_path), *filter(None, os.environ.get('PYTHONPATH', '').split(os.pathsep))]
    env.update(PYTHONPATH=os.pathsep.join(path), PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',
               PYTEST_ADDOPTS='', TMPDIR=str(tmp_path), OPENBLAS_NUM_THREADS='1', EXPECT_STORAGE=expect,
               TMUX_TMPDIR=str(Path(os.environ['TMUX_TMPDIR']).parent))  # the session's container's anchor
    options = [item for plugin in plugins for item in ('-p', plugin)]
    done = subprocess.run([sys.executable, '-m', 'pytest', '-q', *options, '-p', 'tests.conftest',
                           '-c', str(root / 'pyproject.toml'), str(tmp_path / 'test_direct.py')],
                          env=env, cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout


def test_plain_pytest_default_tmpdir_and_basetemp_use_owned_memory(tmp_path):
    if scratch.choose() is None:
        pytest.skip('no writable executable tmpfs/ramfs has 4 GiB free: the fallback test covers this machine')
    assert 'working files on disk' not in plain_pytest(tmp_path, 'memory')


def test_plain_pytest_falls_back_to_the_project_disk_without_memory(tmp_path):
    output = plain_pytest(tmp_path, 'disk', plugins=('force_disk',))
    assert 'pytest: working files on disk: no writable executable tmpfs/ramfs has 4 GiB free' in output


def test_an_outstanding_memory_run_leaves_the_next_its_own_reservation_and_each_retires_only_its_own(project, capsys):
    run, selected, state = make_run(project)
    later, later_selected, later_state = make_run(project)
    assert (selected['kind'], later_selected['kind']) == ('tmpfs', 'tmpfs')
    assert scratch.path(project, run) != scratch.path(project, later)
    assert scratch.pending(project) == sorted([run, later])
    assert 'working files on disk' not in capsys.readouterr().out
    (scratch.path(project, later) / 'still-working').write_text('kept')
    parent = scratch.parent(project, run)
    try:
        outcome = clean.retire_run(project, parent, run, state)
    finally:
        os.close(parent)
    assert clean.discard_records(project, run, state, outcome)['outcome'] == 'complete'
    assert not scratch.path(project, run).exists() and scratch.pending(project) == [later]
    assert (scratch.path(project, later) / 'still-working').read_text() == 'kept', 'the peer is untouched'
    parent = scratch.parent(project, later)
    try:
        outcome = clean.retire_run(project, parent, later, later_state)
    finally:
        os.close(parent)
    assert clean.discard_records(project, later, later_state, outcome)['outcome'] == 'complete'


def test_the_ci_layout_skip_fires_exactly_when_the_refusal_would(project, monkeypatch, tmp_path):
    """The nested CI gates skip by name iff a fresh project would be refused, on the same measurement."""
    from tests.integration import test_testrun_ci_layout_corrections as corrections
    from tests.integration import test_testrun_ci_layout_gates as gates
    from tests.integration import test_testrun_tests_origin as origin
    from tests.integration.testrun_synthetic import skip_without_ci_memory

    named = (
        gates.test_a_ci_layout_run_tests_the_target_in_an_independent_repository_and_touches_no_project_git,
        gates.test_counts_are_withheld_when_the_measured_session_layout_is_not_cis,
        gates.test_an_abandoned_ci_layout_run_is_held_on_an_untrustworthy_state_and_recovered_on_its_own,
        corrections.test_a_package_that_resolves_from_the_ci_venv_inside_the_checkout_is_refused,
        origin.test_tests_from_outside_the_checkouts_tests_folder_refuses_before_pytest,
    )
    assert [fn.__ci_memory_skip__ for fn in named] == [skip_without_ci_memory] * len(named)
    marks = [mark for mark in named[-1].pytestmark if mark.name == 'parametrize']
    assert marks[0].kwargs['ids'] == ['regular-elsewhere', 'namespace-portion-elsewhere']

    def no_room(bound=scratch.BOUND, physical=False):
        assert physical is True and bound == scratch.BOUND + scratch.MARGIN
        return iter(())

    monkeypatch.setattr(scratch, 'choices', no_room)
    assert scratch.eligible_memory(None, physical=True) is None
    with pytest.raises(Refused) as refused:
        scratch.prepare(project, new_run_id(), memory_required=True)
    assert str(refused.value) == scratch.NO_CI_MEMORY
    with pytest.raises(pytest.skip.Exception) as skipped:
        skip_without_ci_memory()
    assert skipped.value.msg == scratch.NO_CI_MEMORY

    base = tmp_path / 'ci-base'
    base.mkdir()

    def room(bound=scratch.BOUND, physical=False):
        assert physical is True and bound == scratch.BOUND + scratch.MARGIN
        return iter([(base, 'tmpfs')])

    monkeypatch.setattr(scratch, 'choices', room)
    monkeypatch.setattr(scratch, 'physical_filesystem', lambda fd: 'tmpfs')
    assert scratch.eligible_memory(project, physical=True) == (base, 'tmpfs')
    skip_without_ci_memory()
    selected = scratch.prepare(project, new_run_id(), memory_required=True)
    assert selected['kind'] == 'tmpfs'


def test_empty_files_and_directories_are_charged_for_metadata(tmp_path):
    (tmp_path / 'empty-file').touch()
    (tmp_path / 'empty-directory').mkdir()
    measured = scratch.usage(tmp_path)
    assert measured['metadata_allowance_bytes'] == 2 * scratch.METADATA_ALLOWANCE
    assert measured['bytes'] >= 2 * scratch.METADATA_ALLOWANCE
