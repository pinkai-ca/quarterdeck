"""A change to a seat's standing orders wakes no seat: it rides at the head of the seat's next message (brief_change).

The door types the head with the message as one input, so its box check and the record's confirmation take the whole
text; every matcher reads the message past the head. Synthetic briefs and messages only."""
from __future__ import annotations

import json
import subprocess
from datetime import timedelta

import pytest

from quarterdeck import brief_change, cli, input_receipt, pending, post, thread_context
from quarterdeck import ship as shipmod
from quarterdeck.harness.types import Input
from .test_pending_delivery import input_row, setup  # noqa: F401
from .test_saved_pool_selection import migrated  # noqa: F401

OLD = "<!-- laws -->\nLaw one.\n\n<!-- ship's orders -->\nOrder one.\nOrder two.\n\n<!-- your seat -->\nSeat line.\n"
NEW = OLD.replace('Order two.', 'Order two, amended.')
NEWER = NEW.replace('Seat line.', 'Seat line, moved.')


def publish(runtime, name, text):
    path = runtime / 'brief' / f'{name}.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def taken(svc, seat, record, live):
    """The program takes the typed text: its box empties and its record shows the input, as typed."""
    proof = pending.active(seat)[-1]
    live['box'] = ''
    input_row(record, proof.data['payload'].strip(), post.now())
    svc.reconcile()
    return proof.data['payload']


def test_the_head_carries_only_the_changed_lines_between_its_two_lines():
    head = brief_change.compose('worker', OLD, NEW)
    lines = head.splitlines()
    assert lines[0].startswith(input_receipt.BRIEF_HEAD) and 'qd brief worker prints the whole' in lines[0]
    assert lines[-1] == input_receipt.BRIEF_HEAD_END
    assert '-Order two.' in lines and '+Order two, amended.' in lines
    assert 'Order one.' not in head and 'Law one.' not in head, 'the changed lines only'


def test_the_next_order_carries_the_change_once_and_is_confirmed_through_its_head(setup):  # noqa: F811
    svc, seat, live, record = setup
    publish(svc.runtime, seat.name, OLD)
    brief_change.seed(svc.runtime, seat.name, OLD)
    publish(svc.runtime, seat.name, NEW)  # an edit of the orders: nothing is posted, nothing is typed
    assert svc.store.all() == [] and live['pasted'] == 0
    first = svc.store.new('order', 'desk', seat.name, 'An invented first order.')
    svc.deliver(seat, [first])
    typed = live['box']
    assert typed.startswith(input_receipt.BRIEF_HEAD) and '+Order two, amended.' in typed
    assert typed.endswith(post.typed([first]).rstrip('\n')), 'the message itself follows whole and unchanged'
    assert svc.attempts.latest()[first.id] == 'pending', 'the box check took the whole text'
    payload = taken(svc, seat, record, live)
    assert svc.attempts.latest()[first.id] == 'delivered', "the record's confirmation took the whole text"
    assert brief_change.told(svc.runtime, seat.name) == NEW
    # Every matcher reads the message past the head: the receipt rule and a resumed seat's consumption alike.
    assert input_receipt.same_text(payload, post.typed([first]))
    item = Input(payload, post.now() + timedelta(seconds=1), 'accepted')
    assert thread_context.consumed_ids(svc.store, seat.name, 'worker', [item]) == {first.id}
    second = svc.store.new('order', 'desk', seat.name, 'An invented second order.')
    svc.deliver(seat, [second])
    assert live['box'] == post.typed([second]).rstrip('\n') or live['box'] == post.typed([second]), 'once only'


def test_two_edits_before_the_message_fold_into_one(setup):  # noqa: F811
    svc, seat, live, _ = setup
    publish(svc.runtime, seat.name, OLD)
    brief_change.seed(svc.runtime, seat.name, OLD)
    publish(svc.runtime, seat.name, NEW)
    publish(svc.runtime, seat.name, NEWER)
    order = svc.store.new('order', 'desk', seat.name, 'An invented order.')
    svc.deliver(seat, [order])
    box = live['box']
    assert box.count(input_receipt.BRIEF_HEAD) == 1 and box.count(input_receipt.BRIEF_HEAD_END) == 1
    assert '+Order two, amended.' in box and '+Seat line, moved.' in box
    assert brief_change.told(svc.runtime, seat.name) == NEWER


def test_a_withheld_delivery_tells_nothing(setup, monkeypatch):  # noqa: F811
    svc, seat, live, _ = setup
    publish(svc.runtime, seat.name, OLD)
    brief_change.seed(svc.runtime, seat.name, OLD)
    publish(svc.runtime, seat.name, NEW)
    live['box'] = 'text a person is typing'
    order = svc.store.new('order', 'desk', seat.name, 'An invented order.')
    svc.deliver(seat, [order])
    assert brief_change.told(svc.runtime, seat.name) == OLD, 'not typed, so not told'


def test_the_bound_holds_for_a_large_rewrite():
    before = OLD + ''.join(f'Old long line {n} of the orders, rewritten whole.\n' for n in range(600))
    after = NEW + ''.join(f'New long line {n} of the orders, rewritten whole.\n' for n in range(600))
    head = brief_change.compose('worker', before, after)
    assert len(head.encode()) < 1024, head
    assert "ship's orders:" in head and 'too long to show here: qd brief worker prints the whole' in head
    assert head.splitlines()[-1] == input_receipt.BRIEF_HEAD_END
    message = post.typed([post.Message('-'.join(('ord', '20320102', '1200', 'desk', f'{1:06x}')), 'order', 'desk', 'worker',
                                       post.now(), 'An invented order.')])
    assert input_receipt.without_brief_head(head + message) == message


def test_a_restart_clears_the_pending_change(migrated):  # noqa: F811
    r = migrated
    seat = r.worker
    brief_change.mark(r.ship.runtime, seat.name, 'An older brief the seat was told of.\n')
    seat.restart()
    published = (r.ship.runtime / 'brief' / f'{seat.name}.md').read_text()
    assert brief_change.told(r.ship.runtime, seat.name) == published
    assert brief_change.head(r.ship.runtime, seat.name) is None


def git(folder, *args):
    return subprocess.run(['git', '-C', str(folder), *args], check=True, capture_output=True, text=True).stdout


def commit(folder, text, message):
    (folder / 'orders.md').write_text(text)
    git(folder, 'add', 'orders.md')
    git(folder, '-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.invalid', 'commit', '-qm', message)


@pytest.fixture
def repository(tmp_path):
    folder, runtime = tmp_path / 'ship', tmp_path / 'ship' / 'runtime'
    folder.mkdir()
    git(folder, 'init', '-q')
    commit(folder, 'Orders.\n', 'first')
    brief_change.editor(folder, runtime)  # the reading starts at the folder's current commit
    return folder, runtime


def test_the_editor_is_named_by_its_trailer_or_its_remote_control_session(repository):
    folder, runtime = repository
    commit(folder, 'Orders, edited.\n', 'qd set seats.x.model = "y"\n\nQd-Seat: editor-seat')
    assert brief_change.editor(folder, runtime) == 'editor-seat'
    sessions = runtime / 'seats' / 'remote-seat' / 'home' / 'sessions'
    sessions.mkdir(parents=True)
    (sessions / '1234.json').write_text(json.dumps({'pid': '1234', 'bridgeSessionId': 'session_SyntheticAbc'}))
    commit(folder, 'Orders, edited again.\n', 'An edit\n\nClaude-Session: https://claude.example.invalid/code/session_SyntheticAbc')
    assert brief_change.editor(folder, runtime) == 'remote-seat'


def test_a_commit_that_names_no_seat_or_two_edits_by_two_seats_is_no_ones_own(repository):
    folder, runtime = repository
    commit(folder, 'Orders, edited.\n', 'An edit with no trailer')
    assert brief_change.editor(folder, runtime) is None
    commit(folder, 'One.\n', 'One\n\nQd-Seat: one-seat')
    commit(folder, 'Two.\n', 'Two\n\nQd-Seat: two-seat')
    assert brief_change.editor(folder, runtime) is None


def test_qd_set_names_its_caller_in_its_commit(repository, monkeypatch):
    folder, _ = repository
    (folder / 'ship.toml').write_text('''[ship]
name = "Synthetic set"
operator = "issuer"
[pools.fake]
provider = "synthetic"
[sessions.crew]
title = "Synthetic crew"
[seats.editor-seat]
session = "crew"
role = "worker"
harness = "fake"
pool = "fake"
model = "fake-1"
''')
    git(folder, 'add', 'ship.toml')
    git(folder, '-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.invalid', 'commit', '-qm', 'declare')
    ship = shipmod.load(folder)
    monkeypatch.setenv('QD_SEAT', 'editor-seat')
    monkeypatch.setenv('GIT_AUTHOR_NAME', 'Synthetic')
    monkeypatch.setenv('GIT_AUTHOR_EMAIL', 'synthetic@example.invalid')
    monkeypatch.setenv('GIT_COMMITTER_NAME', 'Synthetic')
    monkeypatch.setenv('GIT_COMMITTER_EMAIL', 'synthetic@example.invalid')
    shipmod.set_value(ship, 'seats.editor-seat', 'model', 'fake-2')
    assert git(folder, 'log', '-1', '--format=%B').rstrip().endswith('Qd-Seat: editor-seat')
    monkeypatch.setenv('QD_SEAT', 'no-such-seat')
    shipmod.set_value(shipmod.load(folder), 'seats.editor-seat', 'model', 'fake-1')
    assert 'Qd-Seat' not in git(folder, 'log', '-1', '--format=%B'), 'a caller that is no seat of the ship names none'


def test_an_office_seats_watch_delivery_carries_the_change_too(drydock, monkeypatch, capsys):
    monkeypatch.setenv('QD_SHIP', str(drydock))
    monkeypatch.setenv('QD_SEAT', 'desk')
    ship = shipmod.load(drydock)
    assert ship.session_of(ship.seat('desk')).human
    publish(ship.runtime, 'desk', OLD)
    brief_change.seed(ship.runtime, 'desk', OLD)
    publish(ship.runtime, 'desk', NEW)
    order = post.Store(ship.runtime).new('order', 'captain', 'desk', 'An invented order to an office seat.')
    cli.main(['inbox', 'desk', '--wait'])
    printed = capsys.readouterr().out
    assert printed.startswith(input_receipt.BRIEF_HEAD) and '+Order two, amended.' in printed
    assert post.typed([order]).strip() in printed
    assert brief_change.told(ship.runtime, 'desk') == NEW
