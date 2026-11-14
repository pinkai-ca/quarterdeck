"""Ordinary per-seat queue boundaries and bounded metadata readings."""

from pathlib import Path
import os
from types import SimpleNamespace

import pytest

from quarterdeck import check, group_post, post, post_dir, ship as shipmod


def queue(store, count, sender='worker'):
    messages, paths = [], []
    for n in range(count):
        message = store.prepare('report', sender, 'ship', f'ordinary queued report {n}')
        paths.append(post.write_drop(store.dir.parent.parent, message))
        messages.append(message)
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
@pytest.mark.parametrize('count', [128, 129, 300])
def test_busy_ordinary_queue_drains_in_bounded_rounds(tmp_path, caller, count):
    store = post.Store(tmp_path / 'runtime')
    messages, paths = queue(store, count)
    other, other_paths = queue(store, 1, sender='z-last')
    rounds = (count + post.DROP_MAX_FILES - 1) // post.DROP_MAX_FILES
    for number in range(rounds):
        file(store, caller)
        assert sum(not path.exists() for path in paths) == min((number + 1) * post.DROP_MAX_FILES, count)
        assert post.drop_alarms(tmp_path / 'runtime') == []
        if number == 0:
            assert other_paths[0].exists() == (caller == 'host')
    assert all(store.get(message.id) == message for message in messages)
    assert not any(path.exists() for path in paths)


@pytest.mark.parametrize('caller', ['full', 'slice'])
def test_groups_and_ordinary_drops_share_the_same_window(tmp_path, caller, monkeypatch):
    template = Path(__file__).parents[2] / 'ships/forum/ship.toml'
    (tmp_path / 'ship.toml').write_text(template.read_text())
    ship = shipmod.load(tmp_path)
    store = post.Store(ship.runtime)
    root, _ = group_post.stage(ship, store, 'reviewers', 'lead', 'ordinary group task')
    messages, paths = queue(store, 298, sender='lead')
    listings = []
    listing = post_dir.list_children
    def observed(directory, **kwargs):
        if Path(directory) == post.drop_dir(ship.runtime, 'lead') and kwargs.get('limit') is not None:
            listings.append(kwargs['limit'])
        return listing(directory, **kwargs)
    monkeypatch.setattr(post_dir, 'list_children', observed)
    for _ in range(4):
        before = len(listings)
        file(store, caller, ship)
        assert len(listings) == before + 1
    assert listings == [128] * 4
    assert store.get(root.id) == root
    assert all(store.get(message.id) == message for message in messages)
    assert not any(path.exists() for path in paths)
    assert not list(post.drop_dir(ship.runtime, 'lead').iterdir())


@pytest.mark.parametrize('caller', ['full', 'slice'])
@pytest.mark.parametrize('unfileable', ['skipped', 'bad'])
def test_full_unfileable_window_is_named_immediately_and_following_seat_files(tmp_path, caller, unfileable):
    runtime = tmp_path / 'runtime'
    folder = post.drop_dir(runtime, 'first')
    folder.mkdir(parents=True)
    # Skipped group intent names without a group caller exercise a no-progress window.
    for number in range(post.DROP_MAX_FILES):
        suffix = '.group.json' if unfileable == 'skipped' else '.json'
        (folder / f'ordinary-{number}{suffix}').write_text('ordinary non-message')
    store = post.Store(runtime)
    messages, paths = queue(store, 1, sender='z-last')
    file(store, caller)
    reason = f'post drop held: drop/first: {post.DROP_STALLED_REASON}'
    assert post.DROP_STALLED_REASON in post.DROP_HOLD_REASONS
    assert reason in post.drop_alarms(runtime)
    assert store.get(messages[0].id) == messages[0] and not paths[0].exists()
    assert len(list(folder.iterdir())) == post.DROP_MAX_FILES
    # A fresh independent status reading sees the persisted reason, not message text.
    from quarterdeck import watch
    from .test_check_lifetime import synthetic_ship
    ship = synthetic_ship(tmp_path)
    assert reason in watch.facts(ship, SimpleNamespace(), walls=[], login=[], check_reading=([], [])).line()


@pytest.mark.parametrize('caller', ['full', 'slice'])
def test_sustained_budget_use_alerts_after_three_rounds_and_clears(tmp_path, caller, monkeypatch):
    runtime = tmp_path / 'runtime'
    store = post.Store(runtime)
    _, paths = queue(store, 600)
    folder = 'drop/worker'
    marker = runtime / 'post/drop-holds' / (post._hold_name(folder, post.DROP_COUNT_REASON) + '.json')
    writes = []
    replace_file = post.durable.replace_file
    def observed(path, *args, **kwargs):
        if path == marker:
            writes.append(path)
        return replace_file(path, *args, **kwargs)
    monkeypatch.setattr(post.durable, 'replace_file', observed)
    alert = f'post drop waiting: {folder}: {post.DROP_COUNT_REASON}'
    for number in range(4):
        file(store, caller)
        assert (alert in post.drop_alarms(runtime)) == (number >= 2)
    assert len(writes) == 3  # saturation causes no repeated marker writes
    file(store, caller)  # final 88-name window clears telemetry and its alert
    assert not any(path.exists() for path in paths)
    assert not marker.exists() and post.drop_alarms(runtime) == []


@pytest.mark.parametrize('extra', [0, 1, 64])
def test_status_listing_stops_at_the_window_before_stat_without_message_reads(tmp_path, monkeypatch, extra):
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
                if self.watched:
                    class Entry:
                        def __init__(self, original):
                            self.original, self.name = original, original.name
                        def stat(self, **kwargs):
                            assert len(visited) == post.DROP_MAX_FILES
                            return self.original.stat(**kwargs)
                    yield Entry(entry)
                else:
                    yield entry
    monkeypatch.setattr(post_dir.os, 'scandir', Observed)
    monkeypatch.setattr(post, '_read_regular', lambda *a, **kw: pytest.fail('status read content'))
    lines = post.drop_waits(runtime, at=0)
    assert len(visited) == post.DROP_MAX_FILES
    assert lines == []
    assert not (runtime / 'post/drop-holds').exists(), 'status must not publish round counters'
    assert all('ordinary-' not in line and 'private' not in line for line in lines)


@pytest.mark.parametrize('caller', ['full', 'slice'])
def test_group_root_waits_for_an_intent_outside_this_rounds_window(tmp_path, caller, monkeypatch):
    template = Path(__file__).parents[2] / 'ships/forum/ship.toml'
    (tmp_path / 'ship.toml').write_text(template.read_text())
    ship = shipmod.load(tmp_path)
    store = post.Store(ship.runtime)
    root, _ = group_post.stage(ship, store, 'reviewers', 'lead', 'ordinary group task')
    _, paths = queue(store, 127, sender='lead')
    folder = post.drop_dir(ship.runtime, 'lead')
    listing = post_dir.list_children
    first = [True]
    def selected(directory, **kwargs):
        if Path(directory) == folder and kwargs.get('limit') == post.DROP_MAX_FILES and first[0]:
            first[0] = False
            entries = listing(directory)
            return sorted((name, mode) for name, mode in entries if not name.endswith('.group.json'))
        return listing(directory, **kwargs)
    monkeypatch.setattr(post_dir, 'list_children', selected)
    file(store, caller, ship)
    assert root.id not in {message.id for message in store.all()}
    assert (folder / (root.id + '.json')).exists()
    assert (folder / (root.id + '.group.json')).exists()
    assert not any(path.exists() for path in paths)
    file(store, caller, ship)
    assert store.get(root.id) == root
    assert not list(folder.iterdir())
