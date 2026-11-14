"""A link under runtime/post is a hold. The outside target is left unchanged.

The directories and symlinks are real. Nothing is written through the link, and
the link is left in place.
"""

from __future__ import annotations

import os
from pathlib import Path
import stat

import pytest

from quarterdeck import durable, pending, post, post_dir
from quarterdeck.ship import ShipError


def runtime_of(folder: Path) -> Path:
    runtime = folder / 'ship' / 'runtime'
    (runtime / 'post').mkdir(parents=True)
    return runtime


def outside_of(folder: Path) -> Path:
    target = folder / 'outside'
    target.mkdir()
    (target / 'kept.txt').write_text('kept')
    return target


def snapshot(folder: Path):
    found = []
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        dirnames.sort()
        for name in sorted(dirnames + filenames):
            path = Path(dirpath) / name
            info = path.lstat()
            body = path.read_bytes() if stat.S_ISREG(info.st_mode) else None
            found.append((str(path.relative_to(folder)), stat.S_IFMT(info.st_mode),
                          info.st_size, info.st_mtime_ns, body))
    return tuple(found)


def held(error, link: Path) -> None:
    text = str(error)
    assert text.startswith(post_dir.LINK_HELD) and str(link) in text, text


def test_ordinary_store_inbox_lock_and_admission(tmp_path):
    runtime = runtime_of(tmp_path)
    store = post.Store(runtime)
    message = store.new('order', 'desk', 'captain', 'Ordinary body.')
    saved = store.dir / f'{message.id}.json'
    assert saved.is_file() and not saved.is_symlink()
    assert store.get(message.id).body == 'Ordinary body.'
    rendered = post.render_inbox(runtime, store, 'captain')
    assert rendered.name == 'captain.md' and 'Ordinary body.' in rendered.read_text()
    assert list((runtime / 'post' / 'inbox').glob('*.tmp')) == []
    post.render_inbox(runtime, store, 'captain')
    with post.admission(runtime, 'captain'):
        pass
    attempts = post.Attempts(runtime)
    with attempts._locked():
        pass
    lock = runtime / 'post' / 'attempts' / '.lock'
    assert lock.is_file() and not lock.is_symlink()


def test_store_directory_link_holds_on_write_and_listing(tmp_path):
    runtime = runtime_of(tmp_path)
    target = outside_of(tmp_path)
    link = runtime / 'post' / 'store'
    link.symlink_to(target, target_is_directory=True)
    before = snapshot(target)
    with pytest.raises(ShipError) as caught:
        post.Store(runtime).new('order', 'desk', 'captain', 'Do not write through the store link.')
    held(caught.value, link)
    with pytest.raises(ShipError) as listed:
        post.Store(runtime).all()
    held(listed.value, link)
    assert snapshot(target) == before and link.is_symlink()


def test_inbox_directory_link_holds(tmp_path):
    runtime = runtime_of(tmp_path)
    target = outside_of(tmp_path)
    link = runtime / 'post' / 'inbox'
    link.symlink_to(target, target_is_directory=True)
    before = snapshot(target)
    with pytest.raises(ShipError) as caught:
        post.render_inbox(runtime, post.Store(runtime), 'captain')
    held(caught.value, link)
    assert snapshot(target) == before and link.is_symlink()


def test_inbox_temp_link_is_not_written(tmp_path):
    runtime = runtime_of(tmp_path)
    target = outside_of(tmp_path)
    inbox = runtime / 'post' / 'inbox'
    inbox.mkdir()
    link = inbox / 'captain.tmp'
    link.symlink_to(target / 'kept.txt')
    before = snapshot(target)
    rendered = post.render_inbox(runtime, post.Store(runtime), 'captain')
    assert rendered.is_file() and not rendered.is_symlink()
    assert link.is_symlink() and snapshot(target) == before
    assert list(inbox.glob('*.tmp')) == [link]


def test_attempts_directory_link_holds(tmp_path):
    runtime = runtime_of(tmp_path)
    target = outside_of(tmp_path)
    link = runtime / 'post' / 'attempts'
    link.symlink_to(target, target_is_directory=True)
    before = snapshot(target)
    with pytest.raises(ShipError) as caught:
        post.Attempts(runtime).all()
    held(caught.value, link)
    assert snapshot(target) == before and link.is_symlink()


def test_attempts_lock_link_holds(tmp_path):
    runtime = runtime_of(tmp_path)
    target = outside_of(tmp_path)
    attempts = runtime / 'post' / 'attempts'
    attempts.mkdir()
    link = attempts / '.lock'
    link.symlink_to(target / 'kept.txt')
    before = snapshot(target)
    with pytest.raises(ShipError) as caught:
        with post.Attempts(runtime)._locked():
            pass
    held(caught.value, link)
    assert snapshot(target) == before and link.is_symlink()


def test_admission_directory_link_holds(tmp_path):
    runtime = runtime_of(tmp_path)
    target = outside_of(tmp_path)
    link = runtime / 'post' / 'admission'
    link.symlink_to(target, target_is_directory=True)
    before = snapshot(target)
    with pytest.raises(ShipError) as caught:
        with post.admission(runtime, 'captain'):
            pass
    held(caught.value, link)
    assert snapshot(target) == before and link.is_symlink()
    assert list(target.rglob('*.lock')) == []


def test_pending_journal_directory_link_holds(tmp_path):
    runtime = runtime_of(tmp_path)
    target = outside_of(tmp_path)
    link = runtime / 'post' / 'pending'
    link.symlink_to(target, target_is_directory=True)
    before = snapshot(target)
    proof = pending.Proof(link / 'captain' / 'ord-1.json', {})
    with pytest.raises(ShipError) as caught:
        proof.save(phase='prepared')
    held(caught.value, link)
    assert snapshot(target) == before and link.is_symlink()


@pytest.mark.parametrize('name', [
    'pending', 'retired', 'transient', 'lifecycle', 'groups', 'group-holds',
    'structure-waits', 'cascade-holds', 'auth-relaunch', 'imports', 'outbound',
    'fetched', 'addressed',
])
def test_post_subfolder_link_holds_on_write(tmp_path, name):
    runtime = runtime_of(tmp_path)
    target = outside_of(tmp_path)
    link = runtime / 'post' / name
    link.symlink_to(target, target_is_directory=True)
    before = snapshot(target)
    with pytest.raises(ShipError) as caught:
        durable.replace_file(link / 'written.json', '{}\n')
    held(caught.value, link)
    assert snapshot(target) == before and link.is_symlink()
    assert list(target.rglob('*.json')) == []


def test_post_directory_itself_is_a_hold(tmp_path):
    runtime = runtime_of(tmp_path)
    target = outside_of(tmp_path)
    post_dir_path = runtime / 'post'
    for child in post_dir_path.iterdir():
        child.rmdir()
    post_dir_path.rmdir()
    post_dir_path.symlink_to(target, target_is_directory=True)
    before = snapshot(target)
    with pytest.raises(ShipError) as caught:
        post.Store(runtime).new('order', 'desk', 'captain', 'Do not write through post.')
    held(caught.value, post_dir_path)
    assert snapshot(target) == before and post_dir_path.is_symlink()


def test_file_link_is_not_replaced(tmp_path):
    runtime = runtime_of(tmp_path)
    target = outside_of(tmp_path)
    store = runtime / 'post' / 'store'
    store.mkdir()
    link = store / 'message.json'
    link.symlink_to(target / 'kept.txt')
    before = snapshot(target)
    with pytest.raises(ShipError) as caught:
        durable.replace_file(link, '{}\n')
    held(caught.value, link)
    assert snapshot(target) == before and link.is_symlink()
