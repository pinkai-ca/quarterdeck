"""Post traffic refreshes only the post facts, over the last full collection: what a full collection would give."""


from quarterdeck import post, ship as shipmod, watch


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
