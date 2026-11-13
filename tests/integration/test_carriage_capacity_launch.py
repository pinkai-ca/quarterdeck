"""Ordinary synthetic launch assembly; no native program or tmux server starts."""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import json

import pytest

from quarterdeck import cli, output, post, post_threads, recovery, standing
from quarterdeck.seat import Seat
from quarterdeck.ship import Pool, SeatSpec, Session, Ship, ShipError


def prepared(tmp_path, monkeypatch, *, policy=6000):
    spec = SeatSpec('builder', 'crew', 'worker', 'fake', ('synthetic-pool',), sandbox=False)
    declaration = Ship(tmp_path, 'synthetic', 'operator', tmp_path,
                       {'synthetic-pool': Pool('synthetic-pool', 'synthetic')},
                       {'crew': Session('crew', 'Synthetic crew')}, {'builder': spec}, carry_bytes=policy, code='checkout')
    seat = Seat(declaration, spec, SimpleNamespace())
    monkeypatch.setattr(seat, '_accept_pool', lambda: None)
    monkeypatch.setattr(recovery, 'require_clear', lambda _seat: None)
    monkeypatch.setattr(seat, '_observe_selection', lambda: ('fake', None))
    monkeypatch.setattr(seat, '_survives', lambda *_args: None)
    brief = tmp_path / 'synthetic-brief.md'
    brief.write_text('Invented standing instructions.\n')
    monkeypatch.setattr(standing, 'render', lambda *_args: brief)
    opened = []
    def open_window(argv, env, path):
        opened.append((argv, env, path))
        return SimpleNamespace(id='%synthetic-pane', pid=4321)
    monkeypatch.setattr(seat, '_open_window', open_window)
    return seat, opened


def publish(seat, name, body, *, sender='lead', to='builder', kind='order', parent=None, due=None):
    message = post.Message(name, kind, sender, to, datetime(2020, 1, 1, tzinfo=timezone.utc), body,
                           re=parent, subject='Synthetic subject ' + name, due=due)
    post.Store(seat.ship.runtime).write(message)
    return message


def first(opened):
    argv = opened[0][0]
    return argv[argv.index('--first') + 1]


def entries(opening):
    return [json.loads(line) for line in opening.splitlines() if line.startswith('{')]


def test_launch_indexes_large_ordinary_threads_without_copying_their_bodies(tmp_path, monkeypatch):
    # law: context-is-cache -- no ordinary body is typed; the explicit thread command must still print all of it
    seat, opened = prepared(tmp_path, monkeypatch)
    order = publish(seat, 'ordinary-order', 'Invented large task.\n' * 10000, due='tomorrow')
    answer = publish(seat, 'ordinary-amendment', 'Invented amendment.\n' * 1000, kind='answer', parent=order.id)
    before = {p: p.read_bytes() for p in post.Store(seat.ship.runtime).dir.iterdir()}
    proof = seat._up(resume=False)
    opening = first(opened)
    row, = entries(opening)
    assert row['id'] == order.id and row['due'] == 'tomorrow'
    assert row['amendments'] == [answer.id]
    assert order.body not in opening and answer.body not in opening
    assert not (seat.folder / 'carried').exists()
    assert '1 thread indexed' in proof
    assert len(opening.encode()) <= seat.ship.carry_bytes
    printed = post_threads.render(post_threads.whole(post.Store(seat.ship.runtime), answer.id, seat.name))
    assert order.body in printed and answer.body in printed
    assert printed.index(order.body) < printed.index(answer.body)
    assert {p: p.read_bytes() for p in before} == before


def test_fresh_handoff_renders_first_and_self_questions_are_only_indexed(tmp_path, monkeypatch):
    # law: context-is-cache -- full self-orders come first, while self questions remain metadata
    seat, opened = prepared(tmp_path, monkeypatch)
    incoming = publish(seat, 'older-order', 'Do not type this ordinary body.')
    question = publish(seat, 'self-question', 'Do not type this self-question.', sender='builder', kind='question')
    handoff = publish(seat, 'own-handoff', 'Read this complete self-handoff first.', sender='builder')
    seat._up(resume=False)
    opening = first(opened)
    assert handoff.body in opening and opening.index(handoff.body) < opening.index('open threads:')
    assert incoming.body not in opening and question.body not in opening
    assert {row['id'] for row in entries(opening)} == {incoming.id, question.id, handoff.id}


def test_actual_resume_has_index_without_retyping_self_handoff(tmp_path, monkeypatch):
    seat, opened = prepared(tmp_path, monkeypatch)
    handoff = publish(seat, 'own-handoff', 'Retain this in the resumed conversation.', sender='builder')
    monkeypatch.setattr(seat, '_resume_id', lambda: 'synthetic-session')
    seat._up(resume=True)
    opening = first(opened)
    assert handoff.body not in opening
    assert entries(opening)[0]['id'] == handoff.id


def test_restart_that_resolves_fresh_still_receives_full_handoff(tmp_path, monkeypatch):
    seat, opened = prepared(tmp_path, monkeypatch)
    handoff = publish(seat, 'own-handoff', 'Complete handoff for an actual fresh conversation.', sender='builder')
    monkeypatch.setattr(seat, '_resume_id', lambda: None)
    seat._up(resume=True)
    assert handoff.body in first(opened)


def test_large_handoff_is_whole_named_file_before_index_and_is_immutable(tmp_path, monkeypatch):
    seat, opened = prepared(tmp_path, monkeypatch)
    handoff = publish(seat, 'own-handoff', 'Invented complete handoff 🦉.\n' * 5000, sender='builder')
    ordinary = publish(seat, 'ordinary-order', 'Never copy this ordinary task.\n' * 5000)
    seat._up(resume=False)
    generation, = (seat.folder / 'carried').iterdir()
    path = generation / 'handoff.txt'
    assert path.read_text() == post.typed([handoff], whole=True)
    opening = first(opened)
    assert str(path) in opening and 'write a shorter one next time' in opening
    assert opening.index(str(path)) < opening.index('open threads:')
    assert len(opening.encode()) <= seat.ship.carry_bytes
    assert all(p.stat().st_size <= seat.ship.carry_bytes for p in generation.iterdir() if p != path)
    assert all(ordinary.body not in p.read_text() for p in generation.iterdir())
    assert not any(p.name.startswith(('view-', 'parts-')) for p in generation.iterdir())


def test_sent_open_orders_and_questions_are_waits_without_delivery_attempts(tmp_path, monkeypatch):
    seat, opened = prepared(tmp_path, monkeypatch)
    sent = publish(seat, 'sent-order', 'Do not retype outgoing work.', sender='builder', to='reviewer')
    seat._up(resume=False)
    row, = entries(first(opened))
    assert row['active'] == []
    assert row['awaiting'] == [f'{sent.id}: sent, awaiting reviewer']
    assert sent.body not in first(opened)
    assert post.Attempts(seat.ship.runtime).all() == []


def test_unread_answer_has_one_thread_line_with_its_answered_question(tmp_path, monkeypatch):
    seat, opened = prepared(tmp_path, monkeypatch)
    question = publish(seat, 'sent-question', 'Invented question.', sender='builder', to='lead', kind='question')
    answer = publish(seat, 'unread-answer', 'Invented answer.', parent=question.id, kind='answer')
    seat._up(resume=False)
    row, = entries(first(opened))
    assert row['id'] == question.id and row['amendments'] == [answer.id]
    assert row['awaiting'] == []
    assert answer.body not in first(opened)


def test_report_is_pending_only_until_existing_delivery_evidence(tmp_path, monkeypatch):
    seat, _ = prepared(tmp_path, monkeypatch)
    report = publish(seat, 'incoming-report', 'Invented pending report.', kind='report')
    assert [row.id for row in seat.carriage().rows] == [report.id]
    attempts = post.Attempts(seat.ship.runtime)
    attempt = attempts.open(report, seat.name, '%synthetic-pane', 4321, '2020-01-01T00:00:01+00:00')
    attempts.close(attempt, 'delivered', 'synthetic existing receipt')
    assert seat.carriage().rows == ()


def test_index_only_launch_is_not_delivery_of_an_unread_report(tmp_path, monkeypatch):
    seat, opened = prepared(tmp_path, monkeypatch)
    report = publish(seat, 'incoming-report', 'Invented not-yet-delivered report.', kind='report')
    seat._up(resume=False)
    assert report.body not in first(opened)
    assert [row.id for row in seat.carriage().rows] == [report.id]
    attempts = post.Attempts(seat.ship.runtime)
    original = {path: path.read_bytes() for path in attempts.dir.iterdir() if path.is_file()}
    seat.carriage()
    assert {path: path.read_bytes() for path in original} == original, 'index reads do not rewrite receipts'
    actual = attempts.open(report, seat.name, '%synthetic-pane', 4321, seat.launched().at)
    attempts.close(actual, 'delivered', 'synthetic later confirmed delivery')
    assert seat.carriage().rows == ()


def test_held_unconfirmed_report_is_indexed_without_altering_its_attempt(tmp_path, monkeypatch):
    seat, _ = prepared(tmp_path, monkeypatch)
    report = publish(seat, 'held-report', 'Invented unresolved report.', kind='report')
    attempts = post.Attempts(seat.ship.runtime)
    held = attempts.open(report, seat.name, '%synthetic-pane', 4321, '2020-01-01T00:00:01+00:00')
    attempts.close(held, 'pending', 'synthetic unresolved original delivery')
    before = held.path.read_bytes()
    assert [row.id for row in seat.carriage().rows] == [report.id]
    assert held.path.read_bytes() == before and len(attempts.all()) == 1


def test_later_index_reference_does_not_hide_a_prior_confirmed_report_delivery(tmp_path, monkeypatch):
    seat, _ = prepared(tmp_path, monkeypatch)
    report = publish(seat, 'delivered-report', 'Invented already delivered report.', kind='report')
    attempts = post.Attempts(seat.ship.runtime)
    confirmed = attempts.open(report, seat.name, '%synthetic-pane', 4321, '2020-01-01T00:00:01+00:00')
    attempts.close(confirmed, 'delivered', 'synthetic existing positive delivery')
    later = attempts.open(report, seat.name, '%synthetic-pane', 4321, '2020-01-01T00:00:02+00:00')
    attempts.close(later, 'launch', post_threads.INDEXED_LAUNCH_DETAIL)
    before = {path: path.read_bytes() for path in attempts.dir.iterdir() if path.is_file()}
    assert seat.carriage().rows == ()
    assert {path: path.read_bytes() for path in before} == before


def test_impossible_minimum_stops_before_any_window_or_publication(tmp_path, monkeypatch):
    seat, opened = prepared(tmp_path, monkeypatch, policy=100)
    publish(seat, 'ordinary-order', 'Invented task.')
    with pytest.raises(ShipError, match='minimal'):
        seat._up(resume=False)
    assert opened == []
    assert not (seat.folder / 'carried').exists()
    assert post.Attempts(seat.ship.runtime).all() == []


def test_qd_thread_parser_prints_full_graph_and_json_without_receipting(tmp_path, monkeypatch):
    seat, _ = prepared(tmp_path, monkeypatch)
    order = publish(seat, 'ordinary-order', 'Complete invented task.')
    reply = publish(seat, 'ordinary-answer', 'Complete invented reply.', parent=order.id, kind='answer')
    monkeypatch.setenv('QD_SEAT', seat.name)
    monkeypatch.setattr(cli, 'load_ship', lambda _args: seat.ship)
    args = cli.build_parser().parse_args(['thread', reply.id])
    reading = args.run(args)
    assert [message.id for message in reading.data.messages] == [order.id, reply.id]
    rendered = reading.render(reading.data)
    assert order.body in rendered and reply.body in rendered
    data = output.value(reading.data)
    assert data['root'] == order.id and data['messages'][1]['body'] == reply.body
    assert post.Attempts(seat.ship.runtime).all() == []


def test_synthetic_first_message_demo_for_review(tmp_path, monkeypatch):
    """The actual launch renderer, with invented posts, for the desk's display."""
    seat, opened = prepared(tmp_path, monkeypatch, policy=100000)
    task = publish(seat, 'order-sample', 'The sample task stays a file.', due='tomorrow')
    handoff = publish(seat, 'order-handoff', 'Synthetic handoff: the sample is ready. Continue its review.', sender='builder')
    waiting = publish(seat, 'question-review', 'The outgoing question stays a file.',
                      sender='builder', to='reviewer', kind='question')
    seat._up(resume=False)
    opening = first(opened)
    assert handoff.body in opening and opening.index(handoff.body) < opening.index('open threads:')
    assert task.body not in opening and waiting.body not in opening
    rows = {row['id']: row for row in entries(opening)}
    assert rows[task.id]['due'] == 'tomorrow'
    assert rows[waiting.id]['awaiting'] == [f'{waiting.id}: sent, awaiting reviewer']
    assert 'carry_bytes=100000 (policy bound)' in opening
    (tmp_path / 'first-message-demo.txt').write_text(opening)
