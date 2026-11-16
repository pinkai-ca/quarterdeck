"""The forum's protection boundaries, from synthetic evidence only; no program, provider or tmux server.

A member is named failed only from its own present, positive evidence: the exact copy refused in its
own refusal journal, its own launch's pane dead, or the seat held at its limit with no eligible
destination. Absence, a reused pane id, a missing launch, a busy or changing lifecycle, an unresolved
selection, a wall with somewhere to go, elapsed time or unreadable evidence names no failure. A
positive outcome is kept for its copy alone; the one terminal notice waits for every member and is
fixed once saved. Bad evidence holds only its own group, once, on the status line. Each reviewer's
inbox and carriage hold its own messages only; the lead is carried its terminal notice. The shared
post folder stays readable and writable from a jail, as the cards and DESIGN say.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import group_post, ledger, post, quota, refusal, sandbox, ship as shipmod
from quarterdeck.seat import Launched, Seat, SelectionError
from quarterdeck.service import Service
from quarterdeck.tmux import Pane
from tests.unit.thread_reading import index_rows, thread_text


ROOT = Path(__file__).resolve().parents[2]
MEMBERS = ('reviewer-1', 'reviewer-2', 'reviewer-3', 'reviewer-4')
BODY = 'Review this fictional proposal on its own.\n'
SHIP = ('[ship]\nname="forum"\noperator="operator"\nworkspace="work"\n'
        '[pools.fake-a]\nprovider="synthetic"\n[pools.fake-b]\nprovider="synthetic"\n'
        '[sessions.forum]\ntitle="Synthetic forum"\n'
        '[groups.reviewers]\nlead="lead"\nmembers=["reviewer-1","reviewer-2","reviewer-3","reviewer-4"]\n'
        + ''.join(f'\n[seats.{name}]\nsession="forum"\nrole="{"lead" if name == "lead" else "reviewer"}"\n'
                  'harness="fake"\npool=["fake-a","fake-b"]\nmodel="fake-1"\n'
                  for name in ('lead', *MEMBERS)))


class World:
    """A forum ship, a fake tmux holding only the panes a test puts there, and spent pools by name."""

    def __init__(self, folder: Path, monkeypatch):
        (folder / 'ship.toml').write_text(SHIP)
        self.ship = shipmod.load(folder)
        self.panes: dict[str, Pane] = {}
        self.walls: set[str] = set()
        self.wall_reads: list[tuple[str, str]] = []
        self.tmux = SimpleNamespace(pane=lambda ident: self.panes.get(ident))
        self.service = SimpleNamespace(ship=self.ship, runtime=self.ship.runtime, tmux=self.tmux, seat=self.seat)
        self.store = post.Store(self.ship.runtime)
        self.pid = 4000
        self.ship.runtime.mkdir(parents=True, exist_ok=True)
        ledger.Ledger(self.ship.runtime / 'ledger.sqlite').close()  # the book the status reading opens read-only

        def walled(seat, book):
            book.db.execute('SELECT count(*) FROM sqlite_master').fetchone()  # read the handed book, as the real reader does
            self.wall_reads.append((seat.name, seat.pool.name))
            return seat.pool.name in self.walls

        monkeypatch.setattr(quota, 'current_walled', walled)
        monkeypatch.setattr(quota, 'cached_open', lambda ship, book, pool, seat=None: pool not in self.walls)

    def seat(self, name: str) -> Seat:
        return Seat(self.ship, self.ship.seat(name), self.tmux)

    def launch(self, name: str, *, pool: str = 'fake-a', dead: bool = False, shown: bool = True) -> Launched:
        """A saved launch for the seat; ``shown`` puts its own pane, same id and process, in tmux."""
        self.pid += 1
        seat = self.seat(name)
        seat.folder.mkdir(parents=True, exist_ok=True)
        seat.trusted.mkdir(parents=True, exist_ok=True)
        launched = Launched(f'%{self.pid}', self.pid, post.now().isoformat(), pool, None, ['fake'], 'synthetic', 'fake')
        launched.write(seat.trusted / 'launch.json')
        if shown:
            self.show(launched.pane, launched.pid, dead=dead)
        return launched

    def show(self, pane_id: str, pid: int, *, dead: bool = False) -> None:
        self.panes[pane_id] = Pane(pane_id, 'Synthetic forum', 'w', pid, 120, 40, False, dead, 1 if dead else None)

    def refuse(self, name: str, *message_ids: str, launch: dict | None = None, stage: str = 'held') -> None:
        launch = launch or {'pane': '%1', 'pid': 1, 'at': post.now().isoformat()}
        events = refusal.load(self.ship.runtime, name)
        events.append({'stage': stage, 'launch': launch, 'turn': None,
                       'quarantined': [{'message': ident} for ident in message_ids], 'notices': []})
        refusal.save(self.ship.runtime, name, events)

    def submit(self):
        return group_post.submit(self.ship, self.store, 'reviewers', 'operator', BODY)

    def file(self, copies, body='synthetic review'):
        return [self.store.new('report', m.to, 'lead', f'{m.to}: {body}', re=m.id) for m in copies]

    def failure(self, name: str, copy: post.Message):
        return group_post.member_failure(self.service, name, copy)

    def advance(self):
        return group_post.advance(self.ship, post.Store(self.ship.runtime),
                                  lambda name, copy: group_post.member_failure(self.service, name, copy))

    def notices(self, root=None):
        return [m for m in post.Store(self.ship.runtime).for_seat('lead')
                if m.sender == post.SHIP and (root is None or m.re == root.id)]

    def journal(self, root) -> Path:
        return self.ship.runtime / 'post/groups' / f'{root.id}.json'

    def hold(self, root) -> Path:
        return self.ship.runtime / 'post/group-holds' / f'{root.id}.json'


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def copy_for(copies, name):
    return next(m for m in copies if m.to == name)


def torn(path: Path, text: str = '{torn') -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# --- refusal: the exact child, in the member's own journal ------------------------------------------


def test_the_exact_refused_copy_names_its_member_failed(world):
    _, copies = world.submit()
    world.launch('reviewer-1')
    world.refuse('reviewer-1', copy_for(copies, 'reviewer-1').id)
    assert world.failure('reviewer-1', copy_for(copies, 'reviewer-1')) == 'provider refused this copy'


def test_a_quarantine_from_an_earlier_launch_still_names_its_exact_copy(world):
    # The refused copy is never typed into the seat again, whichever launch refused it.
    _, copies = world.submit()
    world.refuse('reviewer-1', copy_for(copies, 'reviewer-1').id,
                 launch={'pane': '%9', 'pid': 9, 'at': '2026-01-01T00:00:00+00:00'})
    world.launch('reviewer-1')
    assert world.failure('reviewer-1', copy_for(copies, 'reviewer-1')) == 'provider refused this copy'


def test_an_unrelated_refusal_names_no_failure(world):
    _, copies = world.submit()
    other = world.store.new('order', 'operator', 'reviewer-1', 'An unrelated synthetic order.')
    world.launch('reviewer-1')
    world.refuse('reviewer-1', other.id)
    world.refuse('reviewer-1', stage='claimed')  # a claim that quarantined nothing
    assert world.failure('reviewer-1', copy_for(copies, 'reviewer-1')) is None


def test_another_seats_journal_naming_the_copy_is_not_this_members_evidence(world):
    _, copies = world.submit()
    mine = copy_for(copies, 'reviewer-1')
    world.launch('reviewer-1')
    world.refuse('reviewer-2', mine.id)
    world.refuse('lead', mine.id)
    assert world.failure('reviewer-1', mine) is None


def test_the_order_free_observation_never_reads_a_refusal_journal(world):
    _, copies = world.submit()
    world.launch('reviewer-1')
    world.refuse('reviewer-1', copy_for(copies, 'reviewer-1').id)
    assert group_post.current_seat_failure(world.service, 'reviewer-1') is None
    torn(refusal.path(world.ship.runtime, 'reviewer-1'))
    assert group_post.current_seat_failure(world.service, 'reviewer-1') is None


@pytest.mark.parametrize('text', ['{not json', '{"format": 2, "seat": "reviewer-1", "events": []}',
                                  '{"format": 1, "seat": "reviewer-2", "events": []}',
                                  '{"format": 1, "seat": "reviewer-1", "events": [{"stage": "held"}]}'])
def test_an_unreadable_refusal_journal_holds_its_group_and_never_guesses(world, text):
    root, copies = world.submit()
    world.launch('reviewer-1')
    path = refusal.path(world.ship.runtime, 'reviewer-1')
    torn(path, text)
    with pytest.raises(refusal.JournalError):
        world.failure('reviewer-1', copy_for(copies, 'reviewer-1'))
    before = world.journal(root).read_bytes()
    assert world.advance() == [(root.id, 'group evidence is inconsistent')]
    assert world.notices() == [] and world.journal(root).read_bytes() == before
    assert json.loads(before)['notice'] is None and json.loads(before)['failed'] == {}
    assert path.read_text() == text  # the evidence is retained for inspection


# --- the member's own launch and pane ---------------------------------------------------------------


def test_no_saved_launch_is_pending_however_old_the_copy(world):
    _, copies = world.submit()
    old = replace(copy_for(copies, 'reviewer-1'), at=datetime(2000, 1, 1, tzinfo=timezone.utc))
    assert world.failure('reviewer-1', old) is None
    assert world.wall_reads == []


def test_a_live_owned_pane_off_its_wall_is_pending(world):
    _, copies = world.submit()
    world.launch('reviewer-1')
    assert world.failure('reviewer-1', copy_for(copies, 'reviewer-1')) is None
    assert world.wall_reads == [('reviewer-1', 'fake-a')]


def test_a_dead_owned_pane_names_its_member(world):
    _, copies = world.submit()
    world.launch('reviewer-1', dead=True)
    assert world.failure('reviewer-1', copy_for(copies, 'reviewer-1')) == 'owned pane is dead'
    assert group_post.current_seat_failure(world.service, 'reviewer-1') == 'owned pane is dead'


@pytest.mark.parametrize('dead', [False, True])
def test_a_reused_pane_id_supplies_no_failure_and_no_wall_reading(world, dead):
    _, copies = world.submit()
    launched = world.launch('reviewer-1', shown=False)
    world.show(launched.pane, launched.pid + 500, dead=dead)  # another process holds the saved id
    world.walls.update({'fake-a', 'fake-b'})
    assert world.failure('reviewer-1', copy_for(copies, 'reviewer-1')) is None
    assert world.wall_reads == []


def test_an_absent_saved_pane_is_pending_not_failed(world):
    # Absence is also what an ordinary restart or a tmux server restart shows for a moment.
    root, copies = world.submit()
    world.launch('reviewer-1', shown=False)
    assert world.failure('reviewer-1', copy_for(copies, 'reviewer-1')) is None
    assert world.advance() == []
    assert world.notices() == [] and json.loads(world.journal(root).read_text())['failed'] == {}


def test_a_busy_lifecycle_is_pending_even_over_a_dead_pane(world):
    _, copies = world.submit()
    world.launch('reviewer-1', dead=True)
    path = world.ship.runtime / 'locks' / 'lifecycle' / 'reviewer-1.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    held = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)  # another transition owns the seat
        assert world.failure('reviewer-1', copy_for(copies, 'reviewer-1')) is None
    finally:
        os.close(held)
    assert world.failure('reviewer-1', copy_for(copies, 'reviewer-1')) == 'owned pane is dead'


def test_a_launch_replaced_during_the_observation_names_no_failure(world):
    _, copies = world.submit()
    old = world.launch('reviewer-1', dead=True)
    seat = world.seat('reviewer-1')

    def racing(ident):
        pane = world.panes.get(ident)
        if ident == old.pane:  # a relaunch lands while the old dead pane is being read
            Launched('%9999', 9999, post.now().isoformat(), 'fake-a', None, ['fake'], 'synthetic', 'fake').write(
                seat.trusted / 'launch.json')
        return pane

    world.tmux.pane = racing
    assert world.failure('reviewer-1', copy_for(copies, 'reviewer-1')) is None


def test_an_unresolved_selection_is_pending(world):
    _, copies = world.submit()
    world.launch('reviewer-1', dead=True)

    def unresolved(name):
        raise SelectionError(name, f'{name}: selection unknown: synthetic')

    world.service.seat = unresolved
    assert world.failure('reviewer-1', copy_for(copies, 'reviewer-1')) is None


def test_an_unreadable_launch_holds_its_group_without_a_notice(world):
    root, copies = world.submit()
    world.launch('reviewer-1')
    (world.seat('reviewer-1').trusted / 'launch.json').write_text('{torn')
    assert world.advance() == [(root.id, 'group evidence is malformed JSON')]  # names no journal it did not read
    assert world.notices() == [] and json.loads(world.journal(root).read_text())['failed'] == {}


def test_a_member_removed_from_the_declaration_is_named(world):
    _, copies = world.submit()
    seats = {name: spec for name, spec in world.ship.seats.items() if name != 'reviewer-4'}
    world.service.ship = replace(world.ship, seats=seats, groups={})
    assert world.failure('reviewer-4', copy_for(copies, 'reviewer-4')) == 'member no longer declared'
    assert group_post.current_seat_failure(world.service, 'reviewer-4') is None  # no order, no group fact


# --- held at a limit, on the pool the member is on now, with nowhere eligible to go ---------------------


def test_a_wall_with_an_eligible_destination_is_pending(world):
    _, copies = world.submit()
    world.launch('reviewer-1')
    world.walls.add('fake-a')
    assert world.failure('reviewer-1', copy_for(copies, 'reviewer-1')) is None
    assert world.wall_reads == [('reviewer-1', 'fake-a')]


def test_held_at_its_limit_with_nowhere_to_go_names_its_member(world):
    _, copies = world.submit()
    world.launch('reviewer-1')
    world.walls.update({'fake-a', 'fake-b'})
    assert world.failure('reviewer-1', copy_for(copies, 'reviewer-1')) == 'held at limit with no eligible destination'
    assert world.wall_reads == [('reviewer-1', 'fake-a')]


def test_a_wall_on_a_pool_the_member_has_left_names_no_failure(world):
    _, copies = world.submit()
    seat = world.seat('reviewer-1')
    world.launch('reviewer-1', pool='fake-b')
    seat.move_to('fake-b')
    world.walls.add('fake-a')
    assert world.failure('reviewer-1', copy_for(copies, 'reviewer-1')) is None
    assert world.wall_reads == [('reviewer-1', 'fake-b')]


def test_an_unreadable_ledger_names_no_wall_failure(world):
    _, copies = world.submit()
    world.launch('reviewer-1')
    world.walls.update({'fake-a', 'fake-b'})
    book = world.ship.runtime / 'ledger.sqlite'
    book.unlink()
    book.write_bytes(b'not a database')
    assert world.failure('reviewer-1', copy_for(copies, 'reviewer-1')) is None


# --- one terminal notice: all members terminal, outcomes per copy, fixed across cuts and restarts ---------


def test_one_failed_member_while_others_work_publishes_nothing_yet(world):
    root, copies = world.submit()
    for name in MEMBERS[:3]:
        world.launch(name)
    world.launch('reviewer-4', dead=True)
    world.advance()
    assert world.notices() == []
    failed = json.loads(world.journal(root).read_text())['failed']
    assert failed == {copy_for(copies, 'reviewer-4').id: {'member': 'reviewer-4', 'reason': 'owned pane is dead'}}


def test_a_saved_failure_stays_with_its_copy_when_the_evidence_clears_without_a_report(world):
    root, copies = world.submit()
    world.launch('reviewer-4', dead=True)
    world.advance()
    world.launch('reviewer-4')  # relaunched, but files nothing
    reports = world.file(copies[:3])
    world.advance()
    notice, = world.notices()
    assert 'member failed' in notice.body and 'reviewer-4: owned pane is dead' in notice.body
    assert all(r.id in notice.body for r in reports)


def test_a_report_filed_before_the_notice_outranks_a_saved_failure(world):
    root, copies = world.submit()
    world.launch('reviewer-4', dead=True)
    world.advance()
    world.launch('reviewer-4')
    reports = world.file(copies)
    world.advance()
    notice, = world.notices()
    assert 'all filed' in notice.body and 'Failed members:' not in notice.body
    assert all(r.id in notice.body for r in reports)


@pytest.mark.parametrize('forged', [
    lambda copies: {copies[0].id: {'member': 'reviewer-2', 'reason': 'owned pane is dead'}},
    lambda copies: {copies[0].id: {'member': 'reviewer-1', 'reason': 'took too long'}},
    lambda copies: {'ord-unknown': {'member': 'reviewer-1', 'reason': 'owned pane is dead'}},
    lambda copies: {copies[0].id: {'member': 'reviewer-1', 'reason': 'owned pane is dead', 'extra': 1}},
    lambda copies: [copies[0].id],
])
def test_a_forged_member_outcome_holds_and_is_never_announced(world, forged):
    root, copies = world.submit()
    world.file(copies[1:])
    data = json.loads(world.journal(root).read_text())
    data['failed'] = forged(copies)
    world.journal(root).write_text(json.dumps(data))
    before = world.journal(root).read_bytes()
    assert world.advance() == [(root.id, 'group evidence is inconsistent')]
    assert world.notices() == [] and world.journal(root).read_bytes() == before


def test_a_saved_failure_notice_is_published_unchanged_after_the_member_recovers(world, monkeypatch):
    root, copies = world.submit()
    world.file(copies[:3])
    world.launch('reviewer-4', dead=True)
    original = group_post._publish

    def cut(store, message):
        if message.sender == post.SHIP:
            raise RuntimeError('synthetic cut before the notice is published')
        original(store, message)

    monkeypatch.setattr(group_post, '_publish', cut)
    assert world.advance() == [(root.id, 'group coordination could not finish')]
    saved = json.loads(world.journal(root).read_text())['notice']
    assert 'reviewer-4: owned pane is dead' in saved['body']
    monkeypatch.setattr(group_post, '_publish', original)
    world.launch('reviewer-4')  # relaunched, and now files
    world.file(copies[3:], 'late synthetic review')
    assert world.advance() == []
    notice, = world.notices()
    assert (notice.id, notice.at.isoformat(), notice.body) == (saved['id'], saved['at'], saved['body'])
    assert not world.hold(root).exists()


def test_a_restarted_service_publishes_no_second_notice_whatever_the_evidence_now(world):
    root, copies = world.submit()
    world.file([m for m in copies if m.to != 'reviewer-2'])
    world.launch('reviewer-2', dead=True)
    world.advance()
    notice, = world.notices()
    assert 'reviewer-2: owned pane is dead' in notice.body
    service = Service(world.ship, world.tmux)
    try:
        world.launch('reviewer-2')
        world.refuse('reviewer-2', copy_for(copies, 'reviewer-2').id)
        for _ in range(3):
            service.groups()
        world.file([copy_for(copies, 'reviewer-2')], 'after the notice')
        service.groups()
    finally:
        service.ledger.close()
    assert world.notices() == [notice]


# --- isolation: bad evidence holds only its own group, once, and shows on the status line -----------------


def test_a_malformed_journal_holds_only_its_group_and_heals(world):
    bad, bad_copies = world.submit()
    good, good_copies = world.submit()
    world.file(bad_copies)
    reports = world.file(good_copies)
    original = world.journal(bad).read_bytes()
    world.journal(bad).write_bytes(original[:40])
    assert world.advance() == [(bad.id, 'group evidence is malformed JSON')]
    notice, = world.notices(good)
    assert 'all filed' in notice.body and all(r.id in notice.body for r in reports)
    assert world.notices(bad) == [] and world.journal(bad).read_bytes() == original[:40]
    assert group_post.holds(world.ship.runtime) == [f'{post.short_of(bad.id)} group held: group evidence is malformed JSON']
    assert world.advance() == []  # unchanged: quiet on later passes
    world.journal(bad).write_bytes(original)  # the operator restores it
    assert world.advance() == []
    assert group_post.holds(world.ship.runtime) == [] and 'all filed' in world.notices(bad)[0].body


def test_a_journal_edited_inconsistently_is_held_not_repaired(world):
    # The post folder is writable from a jail: an edited journal must neither publish nor be rewritten.
    bad, bad_copies = world.submit()
    good, good_copies = world.submit()
    world.file(good_copies)
    data = json.loads(world.journal(bad).read_text())
    data['copies'][0]['body'] = 'A different brief.'
    world.journal(bad).write_text(json.dumps(data))
    edited = world.journal(bad).read_bytes()
    assert world.advance() == [(bad.id, 'group evidence is inconsistent')]
    assert world.journal(bad).read_bytes() == edited
    assert [m.body for m in world.store.all() if m.re == bad.id] == [BODY] * 4
    assert world.notices(bad) == [] and len(world.notices(good)) == 1


def test_the_service_logs_a_hold_once_and_the_status_names_it(world):
    root, _ = world.submit()
    world.journal(root).write_text('{torn')
    service = Service(world.ship, world.tmux)
    try:
        for _ in range(3):
            service.groups()
    finally:
        service.ledger.close()
    events = [json.loads(line) for line in service.log_path.read_text().splitlines()]
    held = [e for e in events if e['event'] == 'group-held']
    assert held == [{'at': held[0]['at'], 'event': 'group-held', 'request': root.id,
                     'reason': 'group evidence is malformed JSON'}]
    from quarterdeck import headlines, watch
    facts = watch.Facts(service='down', waiting=0, oldest=0, blind=0, group_held=group_post.holds(world.ship.runtime))
    assert f'{post.short_of(root.id)} group held: group evidence is malformed JSON' in facts.line()
    crowded = ['synthetic other alert'] * 12  # past the width: categories are counted instead
    assert '1 groups held' in headlines.status_line(facts, crowded)


# --- separation: inbox and carriage hold a member's own messages; the store is not fenced -----------------


def test_a_reviewer_is_shown_and_carried_only_its_own_copy_after_peers_file(world):
    root, copies = world.submit()
    reports = world.file(copies[:3], 'PEER-FINDING')
    world.launch('reviewer-4', dead=True)  # every member terminal: a notice exists; this copy stays open
    world.advance()
    assert len(world.notices()) == 1
    mine = copy_for(copies, 'reviewer-4')
    # The common request id is the copy's own `re`, so it may be shown; nothing of a peer may be.
    foreign = [*copies[:3], *reports, *world.notices()]
    assert all(m.to != 'reviewer-4' for m in foreign) and root.to == 'lead'
    first, carried = world.seat('reviewer-4').first_message()
    assert [m.id for m in carried] == [mine.id]
    reading = post.inbox_reading(world.store, 'reviewer-4')
    assert [m.id for m in reading['open'] + reading['received']] == [mine.id]
    inbox = post.render_inbox(world.ship.runtime, world.store, 'reviewer-4').read_text()
    for text in (first, inbox):
        assert mine.id in text or mine.short in text
        for message in foreign:
            assert message.id not in text and message.short not in text
        assert 'PEER-FINDING' not in text
    # qd thread as this reviewer: its own copy whole; a peer's message at most named as existing, never its words.
    whole = thread_text(world.store, mine.id, 'reviewer-4')
    assert whole.count(mine.body) == 1 and 'PEER-FINDING' not in whole, 'its own copy once; no peer copy or report'
    for message in foreign:
        assert message.body == mine.body or message.body not in whole  # a peer's copy shares the request's words


def test_a_fresh_lead_is_carried_its_notice_but_never_a_reviewers_copy(world):
    root, copies = world.submit()
    world.file(copies, 'PEER-FINDING')
    world.advance()
    notice, = world.notices()
    carriage = world.seat('lead').carriage()
    carried = [m.id for m in carriage.carried]
    assert carried[:1] == [root.id] and notice.id in carried
    assert all(m.to == 'lead' for m in carriage.carried)
    assert not set(carried) & {m.id for m in copies}
    reading = post.inbox_reading(world.store, 'lead')
    shown = {m.id for m in reading['open'] + reading['received']}
    assert notice.id in shown and not shown & {m.id for m in copies}


def test_a_ship_report_reaches_only_its_own_addressee(world):
    root, copies = world.submit()
    other = world.store.new('order', 'operator', 'lead', 'A separate synthetic order.')
    stray = world.store.new('report', post.SHIP, 'lead', 'Synthetic ship report on another root.', re=other.id)
    to_peer = world.store.new('report', post.SHIP, 'reviewer-1', 'Synthetic ship report to a reviewer.', re=root.id)
    lead = [m.id for m in world.seat('lead').carriage().carried]
    assert stray.id in lead  # it amends its own open root
    assert to_peer.id not in lead
    reviewer = [m.id for m in world.seat('reviewer-2').carriage().carried]
    assert reviewer == [copy_for(copies, 'reviewer-2').id]


def test_a_refused_copy_is_carried_as_a_notice_without_its_body(world):
    _, copies = world.submit()
    mine = copy_for(copies, 'reviewer-2')
    world.refuse('reviewer-2', mine.id)
    first, carried = world.seat('reviewer-2').first_message()
    assert carried == [] and BODY.strip() not in first
    [row] = [row for row in index_rows(first) if mine.id in row['withheld']]
    assert row['notice'] == refusal.UNTOLD  # its sender, the lead, is a seat; refuse()'s journal holds no notice
    whole = thread_text(world.store, mine.id, 'reviewer-2')
    assert BODY.strip() not in whole, 'neither its own refused copy nor a peer copy prints the refused words'
    assert refusal.UNTOLD in whole


def test_a_jailed_reviewer_still_reaches_the_shared_post_folder_but_no_peer_seat_folder(world):
    # The seat reaches the post through its own view of it. Only this seat's drop folder is writable.
    runtime = world.ship.runtime
    folder = runtime / 'seats' / 'reviewer-1'
    drop = runtime / 'post' / 'drop' / 'reviewer-1'
    jail = sandbox.Jail(Path('/home/synthetic'), folder / 'home', folder, world.ship.workspace,
                        world.ship.folder, runtime, (), (drop, runtime / 'check' / 'reviewer-1'),
                        post_view=runtime / 'views' / 'reviewer-1' / 'post')
    args = sandbox.command(jail, ['fake'])
    triples = [tuple(args[i:i + 3]) for i in range(len(args) - 2)]
    assert ('--bind-try', str(drop), str(drop)) in triples
    assert ('--bind-try', str(runtime / 'post'), str(runtime / 'post')) not in triples
    assert ('--ro-bind', str(world.ship.folder), str(world.ship.folder)) in triples
    # The runtime is an empty fence inside: the post is shown read-only, and the seat's own folder is mounted over it.
    assert ('--tmpfs', str(runtime)) in [tuple(args[i:i + 2]) for i in range(len(args) - 1)]
    # Only the seat's own view of the post, in the post's place: never the post whole.
    assert ('--ro-bind', str(runtime / 'views' / 'reviewer-1' / 'post'), str(runtime / 'post')) in triples
    assert ('--ro-bind-try', str(runtime / 'post'), str(runtime / 'post')) not in triples
    assert ('--bind', str(folder), str(folder)) in triples
    assert not any(str(runtime / 'seats' / peer) in args for peer in ('lead', *MEMBERS[1:]))
    assert args.index(str(runtime)) < args.index(str(folder))  # own folder mounted over the fence


def test_the_template_jails_its_endpoint_reviewer_outside_the_ship_folder(tmp_path, monkeypatch):
    folder = tmp_path / 'forum'
    folder.mkdir()
    (folder / 'ship.toml').write_text((ROOT / 'ships/forum/ship.toml').read_text())
    ship = shipmod.load(folder)
    spec = ship.seat('reviewer-4')
    assert ship.jailed(spec) and ship.cwd_of(spec) != ship.folder and ship.folder in ship.cwd_of(spec).parents
    assert (ROOT / 'ships/forum' / ship.cwd_of(spec).relative_to(ship.folder)).is_dir()  # tracked in the template
    monkeypatch.setattr(sandbox, 'available', lambda: True)
    home = tmp_path / 'operator-home'
    home.mkdir()
    jail = sandbox.plan(ship, 'reviewer-4', ship.runtime / 'seats/reviewer-4', ship.runtime / 'seats/reviewer-4/home',
                        ship.cwd_of(spec), 'pi', str(tmp_path / 'no-bin'), home)
    assert jail.workspace == ship.cwd_of(spec).resolve()


def test_the_cards_and_design_claim_no_fence_and_no_universal_read_detection():
    design = ' '.join((ROOT / 'docs/design/structures.md').read_text().split())
    reviewer = ' '.join((ROOT / 'ships/forum/roles/reviewer.md').read_text().split())
    lead = ' '.join((ROOT / 'ships/forum/roles/lead.md').read_text().split())
    assert 'remain readable and writable; independence is instruction-only, not a filesystem fence' in design
    assert 'cannot reliably prove that every possible read was observed' in design
    assert 'technically readable and writable' in reviewer
    assert 'readable and writable; independence is instruction-only' in lead
