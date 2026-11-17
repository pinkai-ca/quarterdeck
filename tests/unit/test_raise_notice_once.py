"""The desk's notice of a raise: one per ship and yes, never 'unreadable' while the post store is held.

A rerun that resumes on an earlier yes posts no second order: the yes is its answer record, written once, named by
its path and the sha256 of its bytes. While the post store is held, a readable notice is not shown (the hold names
itself on every status line, and no notice can be judged open or reported then); a notice file that cannot be read
is still named. A throwaway project and office under tmp_path.
"""
import json
from types import SimpleNamespace

import pytest

from quarterdeck import post, raise_notice, raise_ship, watch
from tests.unit.test_raise_ship import args, project  # noqa: F401  (the throwaway office, by name)


def orders(office):
    return [m for m in post.Store(office.runtime).all() if m.subject == raise_notice.SUBJECT]


def test_a_resumed_run_on_the_same_yes_posts_no_second_order(project, monkeypatch):
    root, office, calls = project
    raise_ship.run(args(), interactive=True, read=lambda _: 'yes', write=lambda _: None)
    first, = orders(office)
    monkeypatch.setenv('QD_SHIP', str(office.folder))
    monkeypatch.setenv('QD_SEAT', 'desk')
    lines = raise_ship.run(args(), interactive=False)  # the desk resumes on the person's earlier yes
    assert 'resuming without edits' in lines[0]
    assert [m.id for m in orders(office)] == [first.id], 'one notice for one yes'
    assert any(first.short in line or first.id in line for line in lines)


def test_a_new_yes_posts_its_own_order(project, tmp_path):
    root, office, calls = project
    draft = SimpleNamespace(name='mission', folder=root / 'ships' / 'mission')  # the order is checked against these
    one, two = tmp_path / 'one.answer.json', tmp_path / 'two.answer.json'
    one.write_text(json.dumps({'format': 1, 'answer': 'yes', 'terminal': '/dev/pts/synthetic'}))
    two.write_text(json.dumps({'format': 1, 'answer': 'yes', 'terminal': '/dev/pts/another'}))
    first = raise_ship.tell_desk(office, draft, one)
    assert raise_ship.tell_desk(office, draft, one) == first, 'the same yes again: the same order'
    second = raise_ship.tell_desk(office, draft, two)
    assert second != first and len(orders(office)) == 2


def test_a_held_store_shows_no_unreadable_for_a_readable_notice_and_names_a_broken_one(project):
    root, office, calls = project
    raise_ship.run(args(), interactive=True, read=lambda _: 'yes', write=lambda _: None)
    store = post.Store(office.runtime)
    assert len(raise_notice.unseen(office.runtime, store)) == 1, 'open while the desk has not reported'
    held = watch._HeldStore(store)
    assert raise_notice.unseen(office.runtime, held) == [], 'held: not shown, never unreadable'
    broken = raise_notice.folder(office.runtime) / 'mission' / 'notice-broken.json'
    broken.write_text('{ not json')
    lines = raise_notice.unseen(office.runtime, held)
    assert lines == [f'raise notice unreadable: {broken}; inspect it before clearing']


def forged(office, answer, message_id):
    """A notice written by hand: the right yes, by path and sha256, naming ``message_id``."""
    path = raise_notice.folder(office.runtime) / 'mission' / 'notice-forged.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'format': 1, 'ship': 'mission', 'message': message_id, 'terminal': '/dev/pts/synthetic',
                                **raise_notice.yes_of(answer)}))


def a_yes(tmp_path):
    answer = tmp_path / 'one.answer.json'
    answer.write_text(json.dumps({'format': 1, 'answer': 'yes', 'terminal': '/dev/pts/synthetic'}))
    return answer


def test_a_notice_naming_no_order_buys_no_silence(project, tmp_path):
    root, office, calls = project
    draft = SimpleNamespace(name='mission', folder=root / 'ships' / 'mission')
    answer = a_yes(tmp_path)
    forged(office, answer, '-'.join(('ord', '20320102', '1200', 'ship', '000000')))  # an id no store holds
    told = raise_notice.told(office.runtime, 'mission', answer, store=post.Store(office.runtime), desk='desk',
                             ship_folder=draft.folder)
    assert told is None
    posted = raise_ship.tell_desk(office, draft, answer)
    assert [m.id for m in orders(office)] == [posted], 'an order is posted'


@pytest.mark.parametrize('other', ['subject', 'sender'])
def test_a_notice_naming_a_closed_order_of_another_kind_buys_no_silence(project, tmp_path, other):
    root, office, calls = project
    draft = SimpleNamespace(name='mission', folder=root / 'ships' / 'mission')
    answer = a_yes(tmp_path)
    store = post.Store(office.runtime)
    body = f'Ship mission was raised at {draft.folder} and its crew is starting (answer record {answer}).'
    if other == 'subject':
        closed = store.new('order', post.SHIP, 'desk', body, subject='Another matter')
    else:
        closed = store.new('order', 'desk', 'desk', body, subject=raise_notice.SUBJECT)
    store.new('report', 'desk', closed.sender, 'Done.', re=closed.id)
    assert not post.Store(office.runtime).is_open(post.Store(office.runtime).get(closed.id))
    forged(office, answer, closed.id)
    posted = raise_ship.tell_desk(office, draft, answer)
    assert posted != closed.id and posted in [m.id for m in orders(office)], 'an order is posted'
    store = post.Store(office.runtime)
    lines = raise_notice.unseen(office.runtime, store)
    assert any(store.get(posted).short in line for line in lines), 'the status line shows the new order'
