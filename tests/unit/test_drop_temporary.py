"""A publish's temporary in a drop folder is no message: skipped while young, held under its own reason once stale."""

import os
import time

import pytest

from quarterdeck import post, post_dir


@pytest.fixture
def drop(tmp_path):
    runtime = tmp_path / 'runtime'
    store = post.Store(runtime)
    order = store.new('order', 'ship', 'worker', 'ordinary synthetic task')
    report = store.prepare('report', 'worker', 'ship', 'synthetic report', re=order.id)
    folder = post.drop_dir(runtime, 'worker')
    post_dir.ensure_directory(folder)
    return store, order, report, folder


def alarms(store):
    return post.drop_alarms(store.dir.parent.parent)


def test_the_publish_writes_its_temporary_by_the_one_shared_form(drop, monkeypatch):
    store, order, report, folder = drop
    made = []
    original = post_dir.temporary_name
    monkeypatch.setattr(post_dir, 'temporary_name', lambda name: made.append(original(name)) or made[-1])
    post.write_drop(store.dir.parent.parent, report)
    assert len(made) == 1 and post_dir.is_temporary(made[0]) and made[0].startswith(f'.{report.id}.json.')


def test_a_temporary_present_during_a_pass_is_neither_filed_nor_held(drop):
    store, order, report, folder = drop
    temporary = folder / post_dir.temporary_name(f'{report.id}.json')
    temporary.write_text(report.to_json()[:20])  # a publish part written, not yet renamed in
    post.write_drop(store.dir.parent.parent, store.prepare('ack', 'worker', 'ship', 'beside it', re=order.id))
    post.file_drops(store)
    assert alarms(store) == [], 'no false alarm while a publish is in flight'
    assert temporary.is_file(), 'left for its writer to rename in'
    assert [m.kind for m in store.all()] == ['order', 'ack'], 'the pass still files the rest of the folder'
    temporary.unlink()
    post.write_drop(store.dir.parent.parent, report)  # the writer finishes: its whole publish, renamed in
    post.file_drops(store)
    assert alarms(store) == [] and store.get(report.id) == report


def test_a_stale_temporary_is_held_under_its_own_reason_and_kept(drop):
    store, order, report, folder = drop
    temporary = folder / post_dir.temporary_name(f'{report.id}.json')
    temporary.write_text(report.to_json()[:20])
    old = time.time() - post.DROP_TEMPORARY_SECONDS - 5
    os.utime(temporary, (old, old))  # its writer died mid-publish
    post.file_drops(store)
    assert post.DROP_TEMPORARY_REASON in post.DROP_HOLD_REASONS
    assert alarms(store) == [f'post drop held: drop/worker: {post.DROP_TEMPORARY_REASON}']
    assert temporary.is_file() and store.all() == [order], 'named, never filed nor removed'
    temporary.unlink()  # a person clears it: the next pass sweeps the hold
    post.file_drops(store)
    assert alarms(store) == []


def test_a_malformed_message_not_in_the_temporary_form_is_still_not_a_message(drop):
    store, order, report, folder = drop
    (folder / f'{report.id}.json').write_text('{not json')
    post.file_drops(store)
    assert alarms(store) == ['post drop held: drop/worker: not a message']


@pytest.mark.parametrize('name', [
    'x.json.ab12cd34.tmp',        # no leading dot
    '.x.json.ab12cd34.tmp.bak',   # another suffix after it
    '.x.json.ab12cd34.tmpx',      # a longer suffix
    '.x.json.ab12cd3.tmp',        # seven hex digits
    '.x.json.ab12cd345.tmp',      # nine
    '.x.json.AB12CD34.tmp',       # upper case, which token_hex never writes
    '..ab12cd34.tmp',             # no name before the random part
    '.x.json-ab12cd34.tmp',       # another separator
])
def test_a_name_that_only_looks_like_a_temporary_is_read_and_held_as_not_a_message(drop, name):
    store, order, report, folder = drop
    assert not post_dir.is_temporary(name)
    (folder / name).write_text('{not json')
    post.file_drops(store)
    assert alarms(store) == ['post drop held: drop/worker: not a message']
