"""Ordinary message-size boundaries through the shared filing reader."""

from contextlib import contextmanager
from dataclasses import replace
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import check, group_post, post, post_dir, ship as shipmod


def sized(message, size):
    empty = replace(message, body='')
    result = replace(empty, body='x' * (size - len(empty.to_json().encode())))
    assert len(result.to_json().encode()) == size
    return result


def ordinary(tmp_path, size, caller):
    runtime = tmp_path / 'runtime'
    store = post.Store(runtime)
    order = store.new('order', 'ship', 'worker', 'ordinary synthetic task')
    message = sized(store.prepare('report', 'worker', 'ship', '', re=order.id), size)
    path = post.write_drop(runtime, message)

    def file():
        if caller == 'host':
            seat = SimpleNamespace(name='worker', ship=SimpleNamespace(runtime=runtime))
            check.closing_report(seat, store, order)
        elif caller == 'slice':
            filing = post.FilingSlice(seconds=0, operations=1)
            for _ in range(20):
                post.file_drops(store, filing=filing)
                if filing.steps is None:
                    break
            assert filing.steps is None
        else:
            post.file_drops(store)

    return store, order, message, path, file


def held(store, order, path):
    assert path.is_file()
    assert store.all() == [order]
    assert store.is_open(order)
    reason = post.DROP_SIZE_REASON
    assert reason in post.DROP_HOLD_REASONS
    assert post.drop_alarms(store.dir.parent.parent) == [f'post drop held: drop/worker: {reason}']


@pytest.mark.parametrize('caller', ['full', 'slice', 'host'])
def test_message_at_byte_limit_files_and_closes_its_order(tmp_path, caller):
    store, order, message, path, file = ordinary(tmp_path, post.DROP_MAX_BYTES, caller)
    file()
    assert not path.exists()
    assert store.get(message.id) == message
    assert not store.is_open(order)
    assert post.drop_alarms(store.dir.parent.parent) == []


@pytest.mark.parametrize('caller', ['full', 'slice', 'host'])
def test_one_byte_over_is_held_before_the_file_is_opened(tmp_path, monkeypatch, caller):
    store, order, message, path, file = ordinary(tmp_path, post.DROP_MAX_BYTES + 1, caller)
    opened = os.open
    def observing(name, *args, **kwargs):
        assert str(name) != path.name, 'oversize drop was opened for reading'
        return opened(name, *args, **kwargs)
    monkeypatch.setattr(post_dir.os, 'open', observing)
    file()
    held(store, order, path)


@pytest.mark.parametrize('caller', ['full', 'slice', 'host'])
def test_growth_after_the_size_check_stays_bounded_and_is_held(tmp_path, monkeypatch, caller):
    store, order, message, path, file = ordinary(tmp_path, post.DROP_MAX_BYTES, caller)
    fdopen = os.fdopen
    reads = []

    @contextmanager
    def growing(descriptor, *args, **kwargs):
        with fdopen(descriptor, *args, **kwargs) as stream:
            if os.readlink(f'/proc/self/fd/{descriptor}') != str(path):
                yield stream
                return
            # The reader's pre-open stat and pre-read fstat have both completed.
            with path.open('ab') as writer:
                writer.write(b' ')
            class Observed:
                def read(self, limit=-1):
                    reads.append(limit)
                    return stream.read(limit)
            yield Observed()

    monkeypatch.setattr(post_dir.os, 'fdopen', growing)
    file()
    assert reads == [post.DROP_MAX_BYTES]
    assert path.stat().st_size == post.DROP_MAX_BYTES + 1
    held(store, order, path)


@pytest.mark.parametrize('part', ['root', 'intent'])
@pytest.mark.parametrize('extra', [0, 1])
def test_staged_group_drop_reads_obey_the_same_byte_limit(tmp_path, part, extra):
    template = Path(__file__).parents[2] / 'ships/forum/ship.toml'
    (tmp_path / 'ship.toml').write_text(template.read_text())
    ship = shipmod.load(tmp_path)
    store = post.Store(ship.runtime)
    root, _ = group_post.stage(ship, store, 'reviewers', 'lead', '')
    folder = post.drop_dir(ship.runtime, 'lead')
    if part == 'root':
        root = sized(root, post.DROP_MAX_BYTES + extra)
        (folder / f'{root.id}.json').write_text(root.to_json())
    else:
        intent = folder / f'{root.id}.group.json'
        data = intent.read_bytes()
        intent.write_bytes(data + b' ' * (post.DROP_MAX_BYTES + extra - len(data)))
    post.file_drops(store, ship)
    if extra:
        assert store.all() == []
        assert len(list(folder.glob('*.json'))) == 2
        assert post.drop_alarms(ship.runtime) == [f'post drop held: drop/lead: {post.DROP_SIZE_REASON}']
    else:
        assert store.get(root.id) == root
        assert len(store.all()) == 1 + len(ship.groups['reviewers'].members)
        assert not list(folder.glob('*.json'))
        assert post.drop_alarms(ship.runtime) == []
