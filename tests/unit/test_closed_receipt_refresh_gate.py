"""Independent negative gate for the closed-receipt refresh (Claude side): what must never change, read or type."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
import json

import pytest

from quarterdeck import door, input_receipt, pending, post, receipt_refresh, record_owner
from quarterdeck.harness.json_record import RecordError
from quarterdeck.service import Service
from .test_closed_receipt_refresh_ordinary import AT, NEW, world  # noqa: F401 -- the shared synthetic world


def snapshot(world):
    return world.proof.path.read_bytes(), sorted((a.path.name, a.path.read_bytes()) for a in world.attempts.all())


@pytest.fixture
def guarded(world, monkeypatch):
    """No Door, no tmux input, no screen, no current record anywhere in the refresh."""
    def forbidden(*args, **kwargs):
        pytest.fail('the refresh touched a live seat')
    monkeypatch.setattr(door, 'Door', forbidden)
    world.seat.tmux = SimpleNamespace(capture=forbidden, keys=forbidden, paste=forbidden, click=forbidden,
                                      send=forbidden, pane=lambda ident: None)
    world.seat.record = forbidden
    return world


def test_an_empty_saved_record_is_never_joined_to_the_current_one(guarded, monkeypatch):
    world = guarded
    world.proof.save(record=None)
    world.seat.launched = lambda: SimpleNamespace(when=AT, pane='%synthetic', pid=123456789, at=AT.isoformat())
    before = snapshot(world)
    monkeypatch.setattr(input_receipt, 'confirmed', lambda *a: pytest.fail('read without a saved record'))
    receipt_refresh.run(world.service, NEW)
    assert snapshot(world) == before


@pytest.mark.parametrize('owners', [(), ('claude',), ('fake', 'claude')])
def test_a_record_not_bound_to_exactly_its_reader_is_left_alone(guarded, monkeypatch, tmp_path, owners):
    world = guarded
    for registry in (record_owner.REGISTRY, 'record-launches.jsonl'):
        (world.seat.folder / registry).unlink(missing_ok=True)
    for name in owners:
        record_owner.register(world.seat.folder, world.record, name)
    before = snapshot(world)
    monkeypatch.setattr(input_receipt, 'confirmed', lambda *a: pytest.fail('read an unowned record'))
    receipt_refresh.run(world.service, NEW)
    assert snapshot(world) == before


def test_a_live_launch_is_left_to_ordinary_reconcile(guarded, monkeypatch):
    world = guarded
    live = SimpleNamespace(when=AT, pane='%other', pid=1, at='2032-06-07T08:09:00+00:00')
    world.seat.launched = lambda: live
    world.seat.tmux = SimpleNamespace(**{**vars(world.seat.tmux), 'pane': lambda ident: object()})
    before = snapshot(world)
    monkeypatch.setattr(input_receipt, 'confirmed', lambda *a: pytest.fail('read a live launch'))
    receipt_refresh.run(world.service, NEW)
    assert snapshot(world) == before


@pytest.mark.parametrize('competing', ['abandoned', 'unacted', 'delivered', 'withheld'])
def test_an_outcome_that_wins_during_the_read_is_kept_and_the_pass_ends(guarded, monkeypatch, competing):
    world = guarded
    calls = []

    def match(*args):
        calls.append(True)
        current = world.attempts.all()[0]
        world.attempts._write(replace(current, result=competing, detail='synthetic competing outcome'))
        return 'delivered'

    monkeypatch.setattr(input_receipt, 'confirmed', match)
    receipt_refresh.run(world.service, NEW)
    after = snapshot(world)
    journal = json.loads(world.proof.path.read_text())
    assert journal['phase'] == 'not-in-closed-record' and 'receipt_refresh' not in journal
    assert world.attempts.all()[0].result == competing
    assert receipt_refresh.Checkpoint(world.runtime, NEW).complete, "a settled competitor is observed, not retried"
    receipt_refresh.run(world.service, NEW)
    del world.service.receipt_refresh_commit
    receipt_refresh.run(world.service, NEW)
    assert calls == [True] and snapshot(world) == after


def test_another_attempt_that_settled_the_message_wins(guarded):
    world = guarded
    original = world.attempts.all()[0]
    message = post.Store(world.runtime).get(original.message)
    later = world.attempts.open(message, 'seat-orchid', '%later', 1, (AT + timedelta(days=1)).isoformat())
    world.attempts.close(later, 'delivered', 'synthetic later receipt')
    before = snapshot(world)
    receipt_refresh.run(world.service, NEW)
    assert snapshot(world) == before


def test_a_cut_after_the_intent_settles_without_a_second_read_or_any_input(guarded, monkeypatch):
    world = guarded
    write = world.attempts._write
    monkeypatch.setattr(world.attempts, '_write', lambda attempt: (_ for _ in ()).throw(OSError('synthetic cut')))
    receipt_refresh.run(world.service, NEW)
    assert receipt_refresh.unfinished(json.loads(world.proof.path.read_text()))
    monkeypatch.setattr(world.attempts, '_write', write)
    monkeypatch.setattr(input_receipt, 'confirmed', lambda *a: pytest.fail('a second matcher read'))
    world.service.receipt_refresh_commit = None
    receipt_refresh.run(world.service, NEW)
    journal = json.loads(world.proof.path.read_text())
    assert journal['phase'] == world.attempts.all()[0].result == 'delivered'
    assert not receipt_refresh.unfinished(journal)


def test_an_unfinished_intent_refuses_explicit_resend(guarded, monkeypatch):
    world = guarded
    monkeypatch.setattr(world.attempts, '_write', lambda attempt: (_ for _ in ()).throw(OSError('synthetic cut')))
    receipt_refresh.run(world.service, NEW)
    message = post.Store(world.runtime).get(world.attempts.all()[0].message)
    seat = SimpleNamespace(**{**vars(world.seat), 'session': SimpleNamespace(human=False)})
    screen = SimpleNamespace(box='', unknown=None, interruption=None)
    before = snapshot(world)
    with pytest.raises(RecordError):
        pending.explicit_resend(world.service, seat, [message], screen)
    assert snapshot(world) == before


def test_a_refresh_failure_never_stops_the_rebrief(tmp_path):
    runtime = tmp_path / 'runtime'
    (runtime / 'post').mkdir(parents=True)
    (runtime / 'post' / 'receipt-refresh.json').write_text('{"not": "a checkpoint"}\n')
    (runtime / 'requests').mkdir()
    (runtime / 'requests' / f'upgrade-rebrief-{NEW}.json').write_text('{}\n')  # rebrief then returns at its own gate
    logged = []
    fake = SimpleNamespace(framework_commit=NEW, runtime=runtime, log=lambda *a, **k: logged.append((a, k)))
    Service.rebrief(fake)
    Service.rebrief(fake)
    assert len(logged) == 1 and logged[0][1]['step'] == 'receipt-refresh', "logged once, and the rebrief went on"
    assert (runtime / 'post' / 'receipt-refresh.json').read_text() == '{"not": "a checkpoint"}\n', "retained"
