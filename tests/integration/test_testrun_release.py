"""A real kept run releases its checkout, scratch and note after it ends."""
from tests.integration import testrun_synthetic as synth


def test_release_of_a_completed_kept_run(tmp_path):
    project, worktree = synth.make(tmp_path, {'test_wait.py': synth.WAIT_TEST})
    process = synth.start_tool('run', '--target', str(worktree), '-n', '0', '--timeout', '120',
                               cwd=project, env=synth.controller_environment())
    marker = None
    try:
        marker = synth.wait_for(lambda: synth.started_marker(project))
        results = synth.results(project)[-1]
        run = results.name
        kept = synth.tool('keep', run, '--reason', 'ordinary end-to-end release', cwd=project)
        assert kept.returncode == 0, kept.stdout + kept.stderr
        marker.with_name('release').touch()
        stdout, stderr = process.communicate(timeout=180)
        assert process.returncode == 0, stdout + stderr
        before = {name: (results / name).read_bytes() for name in ('result.json', 'report.md')}
        scratch = synth.working_paths(project)[0]
        assert scratch.is_dir()
        done = synth.tool('release', run, cwd=project)
        assert done.returncode == 0, done.stdout + done.stderr
        assert not scratch.exists()
        assert not (project / '.testruns' / 'keeps' / (run + '.json')).exists()
        assert synth.admin_entries(project) == ['feature']
        assert worktree.is_dir()
        assert {name: (results / name).read_bytes() for name in before} == before
    finally:
        if process.poll() is None:
            if marker is not None:
                marker.with_name('release').touch()
            process.communicate(timeout=180)
