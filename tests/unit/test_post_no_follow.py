"""A link under runtime/post is a hold. The outside target is left unchanged.

The directories and symlinks are real. Nothing is written through the link, and
the link is left in place.
"""

from __future__ import annotations

import os
from pathlib import Path
import stat
import threading
from types import SimpleNamespace

import pytest

from quarterdeck import durable, pane_obstruction as pane_meta, pending, post, post_dir, ship as shipmod, watch
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


def at_once(action):
    """Run action. It must finish within two seconds; a blocked open fails the test."""
    box = []

    def run():
        try:
            box.append((None, action()))
        except Exception as error:
            box.append((error, None))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(2)
    assert not thread.is_alive()
    error, value = box[0]
    if error is not None:
        raise error
    return value


def opened_names(monkeypatch, name):
    seen = []
    real_open = os.open

    def counted(path, flags, *args, **kwargs):
        if Path(os.fsdecode(path)).name == name:
            seen.append(name)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, 'open', counted)
    return seen


def test_a_fifo_at_the_pending_marker_is_unread_without_opening(tmp_path, monkeypatch):
    runtime = runtime_of(tmp_path)
    directory = runtime / 'post' / 'pending' / 'captain'
    directory.mkdir(parents=True)
    marker = directory / pane_meta.NAME
    os.mkfifo(marker)
    seen = opened_names(monkeypatch, marker.name)
    seat = SimpleNamespace(ship=SimpleNamespace(runtime=runtime), name='captain')
    assert at_once(lambda: pane_meta.reading(seat)) is None
    assert seen == [] and marker.is_fifo()


def test_a_fifo_in_the_store_is_unread_and_a_real_message_remains(tmp_path, monkeypatch):
    runtime = runtime_of(tmp_path)
    message = post.Store(runtime).new('order', 'desk', 'captain', 'Ordinary body.')
    fifo = runtime / 'post' / 'store' / 'fifo.json'
    os.mkfifo(fifo)
    seen = opened_names(monkeypatch, fifo.name)
    found = at_once(lambda: post.Store(runtime).all())
    assert [item.id for item in found] == [message.id]
    assert seen == [] and fifo.is_fifo()
    with pytest.raises(ShipError, match='post path is not a regular file'):
        at_once(lambda: post_dir.read_bytes(fifo))


def test_rename_refuses_an_existing_name_and_a_link(tmp_path):
    runtime = runtime_of(tmp_path)
    target = outside_of(tmp_path)
    folder = runtime / 'post' / 'pending'
    folder.mkdir()
    source = folder / 'one.json'
    source.write_text('one')
    destination = folder / 'two.json'
    destination.write_text('two')
    with pytest.raises(ShipError, match='post destination already exists'):
        post_dir.rename(source, destination)
    assert source.read_text() == 'one' and destination.read_text() == 'two'
    link = folder / 'link.json'
    link.symlink_to(target / 'kept.txt')
    before = (target / 'kept.txt').read_bytes()
    with pytest.raises(ShipError) as caught:
        post_dir.rename(source, link)
    held(caught.value, link)
    assert source.read_text() == 'one' and link.is_symlink() and (target / 'kept.txt').read_bytes() == before
    source_link = folder / 'source.json'
    source_link.symlink_to(target / 'kept.txt')
    with pytest.raises(ShipError) as caught:
        post_dir.rename(source_link, folder / 'fresh.json')
    held(caught.value, source_link)
    assert source_link.is_symlink() and not (folder / 'fresh.json').exists()


def declared_ship(folder: Path):
    (folder / 'ship.toml').write_text('[ship]\ncode="checkout"\nname="synthetic"\n')
    ship = shipmod.load(folder)
    ship.runtime.mkdir(parents=True)
    (ship.runtime / 'post').mkdir()
    return ship


def test_a_store_link_is_named_on_the_status_line(tmp_path):
    ship = declared_ship(tmp_path)
    target = outside_of(tmp_path)
    link = ship.runtime / 'post' / 'store'
    link.symlink_to(target, target_is_directory=True)
    line = watch.facts(ship, SimpleNamespace()).line()
    assert f'{post_dir.LINK_HELD}{link}' in line


def test_an_imports_link_is_named_on_the_status_line(tmp_path):
    ship = declared_ship(tmp_path)
    target = outside_of(tmp_path)
    (ship.runtime / 'post' / 'store').mkdir()
    link = ship.runtime / 'post' / 'imports'
    link.symlink_to(target, target_is_directory=True)
    line = watch.facts(ship, SimpleNamespace()).line()
    assert f'{post_dir.LINK_HELD}{link}' in line
    with pytest.raises(ShipError) as caught:
        post.Store(ship.runtime).new('order', 'desk', 'captain', 'Still listed.')
    held(caught.value, link)
    line = watch.facts(ship, SimpleNamespace()).line()
    assert f'{post_dir.LINK_HELD}{link}' in line
