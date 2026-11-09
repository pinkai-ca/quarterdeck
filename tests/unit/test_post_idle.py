"""Idle reads reuse parsed evidence; filesystem changes and failed scans still speak."""

from dataclasses import replace
import json
import os
from pathlib import Path

import pytest

from quarterdeck import post


def test_unchanged_immutable_store_lookups_do_not_list_the_directory(tmp_path, monkeypatch):
    store = post.Store(tmp_path)
    order = store.new('order', 'issuer', 'worker', 'synthetic')
    store.all()
    scans = []
    original = os.scandir

    def scan(path):
        scans.append(path)
        return original(path)

    monkeypatch.setattr(os, 'scandir', scan)
    for _ in range(5):
        assert store.is_open(order)
        assert post.inbox_reading(store, 'worker')['open'] == [order]
    assert scans == [], 'nested inbox/closure lookups must not enumerate unchanged immutable posts'
    other = post.Store(tmp_path)
    other.new('report', 'foreign', 'issuer', 'not a closing report', re=order.id)
    assert store.is_open(order)
    report = other.new('report', 'worker', 'issuer', 'done', re=order.id)
    assert store.closing_report(order) == report and not store.is_open(order)
    assert scans


def test_store_replacement_and_failed_scan_do_not_serve_old_posts(tmp_path, monkeypatch):
    store = post.Store(tmp_path)
    order = store.new('order', 'issuer', 'worker', 'synthetic')
    assert store.all() == [order]
    stamp = store.dir.stat()
    # An atomic replacement is visible even if the directory mtime is restored.
    changed = replace(order, body='updated synthetic')
    path = store.dir / f'{order.id}.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(changed.to_json())
    os.replace(temporary, path)
    os.utime(store.dir, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert store.all() == [changed]
    with monkeypatch.context() as patch:
        original = Path.stat

        def stat(path, *args, **kwargs):
            if path == store.dir:
                raise PermissionError('synthetic unreadable store')
            return original(path, *args, **kwargs)

        patch.setattr(Path, 'stat', stat)
        with pytest.raises(PermissionError):
            store.all()
    path.unlink()
    assert store.all() == []
    assert post.Store(tmp_path).all() == []


def test_unchanged_attempts_are_not_reparsed_and_callers_get_a_copy(tmp_path, monkeypatch):
    store, attempts = post.Store(tmp_path), post.Attempts(tmp_path)
    order = store.new('order', 'issuer', 'worker', 'synthetic')
    opened = attempts.open(order, 'worker', None, None, None)
    done = attempts.close(opened, 'queued')
    assert attempts.all() == [done]
    reads = []
    original = Path.read_text

    def read(path, *args, **kwargs):
        if path.parent == attempts.dir:
            reads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', read)
    for _ in range(5):
        assert attempts.queued_ids() == {order.id}
        assert attempts.undelivered(store, 'worker') == []
        attempts.all().clear()
    assert reads == [], 'unchanged history must not be parsed for every seat and verdict lookup'
    # An independent watch process closes the same file; the service must see it.
    post.Attempts(tmp_path).close(opened, 'watch')
    assert attempts.latest() == {order.id: 'watch'}
    assert reads
    later = post.Attempts(tmp_path).open(order, 'worker', None, None, None)
    assert attempts.latest() == {order.id: None}, 'a new unfinished attempt retains uncertainty'
    assert len(attempts.all()) == 2
    post.Attempts(tmp_path).close(later, 'blind')
    assert attempts.blind_ids() == {order.id}


@pytest.mark.parametrize('atomic', [False, True])
def test_attempt_same_path_same_size_rewrite_is_seen_even_with_restored_mtime(tmp_path, atomic):
    store, attempts = post.Store(tmp_path), post.Attempts(tmp_path)
    order = store.new('order', 'issuer', 'worker', 'synthetic')
    opened = attempts.open(order, 'worker', None, None, None)
    attempts.close(opened, 'blind')
    assert attempts.blind_ids() == {order.id}
    stamp = opened.path.stat()
    text = opened.path.read_text().replace('"blind"', '"watch"')
    assert len(text.encode()) == stamp.st_size
    path = opened.path.with_suffix('.tmp') if atomic else opened.path
    path.write_text(text)
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    if atomic:
        os.replace(path, opened.path)
    assert attempts.latest() == {order.id: 'watch'}
    assert post.Attempts(tmp_path).latest() == attempts.latest(), 'restart reads the same evidence'
    opened.path.unlink()
    assert attempts.all() == []


def test_failed_attempt_scan_or_parse_never_returns_cached_success(tmp_path, monkeypatch):
    store, attempts = post.Store(tmp_path), post.Attempts(tmp_path)
    order = store.new('order', 'issuer', 'worker', 'synthetic')
    opened = attempts.open(order, 'worker', None, None, None)
    attempts.close(opened, 'queued')
    assert attempts.queued_ids() == {order.id}
    with monkeypatch.context() as patch:
        def fail(path):
            raise PermissionError('synthetic unreadable directory')
        patch.setattr(os, 'scandir', fail)
        with pytest.raises(PermissionError):
            attempts.all()
    opened.path.write_text('{broken')
    with pytest.raises(json.JSONDecodeError):
        attempts.all()
    attempts._write(replace(opened, result='blind'))
    assert attempts.blind_ids() == {order.id}
