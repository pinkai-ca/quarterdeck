"""Post traffic refreshes only the post facts, over the last full collection: what a full collection would give."""


from datetime import datetime, timedelta, timezone
import os
from types import SimpleNamespace

import pytest

from quarterdeck import headlines, post, presentation, servicectl, ship as shipmod, watch


def test_a_post_refresh_gives_what_a_full_collection_would_and_shows_a_blind_delivery_at_once(drydock, tmux):
    ship = shipmod.load(drydock)
    store, attempts = post.Store(ship.runtime), post.Attempts(ship.runtime)
    before, kept = watch.collect(ship, tmux, store=store, attempts=attempts)
    message = store.new('order', 'desk', 'captain', 'Synthetic status work', subject='Synthetic status work')
    refreshed = watch.refresh_post(kept, store=store, attempts=attempts)
    assert refreshed == watch.facts(ship, tmux, store=store, attempts=attempts)
    assert refreshed.waiting == before.waiting + 1
    # law: nothing-waits-silently -- a blind delivery shows within one post refresh, not at the next full one
    attempts.close(attempts.open(message, 'captain', None, None, None), 'blind')
    refreshed = watch.refresh_post(kept, store=store, attempts=attempts)
    assert refreshed.blind == 1 and 'blind' in refreshed.line()
    assert refreshed == watch.facts(ship, tmux, store=store, attempts=attempts)


def test_a_post_refresh_declines_where_a_seats_reading_stopped_at_a_post_step(drydock, tmux):
    ship = shipmod.load(drydock)
    store, attempts = post.Store(ship.runtime), post.Attempts(ship.runtime)
    _, kept = watch.collect(ship, tmux, store=store, attempts=attempts)
    name = kept.selected[0].name
    kept.steps[name] = [watch._Step([('seat_holds', f'{name}: held for this seat alone: OSError: x', 'append')],
                                    stop=True)]  # its first step, the post's, raised: the steps after never ran
    assert watch.refresh_post(kept, store=store, attempts=attempts) is None, 'only a full collection can say'


@pytest.mark.usefixtures('synthetic_disk_room')
def test_waiting_headline_uses_the_oldest_destination_in_full_and_post_only_publications(drydock, tmux, monkeypatch):
    ship = shipmod.load(drydock)
    store, attempts = post.Store(ship.runtime), post.Attempts(ship.runtime)
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    monkeypatch.setattr(post, 'now', lambda: now)
    earlier = store.new('question', 'captain', 'worker-1', 'Synthetic delivered message')
    attempts.close(attempts.open(earlier, earlier.to, None, None, None), 'delivered')
    now += timedelta(minutes=11)
    desk = store.new('question', 'worker-1', 'desk', 'Synthetic message for an operator-owned watch')
    now += timedelta(minutes=17)
    worker = store.new('order', 'desk', 'captain', 'Synthetic crew delivery')
    now += timedelta(minutes=8)

    wall = int(now.timestamp()) * 1_000_000_000
    monkeypatch.setattr(presentation, 'time', SimpleNamespace(time_ns=lambda: wall, monotonic_ns=lambda: 0))
    pid = os.getpid()
    monkeypatch.setattr(servicectl, 'read_heartbeat', lambda *args, **kwargs: (pid, 0))
    monkeypatch.setattr(servicectl, 'read_pass', lambda *args, **kwargs: (pid, 0, 'pass'))
    monkeypatch.setattr(watch, 'accounting_reading', lambda runtime, state: None)
    with servicectl.acquire_lock(ship.runtime):
        facts = watch.facts(ship, tmux, store=store, attempts=attempts)
        assert (facts.waiting, facts.oldest, facts.oldest_waiting_seat, facts.unarmed) == (2, 25, desk.to, ['desk'])
        framework = presentation.framework_stamp()
        written, kept = presentation.publish_collected(ship, tmux, framework, store, attempts)
        assert written and kept is not None
        assert presentation.published_line(ship.folder) == facts.line()
        assert f'2 waiting, oldest 25m ({desk.to})' in facts.line().split(headlines.SEPARATOR)

        for waiting in ('2 waiting, oldest 25m', '2 waiting'):
            # Reserve the remaining columns for a wide annotation, the unarmed count and the pointer.
            other = ('service ok', waiting, '1 unarmed', headlines.POINTER)
            room = headlines.WIDTH - headlines.columns(headlines.SEPARATOR.join(other)) - len(headlines.SEPARATOR)
            padding = '界' * (room // 2) + 'x' * (room % 2)
            notes = (headlines.note(padding, padding),)
            cold = presentation.collected_line(facts, None, notes=notes)
            published, age = presentation.published_reading(ship.folder, notes=notes)
            assert published == cold and age is None
            assert cold.split(headlines.SEPARATOR) == ['service ok', padding, waiting, '1 unarmed', headlines.POINTER]
            assert headlines.columns(cold) == headlines.WIDTH

        attempts.close(attempts.open(desk, desk.to, None, None, None), 'watch')
        written, _ = presentation.publish_collected(ship, tmux, framework, store, attempts, kept=kept)
        assert written
        facts = watch.facts(ship, tmux, store=store, attempts=attempts)
        assert (facts.waiting, facts.oldest, facts.oldest_waiting_seat) == (1, 8, worker.to)
        assert f'1 waiting, oldest 8m ({worker.to})' in facts.line().split(headlines.SEPARATOR)
        assert presentation.published_line(ship.folder) == facts.line()
