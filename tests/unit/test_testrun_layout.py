"""The fixed layout's authority, its atomic control state, the four run slots, and owned removal: anchored, never
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


def test_four_slots_admit_four_and_a_fifth_refuses_at_once_naming_every_holder(tmp_path):
    root = layout.Root(project(tmp_path))
    holders = [layout.Slot(root) for _ in range(layout.SLOT_COUNT)]
    fifth = layout.Slot(root)
    try:
        for index, slot in enumerate(holders):
            slot.acquire({'verb': 'run', 'run': f'20261111-12000{index}-abcd', 'change': f'feature-{index}', 'seat': 'synthetic'})
        assert sorted(slot.selected for slot in holders) == [0, 1, 2, 3], 'each holds a slot of its own'
        assert holders[0].selected == 0 and (root.path / layout.SLOT).exists(), 'slot.lock, the original name, is first'
        with pytest.raises(layout.Refused) as refused:
            fifth.acquire({'verb': 'clean'})
        assert f'all {layout.SLOT_COUNT} test slots are busy' in str(refused.value)
        for index in range(layout.SLOT_COUNT):
            assert f'run 20261111-12000{index}-abcd, change feature-{index}' in str(refused.value), 'every holder named'
        with pytest.raises(layout.Refused, match='waited 1 s'):
            fifth.acquire({'verb': 'clean'}, wait=1)
        freed = holders[2].selected
        holders[2].release()
        fifth.acquire({'verb': 'clean'})
        assert fifth.selected == freed, 'the fifth takes exactly the slot that came free'
    finally:
        for slot in (*holders, fifth):
            slot.release()
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
    tree.rename(tmp_path / 'original')  # the original still exists, so the replacement has its own inode
    tree.mkdir()
    (tree / 'unrelated').write_text('not the tool\'s')
    parent = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(layout.Refused, match='replaced'):
            owned.remove(parent, 'tree', expected, tree)
    finally:
        os.close(parent)
    assert (tree / 'unrelated').exists() and (tmp_path / 'original').exists()


def test_owned_removal_refuses_a_recreated_tree_even_when_its_inode_is_reused(tmp_path):
    tree = tmp_path / 'tree'
    tree.mkdir()
    fd = os.open(tree, os.O_RDONLY | os.O_DIRECTORY)
    token = owned.mark(fd)
    os.close(fd)
    expected = layout.identity(os.lstat(tree))
    (tree / owned.MARKER).unlink()
    tree.rmdir()
    tree.mkdir()  # the filesystem may hand the freed inode number straight back
    (tree / 'unrelated').write_text('not the tool\'s')
    parent = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(layout.Refused, match='replaced|recreated'):
            owned.remove(parent, 'tree', expected, tree, token)
    finally:
        os.close(parent)
    assert (tree / 'unrelated').exists()


def test_owned_removal_accepts_its_own_marked_tree(tmp_path):
    tree = tmp_path / 'tree'
    tree.mkdir()
    fd = os.open(tree, os.O_RDONLY | os.O_DIRECTORY)
    token = owned.mark(fd)
    os.close(fd)
    parent = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        tally = owned.remove(parent, 'tree', layout.identity(os.lstat(tree)), tree, token)
    finally:
        os.close(parent)
    assert not tree.exists() and tally.files == 1


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


# -- the memory root: refused unless a plain private folder of this user on tmpfs or ramfs ------------------------


@pytest.fixture
def memory(tmp_path):
    from tests.testrunner import scratch
    chosen = scratch.choose()
    if chosen is None:
        pytest.skip('no tmpfs or ramfs with 4 GiB free')
    base, _ = chosen
    name = f'qd-gate-{os.getpid()}-{tmp_path.name}'
    yield scratch, base, name
    leftover = base / name
    if leftover.is_dir() and not leftover.is_symlink():
        os.chmod(leftover, 0o700)
        leftover.rmdir()


def test_the_memory_root_is_made_private_under_a_plain_memory_base(memory, tmp_path):
    scratch, base, name = memory
    where, fd = scratch.memory_root(tmp_path, base, name=name)
    try:
        info = os.fstat(fd)
        assert where == base / name and info.st_uid == os.getuid() and info.st_mode & 0o777 == 0o700
    finally:
        os.close(fd)


def test_a_symlinked_memory_base_is_refused(memory, tmp_path):
    scratch, base, name = memory
    link = tmp_path / 'linked-base'
    link.symlink_to(base, target_is_directory=True)
    with pytest.raises(layout.Refused, match='not an absolute plain path'):
        scratch.memory_root(tmp_path, link, name=name)
    with pytest.raises(layout.Refused, match='not an absolute plain path'):
        scratch.memory_root(tmp_path, Path('relative-base'), name=name)
    assert not (base / name).exists()


def test_a_base_that_is_not_tmpfs_or_ramfs_is_refused(memory, tmp_path):
    scratch, _, name = memory
    disk = Path(__file__).resolve().parent  # refused before anything is made, so the repository itself serves
    fd = os.open(disk, os.O_RDONLY | os.O_DIRECTORY)
    try:
        in_memory = scratch.filesystem(fd) is not None
    finally:
        os.close(fd)
    if in_memory:
        pytest.skip('the repository itself is in memory')
    with pytest.raises(layout.Refused, match='no longer tmpfs or ramfs'):
        scratch.memory_root(tmp_path, disk, name=name)
    assert not (disk / name).exists()


def test_a_group_writable_memory_root_is_refused(memory, tmp_path):
    scratch, base, name = memory
    (base / name).mkdir(mode=0o700)
    os.chmod(base / name, 0o770)
    with pytest.raises(layout.Refused, match='writable by others'):
        scratch.memory_root(tmp_path, base, name=name)


def test_a_memory_root_of_another_user_is_refused(memory, tmp_path, monkeypatch):
    scratch, base, name = memory
    (base / name).mkdir(mode=0o700)
    monkeypatch.setattr(layout.os, 'getuid', lambda: os.geteuid() + 4242)  # as if another user made it
    with pytest.raises(layout.Refused, match='not this user'):
        scratch.memory_root(tmp_path, base, name=name)


def test_a_link_in_place_of_the_memory_root_is_refused(memory, tmp_path):
    scratch, base, name = memory
    real = tmp_path / 'elsewhere'
    real.mkdir(mode=0o700)
    (base / name).symlink_to(real, target_is_directory=True)
    try:
        with pytest.raises(layout.Refused, match='link or not a directory'):
            scratch.memory_root(tmp_path, base, name=name)
    finally:
        (base / name).unlink()


def test_reserved_run_ids_retry_a_repeated_random_candidate_without_touching_evidence(tmp_path, monkeypatch):
    from types import SimpleNamespace
    root = layout.Root(project(tmp_path))
    candidates = iter(('abcd', 'abcd', 'abcd', 'bcde'))
    real_token_hex = layout.secrets.token_hex
    monkeypatch.setattr(layout.secrets, 'token_hex',
                        lambda count: next(candidates) if count == 2 else real_token_hex(count))
    monkeypatch.setattr(layout, 'time', SimpleNamespace(strftime=lambda fmt: '20200101-000000'))
    try:
        first = layout.reserve_run_id(root)
        evidence = root.path / layout.RESULTS / first / 'result.json'
        evidence.write_text('first result')
        second = layout.reserve_run_id(root)
        assert first == '20200101-000000-abcd' and second == '20200101-000000-bcde'
        assert evidence.read_text() == 'first result'
        assert sorted(os.listdir(root.path / layout.RESULTS)) == [first, second]
        assert os.stat(root.path / layout.RESULTS / second).st_mode & 0o777 == 0o700
    finally:
        root.close()


def test_slot_notes_follow_the_reserved_run_in_every_held_slot(tmp_path):
    root = layout.Root(project(tmp_path))
    slot = layout.Slot(root)
    try:
        slot.acquire({'verb': 'run', 'run': None}, count=2)
        run = layout.reserve_run_id(root)
        slot.describe({'verb': 'run', 'run': run})
        assert slot.holder().count('run ' + run) == 2
    finally:
        slot.release()
        root.close()
