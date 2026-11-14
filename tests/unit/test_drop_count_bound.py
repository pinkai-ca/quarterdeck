"""Ordinary per-seat queue boundaries and bounded metadata readings."""

from pathlib import Path
import os
from types import SimpleNamespace

import pytest

from quarterdeck import check, group_post, post, post_dir, ship as shipmod


def queue(store, count, sender='worker'):
    messages = [store.prepare('report', sender, 'ship', f'ordinary queued report {n}')
                for n in range(count)]
    paths = [post.write_drop(store.dir.parent.parent, message) for message in messages]
    return messages, paths


def file(store, caller, ship=None):
    if caller == 'host':
        seat = SimpleNamespace(name='worker', ship=SimpleNamespace(runtime=store.dir.parent.parent))
        order = store.new('order', 'ship', 'worker', 'synthetic host check')
        check.closing_report(seat, store, order)
    elif caller == 'slice':
        filing = post.FilingSlice(seconds=0, operations=1)
        for _ in range(4 * post.DROP_MAX_FILES + 30):
            post.file_drops(store, ship, filing=filing)
            if filing.steps is None:
                break
        assert filing.steps is None
    else:
        post.file_drops(store, ship)


@pytest.mark.parametrize('caller', ['full', 'slice', 'host'])
@pytest.mark.parametrize('extra', [0, 1])
def test_count_boundary_files_or_holds_the_whole_folder(tmp_path, caller, extra):
    store = post.Store(tmp_path / 'runtime')
    messages, paths = queue(store, post.DROP_MAX_FILES + extra)
    other, other_paths = queue(store, 1, sender='z-last')
    file(store, caller)
    if extra:
        assert all(path.exists() for path in paths)
        assert all(store.get(message.id) is None for message in messages)
        assert post.DROP_COUNT_REASON in post.DROP_HOLD_REASONS
        assert post.drop_alarms(tmp_path / 'runtime') == [
            f'post drop held: drop/worker: {post.DROP_COUNT_REASON}']
        # Removing one queued item lets the same unchanged filing door recover.
        paths[-1].unlink()
        file(store, caller)
        assert all(not path.exists() for path in paths)
        assert post.drop_alarms(tmp_path / 'runtime') == []
    else:
        assert all(not path.exists() for path in paths)
        assert all(store.get(message.id) == message for message in messages)
        assert post.drop_alarms(tmp_path / 'runtime') == []
    # A held folder cannot starve the following seat; scoped hosts leave peers alone.
    assert other_paths[0].exists() == (caller == 'host')
    assert (store.get(other[0].id) is None) == (caller == 'host')


@pytest.mark.parametrize('caller', ['full', 'slice'])
@pytest.mark.parametrize('extra', [0, 1])
def test_group_scan_uses_the_same_folder_count(tmp_path, caller, extra):
    template = Path(__file__).parents[2] / 'ships/forum/ship.toml'
    (tmp_path / 'ship.toml').write_text(template.read_text())
    ship = shipmod.load(tmp_path)
    store = post.Store(ship.runtime)
    root, _ = group_post.stage(ship, store, 'reviewers', 'lead', 'ordinary group task')
    queue(store, post.DROP_MAX_FILES - 2 + extra, sender='lead')
    file(store, caller, ship)
    assert (store.get(root.id) is None) == bool(extra)
    if extra:
        assert post.drop_alarms(ship.runtime) == [
            f'post drop held: drop/lead: {post.DROP_COUNT_REASON}']
        assert len(list(post.drop_dir(ship.runtime, 'lead').iterdir())) == post.DROP_MAX_FILES + 1
    else:
        assert not list(post.drop_dir(ship.runtime, 'lead').iterdir())


@pytest.mark.parametrize('extra', [0, 1, 64])
def test_status_listing_stops_at_count_plus_one_without_message_reads(tmp_path, monkeypatch, extra):
    runtime = tmp_path / 'runtime'
    folder = post.drop_dir(runtime, 'worker')
    folder.mkdir(parents=True)
    # Listing cost includes every entry; no filename or body belongs in status.
    for n in range(post.DROP_MAX_FILES + extra):
        (folder / f'ordinary-{n}.json').write_text('private ordinary body')
    scandir = os.scandir
    visited = []
    class Observed:
        def __init__(self, target):
            self.scan = scandir(target)
            self.watched = Path(target).resolve() == folder
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.scan.close()
        def __iter__(self):
            for entry in self.scan:
                if self.watched:
                    visited.append(entry.name)
                yield entry
    monkeypatch.setattr(post_dir.os, 'scandir', Observed)
    monkeypatch.setattr(post, '_read_regular', lambda *a, **kw: pytest.fail('status read content'))
    lines = post.drop_waits(runtime, at=0)
    assert len(visited) == min(post.DROP_MAX_FILES + extra, post.DROP_MAX_FILES + 1)
    assert lines == ([f'post drop held: drop/worker: {post.DROP_COUNT_REASON}'] if extra else [])
    assert all('ordinary-' not in line and 'private' not in line for line in lines)
