"""Synthetic local ref observations, wall time and immutable reminder posts."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from quarterdeck import durable, post, push_reminder, ship as shipmod
from quarterdeck.service import Service
from quarterdeck.ship import ShipError


START = datetime(2026, 1, 1, tzinfo=timezone.utc)
BASE, FIRST, SECOND, THIRD = (char * 40 for char in 'abcd')
SEAT = 'reviewer'  # any declared seat; the framework names none of its own


@pytest.fixture
def scene(tmp_path, monkeypatch):
    clock = SimpleNamespace(at=START, tip=BASE)
    ship = SimpleNamespace(runtime=tmp_path / 'runtime', seats={SEAT: object()}, push_reminder=SEAT)
    def service():
        return SimpleNamespace(ship=ship, runtime=ship.runtime, store=post.Store(ship.runtime),
                               log=lambda *args, **kwargs: None)
    monkeypatch.setattr(push_reminder, 'now', lambda: clock.at)
    monkeypatch.setattr(push_reminder, 'current_tip', lambda ship: clock.tip)
    return clock, service


def test_one_push_files_one_order_at_twenty_minutes(scene):
    clock, service = scene
    svc = service()
    Service.push_reminders(svc)
    assert svc.store.all() == []  # an existing ref is the initial baseline
    clock.at += timedelta(seconds=5)
    clock.tip = FIRST
    Service.push_reminders(svc)
    deadline = clock.at + push_reminder.DELAY
    state_bytes = push_reminder.path(svc.runtime).read_bytes()
    clock.at = deadline - timedelta(microseconds=1)
    Service.push_reminders(svc)
    assert svc.store.all() == []
    assert push_reminder.path(svc.runtime).read_bytes() == state_bytes
    clock.at = deadline
    Service.push_reminders(svc)
    [message] = svc.store.all()
    assert (message.kind, message.sender, message.to) == ('order', post.SHIP, SEAT)
    assert FIRST in message.body and BASE not in message.body
    assert 'failed run at once' in message.body
    assert 'gh run watch RUN_ID --exit-status' in message.body
    assert datetime.fromisoformat(message.due) == deadline
    for _ in range(3):
        clock.at += push_reminder.DELAY
        Service.push_reminders(service())
    assert len(svc.store.all()) == 1


def test_three_pushes_coalesce_until_twenty_minutes_after_newest(scene):
    clock, service = scene
    svc = service()
    push_reminder.run(svc)
    for tip in (FIRST, SECOND, THIRD):
        clock.at += timedelta(minutes=5)
        clock.tip = tip
        push_reminder.run(svc)
    clock.at = START + timedelta(minutes=25)
    push_reminder.run(svc)
    assert svc.store.all() == []
    clock.at = START + timedelta(minutes=35)
    push_reminder.run(svc)
    [message] = svc.store.all()
    assert [line[2:] for line in message.body.splitlines() if line.startswith('- ')] == [FIRST, SECOND, THIRD]
    assert datetime.fromisoformat(message.due) == clock.at
    clock.tip = BASE  # another observed transition is its own later batch
    push_reminder.run(svc)
    clock.at += push_reminder.DELAY
    push_reminder.run(svc)
    messages = svc.store.all()
    assert len(messages) == 2
    assert BASE in messages[-1].body


def test_restart_keeps_original_deadline_and_gives_exactly_one_order(scene):
    clock, service = scene
    push_reminder.run(service())
    clock.tip = FIRST
    push_reminder.run(service())
    clock.at += timedelta(minutes=10)
    push_reminder.run(service())
    clock.at += timedelta(minutes=10)
    push_reminder.run(service())
    clock.at += timedelta(days=1)
    svc = service()
    push_reminder.run(svc)
    assert len(svc.store.all()) == 1


@pytest.mark.parametrize('after_post', [False, True])
def test_interrupted_publication_recovers_same_post_identity(scene, monkeypatch, after_post):
    clock, service = scene
    svc = service()
    push_reminder.run(svc)
    clock.tip = FIRST
    push_reminder.run(svc)
    clock.at += push_reminder.DELAY
    original = post.Store._write
    def cut(store, message):
        if after_post:
            original(store, message)
        raise OSError('synthetic interrupted publication')
    with monkeypatch.context() as patch:
        patch.setattr(post.Store, '_write', cut)
        with pytest.raises(OSError, match='interrupted publication'):
            push_reminder.run(svc)
    intent = post.Message.from_json(push_reminder.load(svc.runtime, SEAT)['intent'])
    assert len(svc.store.all()) == int(after_post)
    push_reminder.run(service())
    push_reminder.run(service())
    [message] = svc.store.all()
    assert message.to_json() == intent.to_json()
    assert push_reminder.load(svc.runtime, SEAT)['intent'] is None


def test_absent_ref_can_appear_and_missing_ref_does_not_erase_pending(scene):
    clock, service = scene
    clock.tip = None
    svc = service()
    push_reminder.run(svc)
    clock.tip = FIRST
    push_reminder.run(svc)
    clock.tip = None
    clock.at += push_reminder.DELAY
    push_reminder.run(service())
    assert FIRST in svc.store.all()[0].body


def test_failed_ref_read_does_not_lose_already_due_reminder(scene, monkeypatch):
    clock, service = scene
    svc = service()
    push_reminder.run(svc)
    clock.tip = FIRST
    push_reminder.run(svc)
    clock.at += push_reminder.DELAY
    def unreadable(ship):
        raise ShipError('synthetic unavailable ref')
    monkeypatch.setattr(push_reminder, 'current_tip', unreadable)
    push_reminder.run(service())
    assert FIRST in svc.store.all()[0].body


def test_no_declared_seat_is_off_and_reads_and_writes_nothing(scene, monkeypatch):
    _, service = scene
    svc = service()
    svc.ship.push_reminder = None  # the seat still exists; only the key is absent
    monkeypatch.setattr(push_reminder, 'current_tip', lambda ship: pytest.fail('unexpected read'))
    push_reminder.run(svc)
    assert not svc.runtime.exists()


def test_current_tip_reads_exact_local_ref_without_network(tmp_path, monkeypatch):
    def git(*args):
        return subprocess.check_output(['git', '-C', str(tmp_path), *args], stderr=subprocess.PIPE)
    git('init', '--quiet')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
        'commit', '--allow-empty', '--quiet', '-m', 'synthetic')
    tip = git('rev-parse', 'HEAD').decode().strip()
    monkeypatch.setattr(push_reminder.install, 'source_repo', lambda ship: tmp_path)
    assert push_reminder.current_tip(object()) is None
    git('update-ref', 'refs/remotes/origin/main-other', tip)
    assert push_reminder.current_tip(object()) is None
    git('update-ref', push_reminder.REF, tip)
    assert push_reminder.current_tip(object()) == tip


def test_invalid_state_is_retained(scene):
    _, service = scene
    svc = service()
    target = push_reminder.path(svc.runtime)
    target.parent.mkdir(parents=True)
    target.write_text('{"version": 999}')
    with pytest.raises(ShipError, match='journal invalid'):
        push_reminder.run(svc)
    assert target.read_text() == '{"version": 999}'


def test_saved_intent_uses_public_store_publication(scene, monkeypatch):
    clock, service = scene
    svc = service()
    push_reminder.run(svc)
    clock.tip = FIRST
    push_reminder.run(svc)
    clock.at += push_reminder.DELAY
    original = post.Store.write
    published = []
    def write(store, message):
        assert push_reminder.load(svc.runtime, SEAT)['intent'] == message.to_json()
        published.append(message.id)
        return original(store, message)
    monkeypatch.setattr(post.Store, 'write', write)
    push_reminder.run(svc)
    push_reminder.run(service())
    [message] = svc.store.all()
    assert published == [message.id]


def test_ref_error_logs_only_when_changed_or_cleared(scene, monkeypatch):
    clock, service = scene
    svc = service()
    logged = []
    svc.log = lambda event, **fields: logged.append((event, fields))
    reading = [ShipError('synthetic unavailable ref')]
    def current(ship):
        if isinstance(reading[0], Exception):
            raise reading[0]
        return reading[0]
    monkeypatch.setattr(push_reminder, 'current_tip', current)
    for _ in range(3):
        push_reminder.run(svc)
    assert logged == [('error', {'step': 'push-reminder-ref', 'error': 'synthetic unavailable ref'})]
    reading[0] = OSError('synthetic changed ref error')
    for _ in range(3):
        push_reminder.run(svc)
    assert len(logged) == 2
    assert logged[-1] == ('error', {'step': 'push-reminder-ref', 'error': 'synthetic changed ref error'})
    reading[0] = clock.tip
    for _ in range(3):
        push_reminder.run(svc)
    assert len(logged) == 3
    assert logged[-1] == ('push-reminder-ref-available', {})
    reading[0] = ShipError('synthetic unavailable ref')
    push_reminder.run(svc)
    assert len(logged) == 4
    assert logged[-1] == logged[0]


def test_an_unpublished_order_to_the_declared_seat_is_found_again_and_filed_once(scene, monkeypatch):
    clock, service = scene
    svc = service()
    push_reminder.run(svc)
    clock.tip = FIRST
    push_reminder.run(svc)
    clock.at += push_reminder.DELAY
    def cut(store, message):
        raise OSError('synthetic cut before the post')
    with monkeypatch.context() as patch:
        patch.setattr(post.Store, 'write', cut)
        with pytest.raises(OSError, match='synthetic cut'):
            push_reminder.run(svc)
    intent = post.Message.from_json(push_reminder.load(svc.runtime, SEAT)['intent'])
    assert (intent.kind, intent.sender, intent.to) == ('order', post.SHIP, SEAT)
    with pytest.raises(ShipError, match='journal invalid'):
        push_reminder.load(svc.runtime, 'other')  # an order saved for one seat is never replayed to another
    push_reminder.run(service())
    [message] = svc.store.all()
    assert (message.id, message.to) == (intent.id, SEAT)
    assert push_reminder.load(svc.runtime, SEAT)['intent'] is None


SHIP_TEXT = """[ship]
name = "synthetic"
operator = "operator"
{line}
[pools.example]
provider = "fake"
[sessions.crew]
[seats.reviewer]
session = "crew"
role = "worker"
harness = "fake"
pool = "example"
"""


def load_ship(tmp_path, line=''):
    folder = tmp_path / 'synthetic'
    folder.mkdir(exist_ok=True)
    (folder / 'ship.toml').write_text(SHIP_TEXT.format(line=line))
    return shipmod.load(folder)


def test_the_ship_file_declares_the_seat_or_leaves_the_reminder_off(tmp_path):
    assert load_ship(tmp_path).push_reminder is None
    assert load_ship(tmp_path, 'push_reminder = "reviewer"').push_reminder == 'reviewer'


@pytest.mark.parametrize('line,refusal', [
    ('push_reminder = "una"', r"ship\.push_reminder names 'una', which is not a declared seat; declared: reviewer"),
    ('push_reminder = ""', r'ship\.push_reminder must name a declared seat'),
    ('push_reminder = true', r'ship\.push_reminder must name a declared seat'),
    ('push_reminder = ["reviewer"]', r'ship\.push_reminder must name a declared seat'),
])
def test_a_reminder_seat_that_is_not_declared_is_refused_at_load(tmp_path, line, refusal):
    with pytest.raises(ShipError, match=refusal):
        load_ship(tmp_path, line)
