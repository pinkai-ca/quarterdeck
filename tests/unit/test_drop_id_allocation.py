"""Bounded short-id allocation, on La'an's terms: the allocator avoids the store's shorts, the shorts its round
has prepared, and the drop windows its round has listed, and lists no drop folder past its window. A queued short
beyond a window may be drawn again; Store.get then refuses that short as ambiguous, never misdelivers. Its draws
are bounded: a kind with no free short is refused with one fixed reason, never a spin.

These gates build drop floods on real files.
"""

import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pytest

from quarterdeck import group_post, post, post_dir, ship as shipmod
from quarterdeck.ship import ShipError
from .test_drop_count_bound import file

FLOOD = 8 * post.DROP_MAX_FILES + 37  # far over one window
FREE = 'f' + '400'  # a suffix no flood here holds


def forum(tmp_path) -> shipmod.Ship:
    template = Path(__file__).parents[2] / 'ships/forum/ship.toml'
    (tmp_path / 'ship.toml').write_text(template.read_text())
    return shipmod.load(tmp_path)


def flood(runtime: Path, seat: str, count: int, *, kind: str = 'order') -> list[tuple[post.Message, Path]]:
    """``count`` queued ordinary drops from ``seat``, each with its own short, as real files."""
    at = datetime(2032, 1, 1, tzinfo=timezone.utc)
    queued = []
    for number in range(count):
        identity = '-'.join((kind[:3], '20320101', '0000', seat, f'{number:04x}'))
        message = post.Message(identity, kind, seat, 'ship', at, f'ordinary queued {kind} {number}')
        queued.append((message, post.write_drop(runtime, message)))
    return queued


def observe_listings(monkeypatch, runtime: Path) -> list[tuple[str, int | None]]:
    """Every listing of a seat's drop folder, by folder and limit."""
    drops = runtime / 'post' / 'drop'
    seen = []
    listing = post_dir.list_children

    def observed(directory, **kwargs):
        if Path(directory).parent == drops:
            seen.append((Path(directory).name, kwargs.get('limit')))
        return listing(directory, **kwargs)
    monkeypatch.setattr(post_dir, 'list_children', observed)
    return seen


@pytest.mark.parametrize('caller', ['full', 'slice'])
def test_group_filing_has_no_extra_drop_directory_inventory(tmp_path, caller, monkeypatch):
    ship = forum(tmp_path)
    store = post.Store(ship.runtime)
    group_post.stage(ship, store, 'reviewers', 'lead', 'ordinary group task')
    listings = []
    listing = post_dir.list_children
    def observed(directory, **kwargs):
        if Path(directory) == post.drop_dir(ship.runtime, 'lead'):
            listings.append(kwargs.get('limit'))
        return listing(directory, **kwargs)
    monkeypatch.setattr(post_dir, 'list_children', observed)
    file(store, caller, ship)
    assert listings == [post.DROP_MAX_FILES], 'group filing re-enumerated the drop outside its shared window'


@pytest.mark.parametrize('flooded', ['lead', 'reviewer-2'])
@pytest.mark.parametrize('caller', ['full', 'slice'])
def test_a_group_post_and_its_copies_finish_over_a_flood_without_listing_past_the_window(
        tmp_path, caller, flooded, monkeypatch):
    ship = forum(tmp_path)
    store = post.Store(ship.runtime)
    queued = flood(ship.runtime, flooded, FLOOD)
    seen = observe_listings(monkeypatch, ship.runtime)
    root, count = group_post.stage(ship, store, 'reviewers', 'lead', 'ordinary group task')  # the seat's post
    assert seen and all(limit == post.DROP_MAX_FILES for _, limit in seen), seen
    assert max(Counter(seen).values()) == 1, 'the post listed a folder twice'
    intent = post.drop_dir(ship.runtime, 'lead') / f'{root.id}.group.json'
    rounds = 0
    while intent.exists():  # the intent may lie beyond the lead's window: rounds until it is filed
        rounds += 1
        assert rounds <= FLOOD // post.DROP_MAX_FILES + 2, 'the intent was never filed'
        seen.clear()
        file(store, caller, ship)
        assert seen and all(limit == post.DROP_MAX_FILES for _, limit in seen), seen
        assert max(Counter(name for name, _ in seen).values()) == 1, 'a round listed a folder twice'
    filed = store.get(root.id)
    copies = [m for m in store.all() if m.re == filed.id and m.sender == 'lead']
    assert sorted(m.to for m in copies) == sorted(ship.groups['reviewers'].members) and count == len(copies)
    assert len({m.short for m in (filed, *copies)}) == len(copies) + 1
    assert sum(path.exists() for _, path in queued) <= FLOOD


@pytest.mark.parametrize('flooded', ['lead', 'reviewer-2'])
def test_a_direct_group_submit_lists_each_drop_folder_once_within_its_window(tmp_path, flooded, monkeypatch):
    ship = forum(tmp_path)
    store = post.Store(ship.runtime)
    flood(ship.runtime, flooded, FLOOD)
    seen = observe_listings(monkeypatch, ship.runtime)
    root, copies = group_post.submit(ship, store, 'reviewers', 'lead', 'ordinary group task')
    assert seen == [(flooded, post.DROP_MAX_FILES)]
    assert len({m.short for m in (root, *copies)}) == len(copies) + 1


def test_a_short_beyond_the_window_drawn_again_is_filed_twice_and_refused_by_short(tmp_path, monkeypatch):
    ship = forum(tmp_path)
    runtime = ship.runtime
    store = post.Store(runtime)
    queued = flood(runtime, 'reviewer-2', 300)
    window = {name for name, _ in post_dir.list_children(post.drop_dir(runtime, 'reviewer-2'),
                                                         limit=post.DROP_MAX_FILES)}
    beyond, beyond_path = next((m, path) for m, path in queued if path.name not in window)
    draw = post.secrets.token_hex
    suffixes = iter([beyond.id.rsplit('-', 1)[1]])
    monkeypatch.setattr(post.secrets, 'token_hex',
                        lambda size: next(suffixes, None) or draw(size) if size == 2 else draw(size))
    drawn = store.new('order', 'lead', 'reviewer-1', 'ordinary new task')
    assert drawn.short == beyond.short and drawn.id != beyond.id, 'beyond the window, the short is not seen'
    assert beyond_path.exists()
    for _ in range(4):
        file(store, 'full', ship)
    assert not any(path.exists() for _, path in queued), 'every queued drop is filed'
    assert store.get(beyond.id) == beyond and store.get(drawn.id) == drawn
    with pytest.raises(ShipError, match=f"'{beyond.short}' names 2 messages; use the full id"):
        store.get(beyond.short)
    assert post.drop_alarms(runtime) == []


def test_a_short_inside_the_window_is_never_drawn_again(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    store = post.Store(runtime)
    queued = flood(runtime, 'waiting', 300)
    window = {name for name, _ in post_dir.list_children(post.drop_dir(runtime, 'waiting'),
                                                         limit=post.DROP_MAX_FILES)}
    inside = next(m for m, path in queued if path.name in window)
    suffixes = iter((inside.id.rsplit('-', 1)[1], FREE))
    monkeypatch.setattr(post.secrets, 'token_hex', lambda size: next(suffixes))
    assert store.prepare('order', 'lead', 'member', 'ordinary new task').short == 'ord-' + FREE


def test_the_senders_own_queued_id_is_never_drawn_again_beyond_its_window(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    store = post.Store(runtime)
    queued = flood(runtime, 'waiting', 300)
    window = {name for name, _ in post_dir.list_children(post.drop_dir(runtime, 'waiting'),
                                                         limit=post.DROP_MAX_FILES)}
    beyond = next(m for m, path in queued if path.name not in window)
    suffixes = iter((beyond.id.rsplit('-', 1)[1], FREE))
    monkeypatch.setattr(post.secrets, 'token_hex', lambda size: next(suffixes))
    monkeypatch.setattr(post, 'now', lambda: beyond.at)
    # The same sender in the same minute would make the same full id; one name lookup, no listing, avoids it.
    assert store.prepare('order', 'waiting', 'ship', 'ordinary new task').short == 'ord-' + FREE


def test_a_taken_set_covering_a_kinds_whole_space_is_refused_in_bounded_time(tmp_path, monkeypatch):
    store = post.Store(tmp_path / 'runtime')
    taken = {f'ord-{n:04x}' for n in range(16 ** 4)}
    draws = []
    draw = post.secrets.token_hex
    monkeypatch.setattr(post.secrets, 'token_hex', lambda size: (size == 2 and draws.append(size)) or draw(size))
    started = time.monotonic()
    with pytest.raises(post.IdSpaceExhausted) as refused:
        store.prepare('order', 'lead', 'member', 'ordinary new task', taken=taken)
    assert str(refused.value) == post.ID_SPACE_REASON
    assert len(draws) == post.ID_DRAWS and time.monotonic() - started < 5
    assert len(taken) == 16 ** 4, 'a refusal adds nothing to the round'
    report = store.prepare('report', 'lead', 'member', 'another kind still has room', taken=taken)
    assert report.short.startswith('rep-')


@pytest.mark.parametrize('caller', ['full', 'slice'])
def test_a_group_intent_with_no_free_short_is_held_with_the_fixed_reason(tmp_path, caller, monkeypatch):
    ship = forum(tmp_path)
    store = post.Store(ship.runtime)
    root, _ = group_post.stage(ship, store, 'reviewers', 'lead', 'ordinary group task')
    draws = []
    taken_suffix = root.id.rsplit('-', 1)[1]  # every draw is the root's own short, which the round holds
    draw = post.secrets.token_hex
    monkeypatch.setattr(post.secrets, 'token_hex',
                        lambda size: draws.append(size) or taken_suffix if size == 2 else draw(size))
    started = time.monotonic()
    file(store, caller, ship)
    assert len(draws) == post.ID_DRAWS and time.monotonic() - started < 30
    assert f'post drop held: drop/lead: {post.ID_SPACE_REASON}' in post.drop_alarms(ship.runtime)
    assert (post.drop_dir(ship.runtime, 'lead') / f'{root.id}.group.json').exists(), 'the intent waits, unchanged'
    assert not [m for m in store.all() if m.re == root.id]
    monkeypatch.undo()
    file(store, caller, ship)  # once a short is free, the next round files it and the hold clears
    assert len([m for m in store.all() if m.re == root.id]) == len(ship.groups['reviewers'].members)
    assert post.drop_alarms(ship.runtime) == []


def test_the_stored_shorts_are_built_once_for_each_load_not_once_for_each_id(tmp_path, monkeypatch):
    store = post.Store(tmp_path / 'runtime')
    first = store.new('report', 'lead', 'ship', 'ordinary report')
    time.sleep(post.POST_TIMESTAMP_TICK_NS / 1e9 + 0.2)  # until then each read lists the store afresh, by design
    store.all()
    built = []
    shorts = post.Message.short
    monkeypatch.setattr(post.Message, 'short', property(lambda self: built.append(self.id) or shorts.fget(self)))
    for _ in range(5):
        store.prepare('order', 'lead', 'member', 'ordinary new task')
    assert built.count(first.id) <= 1, 'each id passed over the whole store again'
    second = store.new('report', 'lead', 'ship', 'another ordinary report')
    assert second.short in store._shorts(), 'a new load of the store brings its shorts'
