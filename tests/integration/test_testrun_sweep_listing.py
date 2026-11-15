"""A sweep's ordinary listing error must not interrupt the run's final evidence."""
from pathlib import Path
import subprocess
import sys

import pytest

from tests.integration.testrun_synthetic import TOOL, make, result, results, scratch

DRIVER = r'''
import errno, pathlib, sys
from unittest.mock import patch
sys.dont_write_bytecode = True
tool = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(tool.parent))
from testrunner import cli, socket_roots
original = socket_roots.sweep
def sweep(*args, **kwargs):
    # Fail the actual outer listing, rather than replacing the sweep with a stub.
    with patch.object(socket_roots.os, 'scandir', side_effect=OSError(errno.EIO, 'synthetic /tmp listing failure')):
        return original(*args, **kwargs)
socket_roots.sweep = sweep
files = [tool, *sorted((tool.parent / 'testrunner').glob('*.py'))]
sys.exit(cli.main(sys.argv[2:], files))
'''


@pytest.mark.parametrize('fails', [False, True], ids=['passed-tests', 'failed-tests'])
def test_listing_failure_preserves_the_outcome_and_drive_write_evidence(tmp_path, fails):
    project, target = make(tmp_path, {'test_example.py': f'def test_example():\n    assert {not fails!r}\n'})
    done = subprocess.run([sys.executable, '-I', '-c', DRIVER, str(TOOL),
                           'run', '--target', str(target), '-n', '0'],
                          cwd=project, capture_output=True, text=True, timeout=120)
    assert done.returncode == (1 if fails else 6), done.stdout + done.stderr
    found = result(project)
    assert found['outcome']['status'] == ('failed' if fails else 'passed')
    assert found['outcome']['pytest_exit'] == int(fails)
    assert found['outcome']['wrapper_exit'] == done.returncode
    assert found['evidence']['complete']
    assert found['evidence']['counts']['passed'] == (1 if fails else 2)
    assert found['evidence']['counts'].get('failed', 0) == int(fails)
    assert found['cleanup']['outcome'] == 'held'
    sweep = found['cleanup']['orphan_socket_roots']
    assert sweep['outcome'] == 'held'
    assert 'OSError' in sweep['reason'] and 'synthetic /tmp listing failure' in sweep['reason']
    assert 'process_io_before' in found['drive_writes']
    assert 'process_io_after' in found['drive_writes']
    assert 'device_after' in found['drive_writes']
    assert isinstance(found['drive_writes']['process_write_bytes'], int)
    report = (results(project)[-1] / 'report.md').read_text()
    assert 'synthetic /tmp listing failure' in report and 'process_write_bytes' in report
    assert scratch(project) == []
