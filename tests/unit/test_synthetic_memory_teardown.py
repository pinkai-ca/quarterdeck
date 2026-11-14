"""Ordinary cleanup of inert project trees; no test-tool run or process is started."""

import os

from tests.conftest import SyntheticMemoryRoots
from tests.testrunner import layout, scratch


def test_all_temporary_projects_release_their_memory_roots(tmp_path, monkeypatch):
    projects = [tmp_path / 'p', tmp_path / 'second-project']
    base = tmp_path / 'memory-base'
    base.mkdir()
    roots = []
    for project in projects:
        (project / '.testruns').mkdir(parents=True)
        root = base / scratch.project_name(project)
        root.mkdir(mode=0o700)
        (root / 'runs').mkdir()
        (root / 'runs' / 'inert-kept-product.txt').write_text('synthetic product')
        roots.append(root)

    # This case exercises teardown on disk or memory alike; mount admission is not its subject.
    def open_root(project, selected_base, *, create):
        assert create is False
        path = selected_base / scratch.project_name(project)
        return path, os.open(path, layout.DIRECTORY)

    monkeypatch.setattr(scratch, 'memory_root', open_root)
    checks = set()
    cleanup = SyntheticMemoryRoots(tmp_path, checks)
    cleanup.finish()
    assert all(not root.exists() for root in roots)
    assert all(str(root) in checks for root in roots)
    assert all(project.is_dir() for project in projects)


def test_children_inherit_the_test_temporary_directory(tmp_path):
    import tempfile

    assert os.environ['TMPDIR'] == str(tmp_path)
    assert tempfile.gettempdir() == str(tmp_path)
