"""A jailed seat's view of the post: what the seat is party to, copied, never linked; and the shorts others hold."""

from __future__ import annotations

from pathlib import Path

import pytest

from quarterdeck import post, post_view


def runtime_of(tmp_path: Path) -> Path:
    ship = tmp_path / 'ships' / 'alpha'
    ship.mkdir(parents=True)
    (ship / 'ship.toml').write_text('')
    return ship / 'runtime'


def test_the_view_holds_what_the_seat_sent_or_received_as_copies_and_nothing_else(tmp_path):
    runtime = runtime_of(tmp_path)
    store = post.Store(runtime)
    received = store.new('order', 'captain', 'worker-1', 'to the seat')
    sent = store.new('report', 'worker-1', 'captain', 'from the seat', re=received.id)
    other = store.new('order', 'captain', 'desk', 'between others')
    post.render_inbox(runtime, store, 'worker-1')
    post.render_inbox(runtime, store, 'desk')
    view = post_view.refresh(runtime, 'worker-1')
    assert view == runtime / 'views' / 'worker-1' / 'post'
    names = sorted(p.name for p in (view / 'store').iterdir())
    assert names == sorted([f'{received.id}.json', f'{sent.id}.json'])
    for name in names:
        copy = view / 'store' / name
        assert copy.read_text() == (store.dir / name).read_text() and copy.stat().st_nlink == 1, 'a copy, never a link'
    assert (view / 'inbox' / 'worker-1.md').read_text() == (runtime / 'post' / 'inbox' / 'worker-1.md').read_text()
    assert not (view / 'inbox' / 'desk.md').exists() and not (view / 'drop-holds').exists()
    assert (view / 'drop' / 'worker-1').is_dir(), 'the mount point for the seat\'s real drop'
    assert post_view.taken_shorts(view) >= {received.short, sent.short, other.short}


def test_a_seat_of_the_same_name_on_another_ship_is_not_this_seat():
    message = post.Message('ord-20300101-0000-captain-abcd', 'order', 'worker-1@beta', 'captain', None, 'x')
    assert not post_view.party(message, 'worker-1', 'alpha')
    assert post_view.party(post.Message('ord-20300101-0000-captain-abcd', 'order', 'captain', 'worker-1@alpha', None, 'x'),
                           'worker-1', 'alpha')


def test_an_id_chosen_inside_a_view_never_takes_a_short_another_seat_holds(tmp_path):
    runtime = tmp_path / 'view-runtime'
    (runtime / 'post' / 'store').mkdir(parents=True)
    width = 2 * post.SHORT_ID_BYTES  # the drawn suffix at the width allocation asks for, whatever that width is
    held = [f'{number:0{width}x}' for number in range(post.ID_DRAWS - 1)]
    free, spare = f'{16 ** width - 2:0{width}x}', f'{16 ** width - 3:0{width}x}'
    asked: list[int] = []

    def drawing(values):
        draws = iter(values)

        def token_hex(size):
            asked.append(size)
            return next(draws)
        return token_hex

    # Draws are bounded (at most post.ID_DRAWS): every one but the last falls on a short the view holds.
    (runtime / 'post' / post_view.SHORTS).write_text(''.join(f'ord-{short}\n' for short in held))
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(post.secrets, 'token_hex', drawing([*held, free]))
        message = post.Store(runtime).prepare('order', 'worker-1', 'captain', 'one short left')
    assert message.short == f'ord-{free}'
    assert asked == [post.SHORT_ID_BYTES] * post.ID_DRAWS
    # With every draw on a short the view holds, the draws stop at post.ID_DRAWS and the post is refused with the
    # fixed reason, never spun: the spare short beyond the bound is free, and is never drawn.
    (runtime / 'post' / post_view.SHORTS).write_text(''.join(f'ord-{short}\n' for short in [*held, free]))
    asked.clear()
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(post.secrets, 'token_hex', drawing([*held, free, spare]))
        with pytest.raises(post.IdSpaceExhausted, match=post.ID_SPACE_REASON):
            post.Store(runtime).prepare('order', 'worker-1', 'captain', 'none left')
    assert asked == [post.SHORT_ID_BYTES] * post.ID_DRAWS


def test_the_view_drops_an_inbox_the_post_no_longer_has(tmp_path):
    runtime = runtime_of(tmp_path)
    store = post.Store(runtime)
    store.new('order', 'captain', 'worker-1', 'to the seat')
    post.render_inbox(runtime, store, 'worker-1')
    view = post_view.refresh(runtime, 'worker-1')
    (runtime / 'post' / 'inbox' / 'worker-1.md').unlink()
    post_view.refresh(runtime, 'worker-1')
    assert not (view / 'inbox' / 'worker-1.md').exists()


def opened(monkeypatch) -> list[str]:
    """The store messages the view's index opens, by name."""
    seen = []
    original = post_view._parties_of

    def counting(runtime, path):
        seen.append(path.stem)
        return original(runtime, path)
    monkeypatch.setattr(post_view, '_parties_of', counting)
    return seen


def test_a_refresh_opens_only_the_messages_added_since_the_last(tmp_path, monkeypatch):
    runtime = runtime_of(tmp_path)
    store = post.Store(runtime)
    first = [store.new('order', 'captain', 'worker-1' if n % 2 else 'desk', f'm{n}') for n in range(6)]
    seen = opened(monkeypatch)
    post_view.refresh(runtime, 'worker-1')
    assert sorted(seen) == sorted(m.id for m in first), 'the first refresh reads each message once'
    seen.clear()
    post_view.refresh(runtime, 'worker-1')
    post_view.refresh(runtime, 'desk')
    assert seen == [], 'nothing new: nothing opened, for this seat or another'
    added = store.new('order', 'captain', 'worker-1', 'added')
    view = post_view.refresh(runtime, 'worker-1')
    assert seen == [added.id]
    assert (view / 'store' / f'{added.id}.json').is_file()
    assert post_view.taken_shorts(view) == {m.short for m in first + [added]}


def test_the_index_outlives_the_process_and_a_half_written_line_is_read_again(tmp_path, monkeypatch):
    runtime = runtime_of(tmp_path)
    store = post.Store(runtime)
    one = store.new('order', 'captain', 'worker-1', 'one')
    post_view.refresh(runtime, 'worker-1')
    post_view._INDEXES.clear()  # a new process: only the file is left
    seen = opened(monkeypatch)
    post_view.refresh(runtime, 'worker-1')
    assert seen == [], 'the index on disk is read, not the store'
    index = runtime / 'views' / post_view.INDEX
    two = store.new('order', 'captain', 'worker-1', 'two')
    with index.open('a') as handle:
        handle.write(f'["{two.id}", "captain", "desk"')  # an append cut short, and wrong as far as it goes
    post_view._INDEXES.clear()
    view = post_view.refresh(runtime, 'worker-1')
    assert seen == [two.id], 'a line without its end is never trusted: the message is read again'
    assert {p.name for p in (view / 'store').iterdir()} == {f'{one.id}.json', f'{two.id}.json'}


def test_the_index_lies_outside_every_view(tmp_path):
    runtime = runtime_of(tmp_path)
    post.Store(runtime).new('order', 'captain', 'worker-1', 'one')
    view = post_view.refresh(runtime, 'worker-1')
    index = runtime / 'views' / post_view.INDEX
    assert index.is_file() and view not in index.parents
    assert post_view.seats(runtime) == ['worker-1'], 'the index is no seat'


def views_service(runtime: Path, seats: dict, kept_up: set[str] = frozenset()):
    from types import SimpleNamespace
    from quarterdeck import service as servicemod

    logged = []
    ship = SimpleNamespace(seats=seats, jailed=lambda spec: spec.jailed)

    def seat(name):
        argv = ['bwrap', '--', 'program'] if name in kept_up else ['program']
        return SimpleNamespace(launched=lambda: SimpleNamespace(argv=argv), is_up=name in kept_up)
    stub = SimpleNamespace(runtime=runtime, ship=ship, seat=seat, log=lambda event, **fields: logged.append((event, fields)))
    stub._view_kept = lambda name: servicemod.Service._view_kept(stub, name)
    stub._viewed_seats = lambda: servicemod.Service._viewed_seats(stub)
    stub._refresh_views = lambda names, snapshot=None: servicemod.Service._refresh_views(stub, names, snapshot)
    return stub, logged, lambda: servicemod.Service.views(stub)


def test_the_service_removes_a_view_no_seat_should_have(tmp_path):
    from types import SimpleNamespace

    runtime = runtime_of(tmp_path)
    store = post.Store(runtime)
    for name in ('worker-1', 'worker-2', 'worker-3', 'gone', 'check-wake-synthetic', 'check-live-synthetic'):
        store.new('order', 'captain', name, f'to {name}')
        post_view.refresh(runtime, name)
    (runtime / 'seats' / 'check-live-synthetic').mkdir(parents=True)  # a temporary seat not yet torn down
    seats = {'worker-1': SimpleNamespace(jailed=True), 'worker-2': SimpleNamespace(jailed=False),
             'worker-3': SimpleNamespace(jailed=False)}
    stub, logged, views = views_service(runtime, seats, kept_up={'worker-3'})
    views()
    # worker-1 is jailed; worker-3 is no longer, but still runs in its jail until it is relaunched; the live check
    # seat's folder stands. worker-2 is no longer jailed and not running in one; 'gone' is declared no more, and the
    # torn-down check seat's folder is gone.
    assert post_view.seats(runtime) == ['check-live-synthetic', 'worker-1', 'worker-3'], logged
    assert sorted(f['seat'] for event, f in logged if event == 'view-removed') == ['check-wake-synthetic', 'gone',
                                                                                 'worker-2']
    late = store.new('order', 'captain', 'worker-1', 'late')
    views()
    assert (post_view.folder(runtime, 'worker-1') / 'store' / f'{late.id}.json').is_file()


def test_the_view_shows_only_the_seats_own_attempts_its_standing_offer_names(tmp_path):
    import json

    from quarterdeck import durable, watch_broker

    runtime = runtime_of(tmp_path)
    store = post.Store(runtime)
    attempts = post.Attempts(runtime)
    offered = store.new('order', 'captain', 'desk', 'offered')
    earlier = attempts.open(offered, 'desk', None, None, None)  # given before: qd inbox marks a second delivery
    attempts.close(earlier, 'blind', 'synthetic')
    now = attempts.open(offered, 'desk', None, None, None)
    other = store.new('order', 'captain', 'worker-1', 'not the desk\'s')
    attempts.open(other, 'worker-1', None, None, None)
    unoffered = store.new('order', 'captain', 'desk', 'not offered')
    attempts.open(unoffered, 'desk', None, None, None)
    view = post_view.refresh(runtime, 'desk')
    assert not (view / 'attempts').exists(), 'no offer, no attempts'
    durable.replace_file(watch_broker.offer_path(runtime, 'desk'), json.dumps(dict(
        format=1, seat='desk', generation='a' * 32, messages=[offered.id], attempts=[now.path.name])) + '\n')
    post_view.refresh(runtime, 'desk')
    assert sorted(p.name for p in (view / 'attempts').iterdir()) == sorted([earlier.path.name, now.path.name])
    assert (view / 'watch-offers' / 'desk.json').is_file()
    watch_broker.offer_path(runtime, 'desk').unlink()
    post_view.refresh(runtime, 'desk')
    assert not (view / 'attempts').exists() and not (view / 'watch-offers' / 'desk.json').exists()
