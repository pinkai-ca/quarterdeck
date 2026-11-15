"""tests/testrun.py release refuses where its authority resolves outside the recorded run.

A kept run is made for real on a synthetic project. Then one authority path is redirected by a link: the run's
scratch itself, the folder that holds the runs' scratch, the keep note, the keeps folder, or the run's results
folder. Each is moved aside within its own folder, so the move never crosses a filesystem, and a link to it takes
its place. release exits 2 with its reason, and nothing of what the link reaches changes, byte for byte. Leaf links
inside a genuine scratch are unlinked and never followed. A protection test: Claude validation.
"""
import os
from pathlib import Path

import pytest

from tests.integration.testrun_synthetic import WAIT_TEST, make, results, start, started_marker, tool, wait_for


def kept_run(tmp_path):
    """A finished run whose scratch was kept while it ran: its folder, its note and its result."""
    project, worktree = make(tmp_path, {'test_wait.py': WAIT_TEST})
    process = start('run', '--target', str(worktree), '-n', '0', cwd=project)
    marker = wait_for(lambda: started_marker(project))
    folder = marker.parent.parent
    run = folder.name
    kept = tool('keep', run, '--reason', 'synthetic kept scratch', cwd=project)
    assert kept.returncode == 0, kept.stdout + kept.stderr
    marker.with_name('release').write_text('go')
    out, err = process.communicate(timeout=120)
    assert process.returncode == 0, out + err
    assert folder.is_dir() and not folder.is_symlink()
    (folder / 'canary').write_text('synthetic canary')
    return project, run, folder


def snapshot(top: Path) -> dict:
    """Every entry under top, by its own kind: a file's bytes, a link's target, a folder's presence."""
    found = {}
    for here, folders, files in os.walk(top, followlinks=False):
        for name in folders + files:
            path = Path(here) / name
            key = str(path.relative_to(top))
            if path.is_symlink():
                found[key] = ('link', os.readlink(path))
            elif path.is_dir():
                found[key] = ('folder',)
            else:
                found[key] = ('file', path.read_bytes())
    return found


def authority(project: Path, run: str, folder: Path, case: str) -> Path:
    keeps = project / '.testruns' / 'keeps'
    return {'scratch': folder, 'runs': folder.parent, 'note': keeps / f'{run}.json', 'keeps': keeps,
            'results': project / '.testruns' / 'results' / run}[case]


@pytest.mark.parametrize('case', ['scratch', 'runs', 'note', 'keeps', 'results'])
def test_release_refuses_an_authority_path_redirected_by_a_link_and_touches_nothing_it_reaches(tmp_path, case):
    project, run, folder = kept_run(tmp_path)
    note = project / '.testruns' / 'keeps' / f'{run}.json'
    target = authority(project, run, folder, case)
    moved = target.with_name(target.name + '.moved')
    os.rename(target, moved)
    os.symlink(moved, target)
    reached = snapshot(moved) if moved.is_dir() else {'': ('file', moved.read_bytes())}
    done = tool('release', run, cwd=project)
    assert done.returncode == 2, done.stdout + done.stderr
    assert done.stderr.startswith(f'testrun: refused: {target} is a link'), done.stderr
    assert done.stderr.rstrip().endswith('; nothing touched'), done.stderr
    assert target.is_symlink() and os.readlink(target) == str(moved), 'the link itself stays'
    assert (snapshot(moved) if moved.is_dir() else {'': ('file', moved.read_bytes())}) == reached
    if case not in ('note', 'keeps'):
        assert note.is_file() and not note.is_symlink(), 'the keep note stays for a retry'
    os.unlink(target)
    os.rename(moved, target)
    assert (folder / 'canary').read_text() == 'synthetic canary', 'the kept scratch is whole'


def test_release_unlinks_leaf_links_inside_the_scratch_and_never_follows_them(tmp_path):
    project, run, folder = kept_run(tmp_path)
    outside = tmp_path / 'outside'
    (outside / 'folder').mkdir(parents=True)
    (outside / 'file').write_text('synthetic outside file')
    (outside / 'folder' / 'inner').write_text('synthetic outside inner')
    os.symlink(outside / 'file', folder / 'leaf-file')
    os.symlink(outside / 'folder', folder / 'leaf-folder')
    os.symlink(outside / 'folder', folder / 'tmp' / 'leaf-nested')
    before = snapshot(outside)
    done = tool('release', run, cwd=project)
    assert done.returncode == 0, done.stdout + done.stderr
    assert f'run {run}: release complete' in done.stdout
    assert not folder.exists() and not folder.is_symlink()
    assert snapshot(outside) == before, 'nothing a leaf link reached was followed'
    assert not (project / '.testruns' / 'keeps' / f'{run}.json').exists()
    assert {'result.json', 'report.md'} <= set(os.listdir(results(project)[-1]))
