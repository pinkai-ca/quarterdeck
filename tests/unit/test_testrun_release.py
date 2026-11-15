"""Ordinary lifecycle coverage for releasing a kept test run."""
from __future__ import annotations

import os

import pytest

from tests.integration import testrun_synthetic as synth
from tests.testrunner import clean, owned, procs
from tests.testrunner.layout import KEEPS, RESULTS, RUNS, Root, RunGuard, identity, write_json


@pytest.fixture
def project(tmp_path):
    project, _ = synth.make(tmp_path, {})
    return project


def recorded_run(root, run):
    scratch = root.path / RUNS / run
    fd = root.child(RUNS, run)
    try:
        state = {'run': run, 'change': 'feature', 'scratch': identity(os.fstat(fd)),
                 'owner': owned.mark(fd), 'boot': procs.boot_id(), 'groups': []}
        write_json(fd, 'state.json', state)
    finally:
        os.close(fd)
    results = root.child(RESULTS, run)
    try:
        write_json(results, 'result.json', {'run': run, 'outcome': {'status': 'passed'}})
    finally:
        os.close(results)
    (root.path / RESULTS / run / 'report.md').write_text('Original report.\n')
    return scratch


def test_release_removes_only_kept_scratch_and_note_preserves_results(project):
    root = Root(project)
    try:
        run, other = '20000101-000000-aaaa', '20000101-000000-bbbb'
        scratch = recorded_run(root, run)
        other_scratch = recorded_run(root, other)
        package = scratch / 'checkout' / 'src' / 'example'
        package.mkdir(parents=True)
        (package / 'module.py').write_text('VALUE = 1\n')
        package.chmod(0o555)
        for item in (run, other):
            assert synth.tool('keep', item, '--reason', 'ordinary lifecycle evidence', cwd=project).returncode == 0
        results = root.path / RESULTS / run
        before = {name: (results / name).read_bytes() for name in ('result.json', 'report.md')}
        done = synth.tool('release', run, cwd=project)
        assert done.returncode == 0, done.stdout + done.stderr
        assert 'release complete' in done.stdout
        assert not scratch.exists()
        assert not (root.path / KEEPS / (run + '.json')).exists()
        assert other_scratch.is_dir()
        assert clean.kept(root, other) is not None
        assert {name: (results / name).read_bytes() for name in before} == before
        again = synth.tool('release', run, cwd=project)
        assert again.returncode == 2 and 'not kept' in again.stderr
        assert synth.tool('release', other, cwd=project).returncode == 0
    finally:
        root.close()


def test_release_refuses_run_without_keep(project):
    root = Root(project)
    try:
        run = '20000101-000000-cccc'
        scratch = recorded_run(root, run)
        done = synth.tool('release', run, cwd=project)
        assert done.returncode == 2 and 'not kept' in done.stderr
        assert scratch.is_dir()
        assert synth.tool('retire', run, cwd=project).returncode == 0
        unknown = synth.tool('release', '20000101-000000-dddd', cwd=project)
        assert unknown.returncode == 2 and 'not kept' in unknown.stderr
    finally:
        root.close()


def test_release_refuses_active_kept_run(project):
    root = Root(project)
    try:
        run = '20000101-000000-eeee'
        scratch = recorded_run(root, run)
        assert synth.tool('keep', run, '--reason', 'still working', cwd=project).returncode == 0
        guard = RunGuard(root, run)
        try:
            guard.acquire()
            done = synth.tool('release', run, cwd=project)
            assert done.returncode == 2 and 'active' in done.stderr
            assert scratch.is_dir() and clean.kept(root, run) is not None
        finally:
            guard.release()
        assert synth.tool('release', run, cwd=project).returncode == 0
    finally:
        root.close()


def test_release_partial_cleanup_retains_keep_for_retry(project, monkeypatch):
    root = Root(project)
    try:
        run = '20000101-000000-ffff'
        scratch = recorded_run(root, run)
        assert synth.tool('keep', run, '--reason', 'retry cleanup', cwd=project).returncode == 0
        original = owned.retire
        monkeypatch.setattr(owned, 'retire', lambda *a, **kw: {'outcome': 'held', 'reason': 'ordinary cleanup error'})
        assert clean.retire_within(root, run, releasing=True) == 2
        assert scratch.is_dir() and clean.kept(root, run) is not None
        monkeypatch.setattr(owned, 'retire', original)
        assert clean.retire_within(root, run, releasing=True) == 0
        assert not scratch.exists() and clean.kept(root, run) is None
    finally:
        root.close()
