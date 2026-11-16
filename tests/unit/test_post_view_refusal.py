"""A message the seat's provider refused goes into the seat's jailed view of the post as its notice alone, never as
the message (law: context-is-cache). The service writes the view outside every jail, where it reads the seat's
refusal journal; inside the jail the journal is not visible, so the reading relies on the view and claims nothing
else. The ship's own store keeps the message whole.

Inside a jail the post is the view bound in the post's place and runtime/trusted is empty: here a runtime whose post
is a copy of the view, with no trusted folder, reads as the jail does.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

from quarterdeck import post, post_threads, post_view, refusal, trusted_files


def runtime_of(tmp_path: Path) -> Path:
    ship = tmp_path / 'ships' / 'alpha'
    ship.mkdir(parents=True)
    (ship / 'ship.toml').write_text('')
    return ship / 'runtime'


def quarantine(runtime: Path, seat: str, message: post.Message, *, told: bool) -> None:
    notices = [{'message': message.id, 'id': f'rep-20300101-0000-ship-{"1" * 6}', 'at': datetime.now().isoformat()}]
    refusal.save(runtime, seat, [{'stage': 'held', 'launch': {}, 'turn': None, 'code': None, 'reason': 'test',
                                  'quarantined': [{'message': message.id, 'sender': message.sender,
                                                   'kind': message.kind}],
                                  'notices': notices if told else []}])


def jailed(runtime: Path, seat: str, tmp_path: Path) -> Path:
    """A runtime as the seat's jail shows it: the view in the post's place, and no trusted folder."""
    inside = tmp_path / 'inside' / 'ships' / 'alpha'
    shutil.rmtree(inside.parent.parent, ignore_errors=True)
    inside.mkdir(parents=True)
    (inside / 'ship.toml').write_text('')
    shutil.copytree(post_view.folder(runtime, seat), inside / 'runtime' / 'post')
    return inside / 'runtime'


def a_world(tmp_path: Path):
    runtime = runtime_of(tmp_path)
    store = post.Store(runtime)
    kept = store.new('order', 'captain', 'worker-1', 'An order the seat may read.', subject='Readable work')
    refused = store.new('order', 'captain', 'worker-1', 'The invented body the provider refused.',
                        subject='Invented refused subject')
    return runtime, store, kept, refused


def test_a_refused_message_goes_into_the_view_as_its_notice_and_stays_whole_in_the_store(tmp_path):
    runtime, store, kept, refused = a_world(tmp_path)
    original = (store.dir / f'{refused.id}.json').read_text()
    view = post_view.refresh(runtime, 'worker-1')  # copied whole before the refusal
    assert 'invented body' in (view / 'store' / f'{refused.id}.json').read_text()
    quarantine(runtime, 'worker-1', refused, told=True)
    post_view.refresh(runtime, 'worker-1')
    text = (view / 'store' / f'{refused.id}.json').read_text()
    assert 'invented body' not in text and 'Invented refused subject' not in text
    shown = post.Message.from_json(text)
    assert (shown.id, shown.kind, shown.sender, shown.to) == (refused.id, 'order', 'captain', 'worker-1')
    assert shown.subject is None and shown.body == refusal.WITHHELD
    assert (store.dir / f'{refused.id}.json').read_text() == original, 'the ship keeps the message whole'
    assert (view / 'store' / f'{kept.id}.json').read_text() == (store.dir / f'{kept.id}.json').read_text()


def test_a_sender_not_told_is_said_so_in_the_view_as_outside(tmp_path):
    runtime, store, kept, refused = a_world(tmp_path)
    quarantine(runtime, 'worker-1', refused, told=False)
    view = post_view.refresh(runtime, 'worker-1')
    outside = post._shown(refused, *post.InboxContext(store).refused('worker-1'),
                          post.InboxContext(store).sender_seat)
    shown = post.Message.from_json((view / 'store' / f'{refused.id}.json').read_text())
    assert shown.body == outside.body and shown.subject is None and 'invented body' not in shown.body


def test_inside_the_jail_qd_inbox_and_qd_thread_show_only_the_notice(tmp_path):
    runtime, store, kept, refused = a_world(tmp_path)
    store.new('report', 'worker-1', 'captain', 'A reply in the refused thread.', re=refused.id)
    quarantine(runtime, 'worker-1', refused, told=True)
    post.render_inbox(runtime, store, 'worker-1')
    post_view.refresh(runtime, 'worker-1')
    inside = jailed(runtime, 'worker-1', tmp_path)
    assert not trusted_files.path(inside, 'worker-1', refusal.FILE).exists()
    jail_store = post.Store(inside)
    inbox = post.render_inbox_reading(post.inbox_reading(jail_store, 'worker-1'))
    assert 'invented body' not in inbox and 'Invented refused subject' not in inbox
    assert refusal.WITHHELD in inbox and 'An order the seat may read.' in inbox
    thread = post_threads.whole(jail_store, refused.id, 'worker-1')
    text = json.dumps([m.body for m in thread.messages]) + json.dumps([m.subject for m in thread.messages])
    assert 'invented body' not in text and 'Invented refused subject' not in text
    assert refused.id not in thread.readable, 'a notice is never counted as the message read'
    outside = post_threads.whole(store, refused.id, 'worker-1')
    assert [(m.id, m.subject, m.body) for m in thread.messages] == [(m.id, m.subject, m.body) for m in outside.messages]
    assert thread.readable == outside.readable


def test_an_unreadable_journal_withholds_every_body_in_the_view_until_it_reads_again(tmp_path):
    runtime, store, kept, refused = a_world(tmp_path)
    quarantine(runtime, 'worker-1', refused, told=True)
    view = post_view.refresh(runtime, 'worker-1')
    journal = trusted_files.path(runtime, 'worker-1', refusal.FILE)
    good = journal.read_text()
    journal.write_text('{not json')
    later = store.new('order', 'captain', 'worker-1', 'An order filed while the journal is unreadable.')
    post_view.refresh(runtime, 'worker-1')
    for message in (kept, refused, later):
        shown = post.Message.from_json((view / 'store' / f'{message.id}.json').read_text())
        assert shown.subject is None and shown.body == post.UNREADABLE, message.id
    inside = jailed(runtime, 'worker-1', tmp_path)
    inbox = post.render_inbox_reading(post.inbox_reading(post.Store(inside), 'worker-1'))
    assert 'may read' not in inbox and 'invented body' not in inbox and 'while the journal' not in inbox
    journal.write_text(good)
    post_view.refresh(runtime, 'worker-1')
    for message in (kept, later):
        assert (view / 'store' / f'{message.id}.json').read_text() == (store.dir / f'{message.id}.json').read_text()
    assert post.Message.from_json((view / 'store' / f'{refused.id}.json').read_text()).body == refusal.WITHHELD


def test_a_view_that_cannot_say_what_was_refused_withholds_every_body_inside(tmp_path):
    """A jailed reading never takes the journal's absence for 'nothing refused': a view without its account of the
    refusals (one written by an earlier service) withholds every body."""
    runtime, store, kept, refused = a_world(tmp_path)
    post_view.refresh(runtime, 'worker-1')
    inside = jailed(runtime, 'worker-1', tmp_path)
    (inside / 'post' / post_view.REFUSED).unlink()
    inbox = post.render_inbox_reading(post.inbox_reading(post.Store(inside), 'worker-1'))
    assert 'may read' not in inbox and 'invented body' not in inbox and post.UNREADABLE in inbox
