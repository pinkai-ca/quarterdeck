"""A link inside runtime/post is a hold that names the folder.

The directories and symlinks are real files under /dev/shm. The tick is one
fleet_post.run. Nothing is written through the link, and the link is left in place.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import tempfile

import pytest

from quarterdeck import fleet_post as fleet, post, ship as shipmod


TOML = '''[ship]
name="Display name"
operator="person"
code="checkout"
[pools.fake]
provider="fake"
[sessions.crew]
title="Synthetic crew"
[seats.captain]
session="crew"
role="captain"
harness="fake"
pool="fake"
[seats.writer]
session="crew"
role="worker"
harness="fake"
pool="fake"
'''


@pytest.fixture
def root():
    folder = Path(tempfile.mkdtemp(prefix='qd-post-link-', dir='/dev/shm'))
    try:
        yield folder
    finally:
        shutil.rmtree(folder)


def make_ship(folder, name):
    ship = folder / 'ships' / name
    ship.mkdir(parents=True)
    (ship / 'ship.toml').write_text(TOML)
    return shipmod.load(ship)


def pair(folder):
    return make_ship(folder, 'alpha'), make_ship(folder, 'beta')


def snapshot(folder):
    found = []
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        dirnames.sort()
        for name in sorted(dirnames + filenames):
            path = Path(dirpath) / name
            info = path.lstat()
            body = path.read_bytes() if stat.S_ISREG(info.st_mode) else None
            found.append((str(path.relative_to(folder)), stat.S_IFMT(info.st_mode), info.st_size, body))
    return tuple(found)


def outside_of(folder, name):
    target = folder / name
    target.mkdir()
    (target / 'kept.txt').write_text('kept')
    return target


def held(errors, link):
    text = '\n'.join(errors)
    assert 'post link held at ' in text and str(link) in text, text
    return text


def test_ordinary_round_trip_still_publishes_real_directories(root):
    left, right = pair(root)
    source, destination = post.Store(left.runtime), post.Store(right.runtime)
    order = source.new('order', 'writer', 'captain@beta', 'Ordinary body across a real directory.')
    assert fleet.run(left, source) == []
    assert fleet.run(right, destination) == []
    assert (destination.dir / f'{order.id}.json').read_bytes() == (source.dir / f'{order.id}.json').read_bytes()
    received = destination.get(order.id)
    assert received.to == 'captain' and received.sender == 'writer@alpha'
    for path in (left.runtime / 'post' / 'outbound' / 'beta',
                 right.runtime / 'post' / 'store',
                 right.runtime / 'post' / 'imports',
                 right.runtime / 'post' / 'fetched' / 'alpha'):
        assert path.is_dir() and not path.is_symlink()


def test_outbound_peer_link_holds_and_writes_nothing(root):
    left, _right = pair(root)
    source = post.Store(left.runtime)
    source.new('order', 'writer', 'captain@beta', 'Do not publish through the peer link.')
    target = outside_of(root, 'outside')
    link = left.runtime / 'post' / 'outbound' / 'beta'
    link.symlink_to(target, target_is_directory=True)
    before = snapshot(target)
    errors = fleet.run(left, source)
    assert snapshot(target) == before and link.is_symlink()
    held(errors, link)
    assert list(target.rglob('*.json')) == []


def test_store_link_holds_and_writes_nothing(root):
    left, right = pair(root)
    source = post.Store(left.runtime)
    order = source.new('order', 'writer', 'captain@beta', 'Do not fetch through the store link.')
    assert fleet.run(left, source) == []
    (right.runtime / 'post').mkdir(parents=True)
    target = outside_of(root, 'outside')
    link = right.runtime / 'post' / 'store'
    link.symlink_to(target, target_is_directory=True)
    before = snapshot(target)
    errors = fleet.run(right, post.Store(right.runtime))
    assert snapshot(target) == before and link.is_symlink()
    held(errors, link)
    assert not (target / f'{order.id}.json').exists()


def test_imports_link_holds_and_writes_nothing(root):
    left, right = pair(root)
    source = post.Store(left.runtime)
    order = source.new('order', 'writer', 'captain@beta', 'Do not fetch through the imports link.')
    assert fleet.run(left, source) == []
    (right.runtime / 'post').mkdir(parents=True)
    target = outside_of(root, 'outside')
    link = right.runtime / 'post' / 'imports'
    link.symlink_to(target, target_is_directory=True)
    before = snapshot(target)
    errors = fleet.run(right, post.Store(right.runtime))
    assert snapshot(target) == before and link.is_symlink()
    held(errors, link)
    assert not (target / f'{order.id}.json').exists()


def test_outbound_directory_link_holds_on_publish(root):
    left, _right = pair(root)
    source = post.Store(left.runtime)
    source.new('order', 'writer', 'captain@beta', 'Do not publish through outbound itself.')
    target = outside_of(root, 'outside')
    link = left.runtime / 'post' / 'outbound'
    link.rmdir()
    link.symlink_to(target, target_is_directory=True)
    before = snapshot(target)
    errors = fleet.run(left, source)
    assert snapshot(target) == before and link.is_symlink()
    held(errors, link)
    assert list(target.rglob('*.json')) == []


def test_outbound_directory_link_is_not_listed(root):
    left, right = pair(root)
    source = post.Store(left.runtime)
    order = source.new('order', 'writer', 'captain@beta', 'Do not list through outbound itself.')
    assert fleet.run(left, source) == []
    offer = left.runtime / 'post' / 'outbound' / 'beta' / f'{order.id}.json'
    saved = offer.read_bytes()
    target = outside_of(root, 'outside')
    (target / 'beta').mkdir()
    (target / 'beta' / offer.name).write_bytes(saved)
    link = left.runtime / 'post' / 'outbound'
    moved = root / 'moved-outbound'
    link.rename(moved)
    link.symlink_to(target, target_is_directory=True)
    before = snapshot(target)
    errors = fleet.run(right, post.Store(right.runtime))
    assert snapshot(target) == before and link.is_symlink()
    held(errors, link)
    assert not (right.runtime / 'post' / 'store' / f'{order.id}.json').exists()


def test_offer_file_link_is_not_read(root):
    left, right = pair(root)
    source = post.Store(left.runtime)
    order = source.new('order', 'writer', 'captain@beta', 'Do not read an offer file through a link.')
    assert fleet.run(left, source) == []
    offer = left.runtime / 'post' / 'outbound' / 'beta' / f'{order.id}.json'
    target = outside_of(root, 'outside')
    copied = target / 'offer.json'
    copied.write_bytes(offer.read_bytes())
    offer.unlink()
    offer.symlink_to(copied)
    before = snapshot(target)
    errors = fleet.run(right, post.Store(right.runtime))
    assert snapshot(target) == before and offer.is_symlink()
    held(errors, offer)
    assert not (right.runtime / 'post' / 'store' / f'{order.id}.json').exists()
