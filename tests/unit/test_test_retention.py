"""Pytest run directly, under the project's retention settings: ordinary session history is bounded to the
last three sessions, each keeping its trees, its result, report and failing output. tests/testrun.py removes
all of its scratch at a run's end instead, keeping the complete failure output in its results."""

import os
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
TEST = '''def test_passed(tmp_path):
    (tmp_path / "passed-evidence").write_text("kept with its session")

def test_other(tmp_path):
    (tmp_path / "other-evidence").write_text("synthetic evidence")
'''


def session(folder, history, *, fail=False):
    """One ordinary pytest session under the project's settings, its temporary root isolated in ``history``."""
    folder.mkdir()
    test = folder / "test_synthetic.py"
    test.write_text(TEST + ('    assert False, "synthetic failure"\n' if fail else '    assert True\n'))
    report = folder / "report.xml"
    env = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": "",
           "PYTEST_DEBUG_TEMPROOT": str(history)}
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                             "-c", str(ROOT / "pyproject.toml"), "--rootdir", str(folder), str(test),
                             "--junitxml=" + str(report)],
                            cwd=folder, env=env, capture_output=True, text=True, timeout=60)
    (folder / "output.log").write_text(result.stdout + result.stderr)
    (folder / "result.txt").write_text(str(result.returncode))
    return result


def sessions(history):
    owner, = history.iterdir()  # pytest-of-<user>
    return sorted((path for path in owner.iterdir() if re.fullmatch(r"pytest-\d+", path.name)),
                  key=lambda path: int(path.name.split("-")[1]))


def test_ordinary_session_history_is_bounded_to_the_last_three(tmp_path):
    history = tmp_path / "history"
    history.mkdir()
    for number in range(5):
        result = session(tmp_path / str(number), history, fail=number == 3)
        assert result.returncode == (1 if number == 3 else 0), result.stdout + result.stderr
        assert (tmp_path / str(number) / "report.xml").is_file()
    kept = sessions(history)
    assert [path.name for path in kept] == ["pytest-2", "pytest-3", "pytest-4"]
    for path in kept:  # each kept session keeps its trees whole: nothing is removed during a test's teardown
        assert len(list(path.rglob("passed-evidence"))) == 1 and len(list(path.rglob("other-evidence"))) == 1


def test_a_failed_session_keeps_its_output_report_and_tree(tmp_path):
    history = tmp_path / "history"
    history.mkdir()
    result = session(tmp_path / "failed", history, fail=True)
    assert result.returncode == 1, result.stdout + result.stderr
    evidence, = sessions(history)[0].rglob("other-evidence")
    assert evidence.read_text() == "synthetic evidence"
    assert (tmp_path / "failed" / "result.txt").read_text() == "1"
    assert 'failures="1"' in (tmp_path / "failed" / "report.xml").read_text()
    assert "synthetic failure" in (tmp_path / "failed" / "output.log").read_text()
