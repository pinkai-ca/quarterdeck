"""Compaction readings through the detailed and status-line Watch paths."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
from types import SimpleNamespace

import pytest

from quarterdeck import compaction, post, record_owner, watch
from quarterdeck.harness import get
from quarterdeck.harness.base import Screen
from quarterdeck.harness.limits import Usage, Window
from quarterdeck.headlines import WIDTH
from quarterdeck.ledger import Ledger
from quarterdeck.seat import Launched, Seat
from quarterdeck.ship import Pool, SeatSpec, Session, Ship
from tests.compaction_shapes import AT, Native


@pytest.fixture
def ship_view(tmp_path, monkeypatch):
    panes = {}
    tmux = SimpleNamespace(pane=panes.get, capture=lambda pane, **_: panes[pane].box)
    pools = {name: Pool(name, 'claude') for name in ('crew-pool', 'local-pool')}
    ship = Ship(tmp_path, 'synthetic', 'operator', tmp_path, pools, {'crew': Session('crew', 'Crew')}, {})
    monkeypatch.setattr(post, 'now', lambda: AT)
    monkeypatch.setattr(watch, 'service_state', lambda _: 'ok')
    monkeypatch.setattr(watch.accounting_health, 'reading', lambda _: None)  # a running service's healthy accounting
    monkeypatch.setattr(watch, 'disks', lambda _: [])
    monkeypatch.setattr(watch.credential, 'login_needed', lambda _: [])
    monkeypatch.setattr(get('claude'), 'screen', lambda box: Screen('Synthetic screen', box))

    def add(name, threshold=2, box=''):
        spec = SeatSpec(name, 'crew', 'worker', 'claude', ('crew-pool',), fresh_after_compactions=threshold)
        ship.seats[name] = spec
        seat = Seat(ship, spec, tmux)
        native = Native(seat.home / 'record.jsonl', 'claude', f'synthetic-{name}')
        native.finish()
        pane = SimpleNamespace(id=f'%{len(panes) + 1}', pid=100 + len(panes), dead=False, box=box)
        panes[pane.id] = pane
        Launched(pane.id, pane.pid, AT.isoformat(), 'crew-pool', None, ['claude'], 'synthetic',
                 harness='claude').write(seat.folder / 'launch.json')
        (seat.folder / 'record').write_text(str(native.path))
        record_owner.register(seat.folder, native.path, 'claude')
        compaction.tick(seat)
        return seat, native

    return ship, tmux, add


def test_routine_counts_keep_human_names_without_showing_an_unused_wall(ship_view, monkeypatch):
    ship, tmux, add = ship_view
    names = ('alpha', 'bravo', 'charlie')
    seats = [add(name)[0] for name in names]
    add('helper', threshold=None, box='Synthetic draft')
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(post, 'now', lambda: now)
    reset = now + timedelta(hours=5)
    with Ledger(ship.runtime / 'ledger.sqlite') as book:
        book.record_usage('crew-pool', Usage((Window('5h', .25, reset),), now))
        book.record_usage('local-pool', Usage((Window('5h', 1, reset),), now))

    line = watch.line(ship, tmux)
    routine = 'compactions 0/2: below compaction threshold'
    detailed = watch.table(ship, tmux)
    watched = [watch.render_full(watch.seat_reading(seat)) for seat in seats]
    print(f'LINE ({len(line)} chars): {line}')
    print(f'STATUS: {[row.strip() for row in detailed if routine in row]}')
    for name, rows in zip(names, watched):
        print(f'WATCH {name}: {[row.strip() for row in rows if routine in row]}')
    assert detailed.count(f'  {routine}') == 3
    assert all(f'  {routine}' in rows for rows in watched)
    assert len(line) <= WIDTH and 'helper needs a human' in line and 'local-pool' not in line
    assert 'compaction' not in line


@pytest.mark.parametrize(('case', 'expected'), [
    ('threshold', 'threshold reached; waiting for open handoff/readiness'),
    ('manual', 'ready for manual fresh: human-owned session'),
    ('unknown', 'compaction count unknown'),
    ('unreadable', 'compaction journal unreadable'),
    ('claimed-below', 'fresh transition claimed'),
    ('claimed-unknown', 'fresh transition claimed'),
    ('claimed-disabled', 'transition already claimed'),
    ('disabled', None),
    ('disabled-no-journal', None),
])
def test_compaction_alerts_from_reading_state(ship_view, case, expected):
    ship, tmux, add = ship_view
    seat, native = add('worker', threshold=0 if case == 'disabled-no-journal' else 2)
    if case in ('threshold', 'manual'):
        if case == 'manual':
            ship.sessions['crew'] = replace(seat.session, human=True)
            post.watch_file(ship.runtime, seat.name).write_text(f'{os.getpid()} {AT.isoformat()}\n')
        native.compact()
        native.compact()
        compaction.tick(seat)
    elif case == 'unknown':
        native.path.unlink()
        compaction.tick(seat)
    elif case == 'unreadable':
        compaction.path(ship.runtime, seat.name).write_text('{')
    elif case != 'disabled-no-journal':
        if case.startswith('claimed'):
            data = compaction.load(ship.runtime, seat.name)
            data['transition'] = dict(source=data['source_launch'], generation=data['generation'], handoff='synthetic-handoff')
            data['reading_unknown'] = case == 'claimed-unknown'
            compaction.save(ship.runtime, seat.name, data)
        if case in ('disabled', 'claimed-disabled'):
            ship.seats[seat.name] = replace(seat.spec, fresh_after_compactions=0)

    facts = watch.facts(ship, tmux)
    line = facts.line()
    print(f'{case}: {line}')
    if expected:
        assert len(facts.compactions) == 1 and expected in facts.compactions[0]
        assert expected in line
        if case == 'claimed-unknown':
            assert 'compaction count unknown' in line
    else:
        seat = Seat(ship, ship.seat(seat.name), tmux)
        assert not facts.compactions and 'compaction' not in line
        assert '  compaction automation disabled' in watch.table(ship, tmux)
        assert '  compaction automation disabled' in watch.render_full(watch.seat_reading(seat))


@pytest.mark.parametrize('join_reason', ['no record yet', 'ambiguous: two new records'])
def test_fresh_launch_does_not_render_the_previous_conversations_count(ship_view, monkeypatch, join_reason):
    from quarterdeck.harness.base import Join

    ship, tmux, add = ship_view
    seat, native = add('worker')
    ship.sessions['crew'] = replace(seat.session, human=True)
    native.compact()
    native.compact()
    compaction.tick(seat)
    assert 'compactions 2/2: ready for manual fresh' in compaction.status(seat)
    compaction.disarm(seat)
    previous = seat.launched()
    fresh = replace(previous, pid=previous.pid + 100, at=(AT + timedelta(seconds=10)).isoformat(),
                    resume=None, records_before=[str(native.path)])
    seat.pane().pid = fresh.pid
    fresh.write(seat.folder / 'launch.json')
    (seat.folder / 'record').unlink()
    monkeypatch.setattr(seat, 'join', lambda: Join(None, join_reason))
    before = compaction.path(ship.runtime, seat.name).read_bytes()

    if join_reason == 'no record yet':
        expected = 'compaction count pending: new conversation not yet joined'
        assert compaction.status(seat, alerts_only=True) is None
    else:
        expected = f'compaction count unknown: {join_reason}'
        assert compaction.status(seat, alerts_only=True) == expected
    assert compaction.status(seat) == expected
    assert f'  {expected}' in watch.render_full(watch.seat_reading(seat))
    assert compaction.path(ship.runtime, seat.name).read_bytes() == before
    assert compaction.load(ship.runtime, seat.name)['count'] == 2
    if join_reason == 'no record yet':
        joined = native.path.with_name('new-conversation.jsonl')
        joined.touch()
        monkeypatch.setattr(seat, 'join', lambda: Join(joined))
        assert compaction.status(seat) == ('compaction count unknown: '
                                           'awaiting compaction observation for the new conversation')
        assert compaction.path(ship.runtime, seat.name).read_bytes() == before


def test_a_resumed_launch_keeps_the_same_conversations_observed_count(ship_view):
    ship, tmux, add = ship_view
    seat, native = add('worker')
    native.compact()
    compaction.tick(seat)
    expected = compaction.status(seat)
    data = compaction.load(ship.runtime, seat.name)
    previous = seat.launched()
    resumed = replace(previous, pid=previous.pid + 100, at=(AT + timedelta(seconds=10)).isoformat(),
                      resume=data['source']['conversation'])
    seat.pane().pid = resumed.pid
    resumed.write(seat.folder / 'launch.json')
    before = compaction.path(ship.runtime, seat.name).read_bytes()
    assert compaction.status(seat) == expected == 'compactions 1/2: below compaction threshold'
    assert compaction.path(ship.runtime, seat.name).read_bytes() == before
