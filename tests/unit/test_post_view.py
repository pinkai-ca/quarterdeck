"""A jailed seat's view of the post: what the seat is party to, copied, never linked; and the shorts others hold."""

from __future__ import annotations

from pathlib import Path

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
    view = post_view.refresh(runtime, 'worker-1', store)
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
    every = [f'ord-{number:04x}' for number in range(16 ** 4) if number != 0xabcd]
    (runtime / 'post' / post_view.SHORTS).write_text(''.join(f'{short}\n' for short in every))
    message = post.Store(runtime).prepare('order', 'worker-1', 'captain', 'one short left')
    assert message.short == 'ord-abcd'


def test_the_view_drops_an_inbox_the_post_no_longer_has(tmp_path):
    runtime = runtime_of(tmp_path)
    store = post.Store(runtime)
    store.new('order', 'captain', 'worker-1', 'to the seat')
    post.render_inbox(runtime, store, 'worker-1')
    view = post_view.refresh(runtime, 'worker-1', store)
    (runtime / 'post' / 'inbox' / 'worker-1.md').unlink()
    post_view.refresh(runtime, 'worker-1', store)
    assert not (view / 'inbox' / 'worker-1.md').exists()
