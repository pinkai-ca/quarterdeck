"""Small hand context keeps its open work; worker text stays byte-for-byte stable."""

from datetime import datetime, timedelta, timezone
import hashlib
from types import SimpleNamespace

from quarterdeck import cli, post, post_threads, service, ship as shipmod
from quarterdeck.seat import Seat


AT = datetime(2032, 1, 2, 12, tzinfo=timezone.utc)


def world(folder, role):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'ship.toml').write_text(f'''[ship]
name = "Synthetic context"
operator = "issuer"
code = "checkout"
[pools.fake]
provider = "synthetic"
[sessions.crew]
title = "Synthetic crew"
[seats.recipient]
session = "crew"
role = "{role}"
harness = "fake"
pool = "fake"
model = "fake-1"
[seats.issuer]
session = "crew"
role = "worker"
harness = "fake"
pool = "fake"
model = "fake-1"
''')
    ship = shipmod.load(folder)
    store = post.Store(ship.runtime)
    def add(number, kind, sender, to, body, *, re=None, subject=None, due=None):
        message = post.Message(f'{kind[:3]}-20320102-1200-{sender}-{number:06x}', kind, sender, to,
                               AT + timedelta(seconds=number), body, re=re, subject=subject, due=due)
        store._write(message)
        return message
    old = add(1, 'order', 'issuer', 'recipient', 'Closed synthetic task.')
    closing = add(2, 'report', 'recipient', 'issuer', 'Synthetic task completed.', re=old.id)
    opened = add(3, 'order', 'issuer', 'recipient', 'Read the synthetic packet.',
                 subject='Current task', due='literal due')
    history = add(4, 'report', 'issuer', 'recipient', 'Unrelated synthetic received history.')
    sent = add(5, 'question', 'recipient', 'issuer', 'An unrelated outgoing wait.')
    question = add(6, 'question', 'recipient', 'issuer', 'Which synthetic option?', re=opened.id)
    answer = add(7, 'answer', 'issuer', 'recipient', 'Use the second synthetic option.', re=question.id)
    incoming = add(8, 'question', 'issuer', 'recipient', 'An open incoming question.')
    seat = Seat(ship, ship.seat('recipient'), object())
    return SimpleNamespace(ship=ship, store=store, seat=seat, old=old, closing=closing,
                           opened=opened, history=history, sent=sent, question=question,
                           answer=answer, incoming=incoming)


def test_worker_inbox_and_fresh_index_keep_the_original_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(post, 'now', lambda: AT + timedelta(seconds=30))
    w = world(tmp_path / 'ship', 'worker')
    # Captured on the unchanged implementation by tests/measure_hand_context.py.
    inbox = post.inbox_text(w.store, w.seat.name)
    assert hashlib.sha256(inbox.encode()).hexdigest() == '76d0a6020bda3f1c45f8569e6818fa599a3729acde0797796e45191b8238734d'
    opening, _ = w.seat.first_message()
    assert hashlib.sha256(opening.encode()).hexdigest() == '68a266d282061b7219ef96f5e7111833e532ed04458d1b7324449c4bda74967d'
    path = post.render_inbox(w.ship.runtime, w.store, w.seat.name)
    assert path.read_text() == inbox
    service.Service.inboxes(SimpleNamespace(runtime=w.ship.runtime, ship=w.ship, store=w.store))
    assert path.read_text() == inbox


def test_hand_inbox_contains_open_work_and_one_history_pointer(tmp_path, monkeypatch):
    monkeypatch.setattr(post, 'now', lambda: AT + timedelta(seconds=30))
    w = world(tmp_path / 'ship', 'hand')
    path = post.render_inbox(w.ship.runtime, w.store, w.seat.name)
    text = path.read_text()
    assert '## Open (2)' in text
    assert w.opened.body in text and w.incoming.body in text
    for message in (w.old, w.closing, w.history, w.sent, w.question, w.answer):
        assert message.body not in text
    assert '## Received' not in text
    assert text.count('Other messages stay in the message store.') == 1
    assert '`qd thread <id>`' in text and '`qd inbox --all`' in text
    before = path.stat()
    post.render_inbox(w.ship.runtime, w.store, w.seat.name)
    after = path.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_service_refreshes_the_hand_file_as_open_work_closes(tmp_path, monkeypatch):
    monkeypatch.setattr(post, 'now', lambda: AT + timedelta(seconds=30))
    w = world(tmp_path / 'ship', 'hand')
    runner = SimpleNamespace(runtime=w.ship.runtime, ship=w.ship, store=w.store)
    service.Service.inboxes(runner)
    path = w.ship.runtime / 'post' / 'inbox' / f'{w.seat.name}.md'
    assert path.read_text() == post.inbox_text(w.store, w.seat.name)
    w.store._write(post.Message(f'rep-20320102-1200-recipient-{9:06x}', 'report',
                               'recipient', 'issuer', AT + timedelta(seconds=9),
                               'Finished the current synthetic task.', re=w.opened.id))
    service.Service.inboxes(runner)
    text = path.read_text()
    assert '## Open (1)' in text
    assert w.opened.body not in text and w.incoming.body in text


def test_hand_with_no_open_work_keeps_only_the_history_pointer(tmp_path, monkeypatch):
    monkeypatch.setattr(post, 'now', lambda: AT + timedelta(seconds=30))
    w = world(tmp_path / 'ship', 'hand')
    w.store._write(post.Message(f'rep-20320102-1200-recipient-{9:06x}', 'report',
                               'recipient', 'issuer', AT + timedelta(seconds=9),
                               'Current task completed.', re=w.opened.id))
    w.store._write(post.Message(f'ans-20320102-1200-recipient-{10:06x}', 'answer',
                               'recipient', 'issuer', AT + timedelta(seconds=10),
                               'Current question answered.', re=w.incoming.id))
    text = post.inbox_text(w.store, w.seat.name)
    assert text == ('# Inbox: recipient\n\n## Open (0)\n\n'
                    'Other messages stay in the message store. `qd thread <id>` reads a thread; '
                    '`qd inbox --all` prints the complete history.\n')
    assert w.seat.carriage().rows == ()


def test_hand_fresh_index_selects_only_open_threads_with_their_amendments(tmp_path, monkeypatch):
    monkeypatch.setattr(post, 'now', lambda: AT + timedelta(seconds=30))
    w = world(tmp_path / 'ship', 'hand')
    carriage = w.seat.carriage()
    assert [row.id for row in carriage.rows] == [w.opened.id, w.incoming.id]
    assert carriage.rows[0].amendments == (w.question.id, w.answer.id)
    assert [row.active for row in carriage.rows] == [(w.opened.id,), (w.incoming.id,)]
    assert all(not row.awaiting for row in carriage.rows)
    assert [message.id for message in carriage.carried] == [w.opened.id, w.incoming.id]
    opening, carried = w.seat.first_message()
    assert [message.id for message in carried] == [w.opened.id, w.incoming.id]
    for message in (w.old, w.history, w.sent):
        assert message.id not in opening
    for message in (w.opened, w.incoming, w.question, w.answer):
        assert message.id in opening


def test_unrelated_received_history_does_not_grow_a_hand_context(tmp_path, monkeypatch):
    monkeypatch.setattr(post, 'now', lambda: AT + timedelta(seconds=120))
    w = world(tmp_path / 'ship', 'hand')
    inbox, opening = post.inbox_text(w.store, w.seat.name), w.seat.first_message()[0]
    for number in range(10, 90):
        w.store._write(post.Message(f'rep-20320102-1200-issuer-{number:06x}', 'report',
                                   'issuer', 'recipient', AT + timedelta(seconds=number),
                                   f'Unrelated synthetic history {number}.'))
    assert post.inbox_text(w.store, w.seat.name) == inbox
    assert w.seat.first_message()[0] == opening


def test_hand_history_still_reads_whole_through_inbox_all_and_thread(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(post, 'now', lambda: AT + timedelta(seconds=30))
    w = world(tmp_path / 'ship', 'hand')
    monkeypatch.setattr(cli, 'load_ship', lambda args: w.ship)
    monkeypatch.setattr(cli, '_inbox_target', lambda ship, seat: ('recipient', False, None))
    reading = cli.cmd_inbox(SimpleNamespace(seat='recipient', wait=False, all=True))
    cli.emit(SimpleNamespace(json=False), reading)
    output = capsys.readouterr().out
    for message in (w.old, w.history, w.answer):
        assert message.body in output
    assert post_threads.whole(w.store, w.history.id, w.seat.name).messages == (w.history,)
    assert w.answer in post_threads.whole(w.store, w.opened.id, w.seat.name).messages


def _nothing_open(w):
    w.store._write(post.Message(f'rep-20320102-1200-recipient-{9:06x}', 'report', 'recipient', 'issuer',
                                AT + timedelta(seconds=9), 'Current task completed.', re=w.opened.id))
    w.store._write(post.Message(f'ans-20320102-1200-recipient-{10:06x}', 'answer', 'recipient', 'issuer',
                                AT + timedelta(seconds=10), 'Current question answered.', re=w.incoming.id))


def _with_history(monkeypatch, seat):
    """A fresh start that carries a notice history, as a hand's start after an order does."""
    original = Seat.carriage

    def carriage(self, **kwargs):
        from dataclasses import replace
        history = 'qd > notice history: 1 changed order: settled by a later launch'
        return replace(original(self, **kwargs), history=history)
    monkeypatch.setattr(Seat, 'carriage', carriage)


def test_a_hand_with_nothing_open_ends_its_opening_with_the_line_of_its_card(tmp_path, monkeypatch):
    from quarterdeck import carriage, standing
    monkeypatch.setattr(post, 'now', lambda: AT + timedelta(seconds=30))
    w = world(tmp_path / 'ship', 'hand')
    _nothing_open(w)
    _with_history(monkeypatch, w.seat)
    opening, carried = w.seat.first_message()
    assert carried == [] and opening.endswith(carriage.HAND_IDLE + '\n'), opening
    assert carriage.HAND_IDLE in standing.card(w.ship, 'roles', 'hand')[0], 'the same words as the hand card'


def test_a_hand_with_an_order_open_has_no_idle_line(tmp_path, monkeypatch):
    from quarterdeck import carriage
    monkeypatch.setattr(post, 'now', lambda: AT + timedelta(seconds=30))
    w = world(tmp_path / 'ship', 'hand')
    _with_history(monkeypatch, w.seat)
    opening, _ = w.seat.first_message()
    assert w.opened.id in opening and carriage.HAND_IDLE not in opening


def test_a_worker_with_nothing_open_has_no_idle_line(tmp_path, monkeypatch):
    from quarterdeck import carriage
    monkeypatch.setattr(post, 'now', lambda: AT + timedelta(seconds=30))
    w = world(tmp_path / 'ship', 'worker')
    _nothing_open(w)
    _with_history(monkeypatch, w.seat)
    opening, _ = w.seat.first_message()
    assert opening and carriage.HAND_IDLE not in opening


def _forced_opening(w, tmp_path):
    """The opening the launch makes when its words do not fit and it carries the index in files (force_files)."""
    from quarterdeck import carriage as carriagemod, carriage_plan
    carried = w.seat.carriage()
    return carriage_plan.build(carried.rows, handoff=carried.text or '', header=carried.header,
                               directory=tmp_path / 'generation', limit=100000,
                               inline_limit=carriagemod.INLINE_BYTES, force_files=True,
                               history=carried.history, idle=w.seat._idle_line())


def test_a_hand_with_nothing_open_under_force_files_ends_with_the_same_line(tmp_path, monkeypatch):
    from quarterdeck import carriage
    monkeypatch.setattr(post, 'now', lambda: AT + timedelta(seconds=30))
    w = world(tmp_path / 'ship', 'hand')
    _nothing_open(w)
    _with_history(monkeypatch, w.seat)
    planned = _forced_opening(w, tmp_path)
    assert planned.index_path is not None, 'the index is carried in files'
    assert planned.opening.endswith(carriage.HAND_IDLE + '\n'), planned.opening
    assert planned.opening.count(carriage.HAND_IDLE) == 1


def test_a_hand_with_an_order_open_under_force_files_has_no_idle_line(tmp_path, monkeypatch):
    from quarterdeck import carriage
    monkeypatch.setattr(post, 'now', lambda: AT + timedelta(seconds=30))
    w = world(tmp_path / 'ship', 'hand')
    _with_history(monkeypatch, w.seat)
    planned = _forced_opening(w, tmp_path)
    assert planned.index_path is not None and carriage.HAND_IDLE not in planned.opening


def test_a_worker_with_nothing_open_under_force_files_has_no_idle_line(tmp_path, monkeypatch):
    from quarterdeck import carriage
    monkeypatch.setattr(post, 'now', lambda: AT + timedelta(seconds=30))
    w = world(tmp_path / 'ship', 'worker')
    _nothing_open(w)
    _with_history(monkeypatch, w.seat)
    planned = _forced_opening(w, tmp_path)
    assert planned.index_path is not None and planned.opening and carriage.HAND_IDLE not in planned.opening


def qd_inbox(w, capsys, *flags):
    capsys.readouterr()
    assert cli.main(['--ship', str(w.ship.folder), 'inbox', w.seat.name, *flags]) == 0
    return capsys.readouterr().out


def test_a_hands_plain_qd_inbox_reads_open_only_as_its_file_does(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(post, 'now', lambda: AT + timedelta(seconds=30))
    monkeypatch.delenv('QD_SEAT', raising=False)
    w = world(tmp_path / 'ship', 'hand')
    text = qd_inbox(w, capsys)
    assert text == post.inbox_text(w.store, w.seat.name), 'the same reading and the same line as its file'
    assert w.opened.body in text and w.incoming.body in text and '## Received' not in text
    assert text.count('Other messages stay in the message store.') == 1
    for message in (w.old, w.closing, w.history, w.sent, w.question, w.answer):
        assert message.body not in text
    every = qd_inbox(w, capsys, '--all')
    for message in (w.old, w.history, w.answer, w.opened, w.incoming):
        assert message.body in every, '--all still prints the whole history'


def test_a_workers_plain_qd_inbox_is_unchanged(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(post, 'now', lambda: AT + timedelta(seconds=30))
    monkeypatch.delenv('QD_SEAT', raising=False)
    w = world(tmp_path / 'ship', 'worker')
    text = qd_inbox(w, capsys)
    expected = post.render_inbox_reading(post.inbox_reading(w.store, w.seat.name, recent=True)).rstrip('\n') + '\n'
    assert text == expected and '## Received' in text and w.history.body in text
