"""Pytest run directly, under the project's retention settings: completed runs discard successful scratch
trees and keep only failed-test evidence. tests/testrun.py removes even those at a run's end, keeping the
complete failure output in its results instead."""

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def run(folder, *, fail=False):
    folder.mkdir()
    test = folder / "test_synthetic.py"
    test.write_text('''def test_passed(tmp_path):
    (tmp_path / "passed-evidence").write_text("unneeded")

def test_other(tmp_path):
    (tmp_path / "other-evidence").write_text("synthetic evidence")
''' + ('    assert False, "synthetic failure"\n' if fail else '    assert True\n'))
    report = folder / "report.xml"
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                             "-c", str(ROOT / "pyproject.toml"), str(test),
                             "--basetemp=" + str(folder / "scratch"), "--junitxml=" + str(report)],
                            cwd=folder, env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": ""},
                            capture_output=True, text=True, timeout=30)
    (folder / "output.log").write_text(result.stdout + result.stderr)
    (folder / "result.txt").write_text(str(result.returncode))
    return result


def test_completed_successful_runs_leave_no_test_trees_and_keep_their_results(tmp_path):
    for number in range(3):
        folder = tmp_path / str(number)
        result = run(folder)
        assert result.returncode == 0, result.stdout + result.stderr
        assert not list(folder.rglob("passed-evidence")) and not list(folder.rglob("other-evidence"))
        assert (folder / "report.xml").is_file() and (folder / "result.txt").read_text() == "0"
        assert "2 passed" in (folder / "output.log").read_text()


def test_a_failure_keeps_its_evidence_output_and_report_but_discards_passed_trees(tmp_path):
    folder = tmp_path / "failed"
    result = run(folder, fail=True)
    assert result.returncode == 1, result.stdout + result.stderr
    assert not list(folder.rglob("passed-evidence"))
    evidence, = folder.rglob("other-evidence")
    assert evidence.read_text() == "synthetic evidence"
    assert (folder / "result.txt").read_text() == "1"
    assert 'failures="1"' in (folder / "report.xml").read_text()
    assert "synthetic failure" in (folder / "output.log").read_text()
