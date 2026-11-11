"""tests/testrun.py run, on synthetic projects: every ending leaves only the result, the report and complete
failure output; counts are proved or unknown; the runner and the target are told apart by Git."""
import os
from pathlib import Path
import signal
import time

from tests.conftest import ROOT, git
from tests.integration.testrun_synthetic import (TOOL, WAIT_TEST, admin_entries, make, result, results, scratch,
                                                 start, started_marker, tool, wait_for)


def leaves_only_the_fixed_ending(project, failures=0):
    run, = results(project)
    expected = ['report.md', 'result.json'] + (['failures'] if failures else [])
    assert sorted(os.listdir(run)) == sorted(expected)
    if failures:
        assert len(os.listdir(run / 'failures')) == failures
    assert scratch(project) == []
    assert admin_entries(project) == ['feature']  # the run checkout's Git admin entry is gone too
    return run


def test_a_passing_run_proves_what_ran_and_leaves_only_its_result_and_report(tmp_path):
    project, worktree = make(tmp_path, {'test_ok.py': '''
        def test_writes_scratch(tmp_path):
            (tmp_path / "scratch").write_text("deleted at the end")
        def test_imports():
            import synth
            assert synth.VALUE == 1
        '''})
    env = dict(os.environ, PYTHONPATH='/synthetic/contamination', SYNTH_API_TOKEN='synthetic-secret-value')
    done = tool('run', '--target', str(worktree), '-n', '0', cwd=project, env=env)
    assert done.returncode == 0, done.stdout + done.stderr
    run = leaves_only_the_fixed_ending(project)
    found = result(project)
    head, tree = git(worktree, 'rev-parse', 'HEAD'), git(worktree, 'rev-parse', 'HEAD^{tree}')
    assert found['outcome']['status'] == 'passed' and found['evidence']['complete']
    assert found['evidence']['counts']['passed'] == 3 and found['evidence']['expected'] == 3
    assert found['target']['head'] == head and found['target']['tree'] == tree == found['checkout']['tree']
    assert found['target']['project'] == str(project) and found['change'] == 'feature'
    assert str(TOOL) in found['runner']['files'] and Path(found['runner']['checkout']) == ROOT
    assert Path(found['tools']['run_venv']['imports']['synth']).is_relative_to(found['checkout']['path'])
    assert found['manifests']['target']['unchanged'] and git(worktree, 'status', '--porcelain', '--ignored') == ''
    assert found['checkout']['readonly_package_dirs'] >= 1
    assert 'PYTHONPATH' in found['environment']['removed_names']
    for name in ('result.json', 'report.md'):
        text = (run / name).read_text()
        assert 'synthetic-secret-value' not in text and '/synthetic/contamination' not in text
    registered = (project / '.testruns' / 'changes' / 'feature.json').read_text()
    assert run.name in registered and str(run / 'result.json') in done.stdout


def test_failures_keep_their_complete_output_and_the_report_summarises(tmp_path):
    project, worktree = make(tmp_path, {'test_fail.py': '''
        def test_fails():
            for n in range(5000):
                print(f"line {n}")
            assert False, "synthetic failure"
        def test_passes(tmp_path):
            (tmp_path / "x").write_text("x")
        '''})
    done = tool('run', '--target', str(worktree), '-n', '2', cwd=project)
    assert done.returncode == 1, done.stdout + done.stderr
    run = leaves_only_the_fixed_ending(project, failures=1)
    text = (run / 'failures' / '001.txt').read_text()
    assert 'line 4999' in text and 'synthetic failure' in text and 'test_fails' in text
    report = (run / 'report.md').read_text()
    assert 'failures/001.txt' in report and 'Complete console output' in report
    found = result(project)
    assert found['outcome']['status'] == 'failed'
    assert found['evidence']['counts']['failed'] == 1 and found['evidence']['counts']['passed'] == 2


def test_every_category_is_proved_under_xdist(tmp_path):
    project, worktree = make(tmp_path, {'test_kinds.py': '''
        import pytest
        @pytest.fixture
        def broken_setup():
            raise RuntimeError("setup broke")
        @pytest.fixture
        def broken_teardown():
            yield
            raise RuntimeError("teardown broke")
        def test_setup_error(broken_setup): pass
        def test_teardown_error(broken_teardown): pass
        @pytest.mark.xfail(reason="expected")
        def test_xfailed(): assert False
        @pytest.mark.xfail(reason="lenient")
        def test_xpassed(): pass
        @pytest.mark.xfail(reason="strict", strict=True)
        def test_strict_xpass(): pass
        def test_skipped(): pytest.skip("synthetic skip")
        '''})
    done = tool('run', '--target', str(worktree), '-n', '2', cwd=project)
    assert done.returncode == 1, done.stdout + done.stderr
    found = result(project)
    assert found['evidence']['complete'], found['evidence']['causes']
    assert found['evidence']['counts'] == {'passed': 2, 'failed': 1, 'error': 2, 'skipped': 1, 'xfailed': 1,
                                           'xpassed': 1}
    tools = found['tools']
    assert tools['run_venv']['xdist']['origin'] in ('user site', 'system') and tools['run_venv']['xdist']['version']
    assert tools['run_venv']['pytest']['origin'] in ('user site', 'system')
    if tools['user_site']['exists']:
        assert tools['user_site']['path_line'] and tools['user_site']['bytecode_folders']['changed'] == []
    leaves_only_the_fixed_ending(project, failures=3)


def test_a_collection_error_is_known_and_the_counts_unknown(tmp_path):
    project, worktree = make(tmp_path, {'test_broken.py': 'def test_(:\n'})
    done = tool('run', '--target', str(worktree), '-n', '0', cwd=project)
    assert done.returncode == 5, done.stdout + done.stderr
    found = result(project)
    assert found['outcome']['status'] == 'incomplete' and found['evidence']['collection_errors'] == 1
    assert set(found['evidence']['counts'].values()) == {None}
    run = leaves_only_the_fixed_ending(project, failures=1)
    assert 'SyntaxError' in (run / 'failures' / '001.txt').read_text()


def test_no_tests_is_its_own_ending(tmp_path):
    project, worktree = make(tmp_path, {'test_empty.py': '"""Nothing here."""\n'})
    done = tool('run', 'tests/test_empty.py', '--target', str(worktree), '-n', '0', cwd=project)
    assert done.returncode == 5, done.stdout + done.stderr
    assert result(project)['outcome']['status'] == 'no-tests'
    leaves_only_the_fixed_ending(project)


def test_the_time_limit_stops_the_run_and_still_leaves_the_fixed_ending(tmp_path):
    project, worktree = make(tmp_path, {'test_wait.py': WAIT_TEST})
    began = time.monotonic()
    done = tool('run', '--target', str(worktree), '-n', '0', '--timeout', '8', cwd=project)
    assert done.returncode == 3, done.stdout + done.stderr
    assert time.monotonic() - began < 90
    found = result(project)
    assert found['outcome']['status'] == 'timeout' and set(found['evidence']['counts'].values()) == {None}
    assert 'tests/test_wait.py::test_waits' in found['evidence']['missing']
    leaves_only_the_fixed_ending(project)


def test_an_interruption_stops_the_runs_own_group_and_leaves_the_fixed_ending(tmp_path):
    project, worktree = make(tmp_path, {'test_wait.py': WAIT_TEST})
    process = start('run', '--target', str(worktree), '-n', '0', cwd=project)
    marker = wait_for(lambda: started_marker(project))
    test_pid = int(wait_for(lambda: marker.read_text()))
    process.send_signal(signal.SIGINT)
    out, err = process.communicate(timeout=120)
    assert process.returncode == 4, out + err
    assert result(project)['outcome']['status'] == 'interrupted'
    wait_for(lambda: not Path(f'/proc/{test_pid}').exists(), 30)
    leaves_only_the_fixed_ending(project)


def test_a_lost_worker_is_incomplete_and_its_crash_is_kept(tmp_path):
    project, worktree = make(tmp_path, {'test_crash.py': '''
        import os
        def test_crashes():
            os._exit(3)
        def test_fine():
            pass
        '''})
    done = tool('run', '--target', str(worktree), '-n', '2', cwd=project)
    assert done.returncode == 5, done.stdout + done.stderr
    found = result(project)
    assert not found['evidence']['complete'] and found['outcome']['status'] == 'incomplete'
    assert 'tests/test_crash.py::test_crashes' in found['evidence']['crashed_items'] or found['evidence']['workers_lost']
    assert found['failures'] and 'test_crashes' in found['failures'][0]['nodeid']


def test_a_dirty_target_is_refused_before_anything_is_made(tmp_path):
    project, worktree = make(tmp_path, {})
    (worktree / 'tests' / 'test_new.py').write_text('def test_new(): pass\n')
    done = tool('run', '--target', str(worktree), cwd=project)
    assert done.returncode == 2 and 'uncommitted changes' in done.stderr and 'tests/test_new.py' in done.stderr
    assert not (project / '.testruns').exists()


def test_a_busy_slot_refuses_and_names_its_holder(tmp_path):
    from tests.testrunner import layout
    project, worktree = make(tmp_path, {})
    root = layout.Root(project)
    slot = layout.Slot(root)
    slot.acquire({'verb': 'synthetic-holder', 'seat': 'synthetic'})
    try:
        done = tool('run', '--target', str(worktree), cwd=project)
        assert done.returncode == 2 and 'busy: verb synthetic-holder' in done.stderr
        done = tool('run', '--target', str(worktree), '--wait', '1', cwd=project)
        assert done.returncode == 2 and 'waited 1 s' in done.stderr
    finally:
        slot.release()
        root.close()
    assert results(project) == []


def test_the_run_owns_its_startup_bytecode_for_isolated_children(tmp_path):
    """Cold: a fresh checkout with no prior bytecode. Isolated children importing the package concurrently,
    across workers, change nothing in the package's bytecode folder, which is complete and read-only first."""
    project, worktree = make(tmp_path, {'test_cold.py': '''
        import os, pathlib, subprocess, sys
        import pytest
        def stamp(folder):
            entries = sorted(folder.iterdir())
            info = folder.lstat()
            return [(info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)] + [
                (p.name, p.lstat().st_ino, p.lstat().st_mtime_ns, p.lstat().st_size) for p in entries]
        @pytest.mark.parametrize("round", range(4))
        def test_children(round):
            import synth
            cache = pathlib.Path(synth.__file__).parent / "__pycache__"
            before = stamp(cache)
            assert not os.access(cache.parent, os.W_OK)
            code = "import synth, synth.a, synth.b, synth.c, synth.d, synth.e, synth.f, synth.g, synth.h"
            children = [subprocess.Popen([sys.executable, "-I", "-c", code]) for _ in range(4)]
            assert [child.wait(60) for child in children] == [0, 0, 0, 0]
            assert stamp(cache) == before
        '''})
    done = tool('run', '--target', str(worktree), '-n', '4', cwd=project)
    assert done.returncode == 0, done.stdout + done.stderr
    found = result(project)
    mutations = found['manifests']['checkout']['mutations']
    assert mutations['tracked'] == {'added': [], 'removed': [], 'changed': []}
    # pytest writes its rewritten test modules under tests/; the package's own bytecode never changes
    assert [name for name in mutations['extra']['added'] if name.startswith('src/')] == []
    assert 'PYTHONDONTWRITEBYTECODE' not in found['environment']['fixed']
