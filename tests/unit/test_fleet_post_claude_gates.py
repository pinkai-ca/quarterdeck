"""Claude gates for the post across ships, on two synthetic ships in a throwaway project.

Independent of the transport's own tests: each gate states what must stay true when a message crosses to another
ship. No real ship, seat or provider is touched.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path

import pytest

from quarterdeck import compaction, fleet_post as fleet, post, post_threads, refusal, seat as seatmod, ship as shipmod, thread_context
from tests.unit.test_fleet_post import pair  # noqa: F401  (two synthetic ships, alpha and beta, each with captain and writer)


def everything(folder: Path) -> dict[str, tuple]:
    """Every entry under a ship folder, runtime included, by what a write would change."""
    found = {}
    for root, folders, files in os.walk(folder):
        for name in folders + files:
            path = Path(root) / name
            info = path.lstat()
            found[str(path.relative_to(folder))] = (info.st_mode, info.st_ino, info.st_size, info.st_mtime_ns,
                                                    info.st_ctime_ns)
    return found


def round_trip(left, right, source, destination):
    order = source.new('order', 'writer', 'captain@beta', 'Synthetic gate order body.')
    fleet.run(left, source)
    fleet.run(right, destination)
    return order, destination.get(order.id)


# -- the import takes recipient admission and the compaction hooks before it becomes eligible ---------------------

def test_an_import_is_admitted_for_its_recipient_and_protected_before_it_is_eligible(pair, monkeypatch):
    left, right, source, destination = pair
    order = source.new('order', 'writer', 'captain@beta', 'Admit me before I am eligible.')
    fleet.run(left, source)
    held: list[tuple[Path, str]] = []
    real = post.admission

    @contextmanager
    def admission(runtime, name):
        with real(runtime, name):
            held.append((Path(runtime), name))
            try:
                yield
            finally:
                held.remove((Path(runtime), name))
    monkeypatch.setattr(post, 'admission', admission)
    seen = []
    stored = right.runtime / 'post' / 'store' / (order.id + '.json')

    def hook(step):
        def record(runtime, message):
            if message.id == order.id:
                seen.append(dict(step=step, to=message.to, admitted=(Path(runtime), 'captain') in held,
                                 stored=stored.exists(),
                                 eligible=order.id in {m.id for m in post.Store(right.runtime).for_seat('captain')}))
        return record
    monkeypatch.setattr(compaction, 'protect_closure', hook('protect_closure'))
    monkeypatch.setattr(compaction, 'admitting', hook('admitting'))

    assert fleet.run(right, destination) == []
    assert [s['step'] for s in seen] == ['protect_closure', 'admitting'], seen
    assert all(s['to'] == 'captain' and s['admitted'] and not s['stored'] and not s['eligible'] for s in seen), seen
    assert order.id in {m.id for m in post.Store(right.runtime).for_seat('captain')}, 'eligible once imported'
    seen.clear()
    fleet.run(right, destination)
    assert seen == [], 'an import already made is not admitted twice'


# -- another ship's seat with the same bare name gains no access --------------------------------------------------

def test_the_same_bare_name_on_the_other_ship_gains_no_access_by_inbox_thread_or_carriage(pair):
    left, right, source, destination = pair
    order, received = round_trip(left, right, source, destination)
    body = 'Synthetic gate order body.'

    # On beta, its own writer shares the sender's bare name: no inbox, no open work, no words, no carriage.
    assert received.sender == 'writer@alpha' and received.to == 'captain'
    assert order.id not in {m.id for m in destination.for_seat('writer')}
    assert destination.open_for('writer') == []
    assert body not in post_threads.render(post_threads.whole(destination, order.id, 'writer'))
    assert order.id not in {m.id for m in thread_context.select(destination, 'writer').incoming}
    assert order.id not in {m.id for m in post.Attempts(right.runtime).undelivered(destination, 'writer')}
    # On alpha, its own captain shares the addressee's bare name: the same.
    assert order.id not in {m.id for m in source.for_seat('captain')}
    assert source.open_for('captain') == []
    assert body not in post_threads.render(post_threads.whole(source, order.id, 'captain'))
    assert order.id not in {m.id for m in thread_context.select(source, 'captain').incoming}
    assert order.id not in {m.id for m in post.Attempts(left.runtime).undelivered(source, 'captain')}
    # The parties themselves do read it: the gate is not passing because nobody can.
    assert body in post_threads.render(post_threads.whole(destination, order.id, 'captain'))
    assert body in post_threads.render(post_threads.whole(source, order.id, 'writer'))

    # beta's writer reports on alpha's order: the order's addressee is captain@beta, so it stays open.
    destination.new('report', 'writer', 'writer@alpha', 'Synthetic impostor report.', re=received.id)
    fleet.run(right, destination)
    fleet.run(left, source)
    assert source.is_open(source.get(order.id)), 'a report by a same-named seat of another ship never closes'
    # and alpha's own captain cannot close it either.
    source.new('report', 'captain', 'writer', 'Synthetic local impostor.', re=order.id)
    assert source.is_open(source.get(order.id))
    destination.new('report', 'captain', 'writer@alpha', 'Synthetic real report.', re=received.id)
    fleet.run(right, destination)
    fleet.run(left, source)
    assert not source.is_open(source.get(order.id)), 'its own addressee closes it'


# -- a refused message stays refused across ships: its notice only, no body -----------------------------------------

def quarantine(runtime: Path, seat: str, message: post.Message, notice_id: str) -> None:
    refusal.save(runtime, seat, [dict(stage='held', launch={}, turn=None, code='refusal', reason='synthetic gate',
                                      quarantined=[dict(message=message.id, sender=message.sender, kind=message.kind)],
                                      notices=[dict(message=message.id, id=notice_id,
                                                    at=datetime.now(timezone.utc).isoformat())])])


def test_an_imported_message_refused_by_its_addressee_is_never_offered_again_and_its_sender_is_told_without_it(pair):
    left, right, source, destination = pair
    body = 'Synthetic body the provider refused.'
    order = source.new('order', 'writer', 'captain@beta', body)
    fleet.run(left, source)
    fleet.run(right, destination)
    received = destination.get(order.id)
    notice_id = 'rep-20400101-0000-ship-gate'
    quarantine(right.runtime, 'captain', received, notice_id)
    event = refusal.load(right.runtime, 'captain')[0]
    entry = dict(message=received.id, sender=received.sender, kind=received.kind)
    told = refusal.notice(event, entry, 'captain')
    refusal._publish(destination, told)  # as the refusal step publishes it

    # On beta: its inbox shows the notice, never the words, and a fresh start carries the same.
    inbox = post.inbox_text(destination, 'captain')
    assert received.short in inbox and refusal.WITHHELD in inbox and body not in inbox
    captain = seatmod.Seat(right, right.seat('captain'), None)
    carried = captain.carriage()
    text = '\n'.join(filter(None, [carried.text or '', carried.header, *map(str, carried.rows)]))
    assert body not in text
    assert all(m.id != received.id or m.body != body for m in carried.carried)

    # On alpha: the sender is told by a notice that carries no body, and the order stays open.
    fleet.run(right, destination)
    fleet.run(left, source)
    notice = source.get(notice_id)
    assert notice.re == order.id and body not in notice.body
    assert notice.sender == 'ship@beta' and notice.to == 'writer'
    assert source.is_open(source.get(order.id)), 'a refusal notice never closes the order'


def test_a_message_refused_on_its_own_ship_is_not_carried_to_another_ship_by_a_reply(pair):
    """A descendant crossing to another ship carries its ancestry as context. A refused ancestor goes as its notice
    only: its words never reach the other ship's store or anything rendered there."""
    left, right, source, destination = pair
    body = 'Synthetic body refused on alpha.'
    refused = source.new('order', 'captain', 'writer', body)
    quarantine(left.runtime, 'writer', refused, 'rep-20400101-0001-ship-gate')
    source.new('question', 'writer', 'captain@beta', 'About the order I could not read: what was it?', re=refused.id)
    fleet.run(left, source)
    fleet.run(right, destination)
    carried = [path for path in (right.runtime).rglob('*') if path.is_file() and body.encode() in path.read_bytes()]
    assert carried == [], f"the refused message's words reached the other ship: {carried}"


# -- the receiving service writes only its own runtime, and the sender only reads -----------------------------------

def test_each_service_writes_only_its_own_ship_and_reads_the_other(pair):
    left, right, source, destination = pair
    order = source.new('order', 'writer', 'captain@beta', 'Synthetic gate order body.')

    before = everything(right.folder)
    fleet.run(left, source)
    fleet.status(left, source)
    assert everything(right.folder) == before, "the sender's pass wrote nothing of the receiving ship"

    before = everything(left.folder)
    fleet.run(right, destination)
    fleet.status(right, destination)
    assert everything(left.folder) == before, "the receiver's fetch only read the sending ship"
    assert (right.runtime / 'post' / 'store' / (order.id + '.json')).exists()

    before = everything(right.folder)
    assert fleet.waiting(left, source) == []
    assert everything(right.folder) == before, "the sender confirms the fetch by reading only"

    report = destination.new('report', 'captain', 'writer@alpha', 'Done.', re=order.id)
    before = everything(left.folder)
    fleet.run(right, destination)
    assert everything(left.folder) == before
    before = everything(right.folder)
    fleet.run(left, source)
    assert everything(right.folder) == before
    assert source.closing_report(source.get(order.id)).id == report.id


# -- only its author offers a message as its own ----------------------------------------------------------------------

def test_a_third_ship_cannot_offer_another_ships_message_as_that_ships_even_without_a_route(pair):
    left, right, source, destination = pair
    folder = left.folder.parent / 'gamma'
    folder.mkdir()
    (folder / 'ship.toml').write_text(left.file.read_text())
    gamma = shipmod.load(folder)
    order = source.new('order', 'writer', 'captain@beta', 'Synthetic order only alpha may offer.')
    data = (left.runtime / 'post' / 'store' / f'{order.id}.json').read_bytes()
    (gamma.runtime / 'post' / 'store').mkdir(parents=True)
    (gamma.runtime / 'post' / 'store' / f'{order.id}.json').write_bytes(data)
    value = dict(format=1, source='gamma', target='beta', message=order.id,
                 files=[dict(id=order.id, origin='alpha', size=len(data), sha256=fleet.digest(data))])
    fleet.publish(gamma.runtime / 'post' / 'outbound' / 'beta' / f'{order.id}.json', fleet.encoded(value))
    fleet.run(right, destination)
    assert not (right.runtime / 'post' / 'store' / f'{order.id}.json').exists(), \
        "a third ship's offer was imported as alpha's own"
