"""The fixed layout's authority, its atomic control state, the one slot, and owned removal: anchored, never
following a link, refusing what is not the recorded tree."""
import os
from pathlib import Path
import subprocess

import pytest

from tests.testrunner import layout, owned


def project(tmp_path) -> Path:
    folder = tmp_path / 'synthetic-project'
    folder.mkdir()
    subprocess.run(['git', 'init', '-q', str(folder)], check=True)
    return folder


def test_the_root_ignores_itself_and_is_private(tmp_path):
    root = layout.Root(project(tmp_path))
    try:
        info = os.lstat(root.path)
        assert oct(info.st_mode & 0o777) == '0o700' and (root.path / '.gitignore').read_text() == '*\n'
        assert subprocess.run(['git', '-C', str(root.project), 'status', '--porcelain'], capture_output=True,
                              text=True).stdout == ''
    finally:
        root.close()


@pytest.mark.parametrize('plant', ['link', 'file', 'shared'])
def test_a_root_that_is_not_a_private_directory_is_refused(tmp_path, plant):
    folder = project(tmp_path)
    decoy = tmp_path / 'decoy'
    decoy.mkdir()
    (decoy / 'canary').write_text('outside')
    if plant == 'link':
        (folder / '.testruns').symlink_to(decoy, target_is_directory=True)
    elif plant == 'file':
        (folder / '.testruns').write_text('not a folder')
    else:
        (folder / '.testruns').mkdir(mode=0o777)
        os.chmod(folder / '.testruns', 0o777)
    with pytest.raises(layout.Refused):
        layout.Root(folder)
    assert (decoy / 'canary').read_text() == 'outside' and sorted(os.listdir(decoy)) == ['canary']


def test_a_nested_link_in_the_layout_is_refused(tmp_path):
    root = layout.Root(project(tmp_path))
    try:
        decoy = tmp_path / 'decoy'
        decoy.mkdir()
        (root.path / 'runs').symlink_to(decoy, target_is_directory=True)
        with pytest.raises(layout.Refused, match='link'):
            root.child('runs')
    finally:
        root.close()


def test_control_state_of_an_unknown_format_or_malformed_is_refused(tmp_path):
    root = layout.Root(project(tmp_path))
    try:
        fd = root.child('changes')
        layout.write_json(fd, 'ok.json', {'change': 'x'})
        assert layout.read_json(fd, 'ok.json', 'ok')['change'] == 'x'
        (root.path / 'changes' / 'future.json').write_text('{"format": 2}')
        (root.path / 'changes' / 'torn.json').write_text('{"format": 1, "cha')
        for name in ('future.json', 'torn.json'):
            with pytest.raises(layout.Refused):
                layout.read_json(fd, name, name)
        assert sorted(os.listdir(fd)) == ['future.json', 'ok.json', 'torn.json']  # no temporary left
        os.close(fd)
    finally:
        root.close()


def test_the_slot_refuses_at_once_and_names_its_holder(tmp_path):
    root = layout.Root(project(tmp_path))
    first, second = layout.Slot(root), layout.Slot(root)
    try:
        first.acquire({'verb': 'run', 'run': '20261111-120000-abcd', 'change': 'feature', 'seat': 'synthetic'})
        with pytest.raises(layout.Refused, match='busy: verb run, run 20261111-120000-abcd, change feature'):
            second.acquire({'verb': 'clean'})
        with pytest.raises(layout.Refused, match='waited 1 s'):
            second.acquire({'verb': 'clean'}, wait=1)
        first.release()
        second.acquire({'verb': 'clean'})
    finally:
        second.release()
        root.close()


def test_names_are_plain(tmp_path):
    assert layout.change_file('feature/x-1') == 'feature+x-1'
    for bad in ('../x', '-x', 'a b', 'x/', ''):
        with pytest.raises(layout.Refused):
            layout.change_file(bad)
    with pytest.raises(layout.Refused):
        layout.run_name('../20261111-120000-abcd')


def make_tree(base: Path):
    (base / 'a' / 'b').mkdir(parents=True)
    (base / 'a' / 'b' / 'f').write_text('x' * 10)
    os.chmod(base / 'a' / 'b', 0o555)  # like the run's read-only package folders


def test_owned_removal_counts_and_removes_read_only_trees_without_following_links(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'canary').write_text('keep me')
    tree = tmp_path / 'tree'
    tree.mkdir()
    make_tree(tree)
    (tree / 'link').symlink_to(outside, target_is_directory=True)
    parent = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        expected = layout.identity(os.lstat(tree))
        tally = owned.remove(parent, 'tree', expected, tree)
    finally:
        os.close(parent)
    assert tally.as_dict() == {'files': 1, 'dirs': 3, 'links': 1, 'bytes': 10}
    assert not tree.exists() and (outside / 'canary').read_text() == 'keep me'


def test_owned_removal_refuses_a_replaced_tree_and_removes_nothing(tmp_path):
    tree = tmp_path / 'tree'
    tree.mkdir()
    expected = layout.identity(os.lstat(tree))
    tree.rmdir()
    tree.mkdir()
    (tree / 'unrelated').write_text('not the tool\'s')
    parent = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(layout.Refused, match='replaced'):
            owned.remove(parent, 'tree', expected, tree)
    finally:
        os.close(parent)
    assert (tree / 'unrelated').exists()


def test_owned_removal_refuses_a_link_in_place_of_the_tree(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'canary').write_text('keep me')
    (tmp_path / 'tree').symlink_to(outside, target_is_directory=True)
    parent = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(layout.Refused):
            owned.remove(parent, 'tree', None, tmp_path / 'tree')
    finally:
        os.close(parent)
    assert (outside / 'canary').exists()
