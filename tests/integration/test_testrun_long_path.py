"""A long declared socket suite is refused before even collection starts."""
from tests.integration.testrun_synthetic import commit, make, tool
from tests.testrunner import socket_paths


def test_long_checkout_is_refused_without_starting_tests_or_creating_a_run(tmp_path):
    marker = tmp_path / 'collected'
    project, target = make(tmp_path, {'test_not_started.py': f'''
        from pathlib import Path
        Path({str(marker)!r}).write_text('collected')
        def test_not_started():
            raise AssertionError('should never start')
    '''})
    helper = target / 'tests/testrunner/socket_paths.py'
    helper.parent.mkdir(parents=True)
    helper.write_text('raise AssertionError("target helper must not be imported")\n')
    commit(target, 'Declare the synthetic socket suite')
    done = tool('run', '--target', str(target), '--fast', '-n', '0', cwd=project)
    assert done.returncode == 2, done.stdout + done.stderr
    assert 'native Unix socket path limit is 107 bytes' in done.stderr
    assert f'longest checkout/project path is {socket_paths.project_limit()} bytes' in done.stderr
    assert str(project / '.testruns/r') in done.stderr
    assert not marker.exists()
    assert not (project / '.testruns').exists()
