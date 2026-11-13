"""Ordinary argument contract for the test tool's pytest invocation."""
from pathlib import Path

import pytest


@pytest.mark.parametrize('workers', [0, 2, 8])
def test_pytest_arguments_preserve_workers_and_selection(monkeypatch, tmp_path, workers):
    with monkeypatch.context() as context:
        context.syspath_prepend(str(Path(__file__).resolve().parents[1]))
        from testrunner.run import pytest_argv

    python = tmp_path / 'venv' / 'bin' / 'python'
    scratch = tmp_path / 'scratch'
    targets = ['tests/unit', 'tests/integration/test_example.py::test_selected']
    original_targets = targets.copy()

    arguments = pytest_argv(python, scratch, workers, targets)

    expected = [python, '-m', 'pytest', '-q', '-rs', '-p', 'qd_testrun_evidence',
                '-p', 'no:cacheprovider', f'--basetemp={scratch / "tmp" / "b"}']
    if workers:
        expected += ['-n', str(workers), '--dist', 'worksteal']
    assert arguments == expected + original_targets
    assert targets == original_targets
