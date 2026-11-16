"""The post narrowed seat by seat: each seat's attribution scope (Threads.scope, the engine's own) says which seats a
change of the post reaches. Synthetic seats, messages, records and ledger only."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from quarterdeck import post
from quarterdeck.accounting_gate import Reading
from quarterdeck.accounting_scope import Partitions, post_of
from quarterdeck.accounting_slices import post_scopes
from quarterdeck.order_attribution import Threads
from .test_accounting_scoped_jobs import change, scopes_of, world  # noqa: F401

AT = datetime(2032, 1, 2, 12, 0, tzinfo=timezone.utc)
ISSUER, WORKER, REVIEWER, BYSTANDER = 'ordinary-issuer', 'ordinary-worker', 'ordinary-reviewer', 'ordinary-bystander'
SEATS = (ISSUER, WORKER, REVIEWER, BYSTANDER)
ALPHA, BETA = 'ordinary-alpha', 'ordinary-beta'


def sample():
    """Orders, a sub-order, questions and answers, and reports that close orders: a thread the closure follows."""
    at = lambda second: AT + timedelta(seconds=second)  # noqa: E731
    return [post.Message('ord-20320102-1200-x0001', 'order', ISSUER, WORKER, at(0), 'Ordinary work.'),
            post.Message('que-20320102-1200-x0002', 'question', WORKER, ISSUER, at(1), 'A question.',
                         'ord-20320102-1200-x0001'),
            post.Message('ans-20320102-1200-x0003', 'answer', ISSUER, WORKER, at(2), 'An answer.',
                         'que-20320102-1200-x0002'),
            post.Message('ord-20320102-1200-x0004', 'order', WORKER, REVIEWER, at(3), 'A sub-order.',
                         'ord-20320102-1200-x0001'),
            post.Message('rep-20320102-1200-x0005', 'report', REVIEWER, WORKER, at(4), 'Sub-order done.',
                         'ord-20320102-1200-x0004'),
            post.Message('rep-20320102-1200-x0006', 'report', WORKER, ISSUER, at(5), 'Work done.',
                         'ord-20320102-1200-x0001'),
            post.Message('ord-20320102-1200-x0007', 'order', ISSUER, BYSTANDER, at(6), 'Other work.')]


def reading():
    return SimpleNamespace(classes=(), inputs=((),), young=())


def test_the_gates_closure_and_threads_scope_agree_on_a_sample_store(tmp_path):
    """As each message of a sample store is added, the seats the gate dirties are exactly the seats whose
    Threads.scope moved: the gate's closure is the engine's own. The unbounded end is a superset of every finite end
    a turn's attribution reads: a seat whose scope moves at any message's time moves at the unbounded end too."""
    runtime = tmp_path / 'runtime'
    graph = Threads(post.Store(runtime))
    partitions = Partitions()
    partitions.seats = set(SEATS)
    previous = post_scopes(graph, SEATS)
    partitions.certified = dict(previous)
    beyond = []
    for message in sample():
        post.Store(runtime).write(message)
        current = Threads(post.Store(runtime))
        now = post_scopes(current, SEATS)
        moved = {seat for seat in SEATS if now[seat] != previous[seat]}
        assert partitions.record_scopes(reading(), now, set(), runtime)
        assert partitions.dirty_seats == moved, message.id
        assert {message.sender, message.to} <= moved, 'its sender and recipient at least'
        for seat in SEATS:
            for ended in (m.at.isoformat() for m in sample()):
                if current.scope(seat, ended) != graph.scope(seat, ended):
                    assert seat in moved, (message.id, seat, 'a finite end moved where the unbounded did not')
        beyond.append(moved - {message.sender, message.to})
        graph, previous = current, now
        partitions.certified = dict(now)
    assert any(REVIEWER in seats for seats in beyond), 'a closing report reaches beyond its sender and recipient'


def test_a_post_change_alone_reads_no_seat_and_leaves_the_gate_quiet_when_no_scope_moved(world):
    e = world
    (e.runtime / 'post' / 'store' / 'ordinary-message.json').write_text('{}\n')
    assert e.gate.allowed(e.service) and e.gate.scope == set() and e.gate.decision.get('post-scopes') == 1
    assert e.begin(set())
    e.finish(set())
    assert not e.gate.allowed(e.service), 'every scope as it was: nothing to read'


def test_a_seat_whose_scope_moved_is_read_alone_and_then_certified(world):
    e = world
    (e.runtime / 'post' / 'store' / 'ordinary-message.json').write_text('{}\n')
    assert e.gate.allowed(e.service) and e.gate.scope == set()
    assert e.begin(set())
    moved = dict(scopes_of(e.gate), **{ALPHA: 'moved-scope'})
    e.gate.finish(e.service, e.db.total_changes, full=False, scopes=moved)
    assert e.gate.partitions.dirty_seats == {ALPHA}
    assert e.gate.allowed(e.service) and e.gate.scope == {ALPHA}, 'only the seat the post reached'
    assert e.begin({ALPHA})
    e.gate.finish(e.service, e.db.total_changes, full=False, scopes=moved)
    assert e.gate.partitions.certified[ALPHA] == 'moved-scope' and not e.gate.partitions.dirty_seats
    assert not e.gate.allowed(e.service)


def test_a_removed_message_file_forces_a_full_job(world):
    e = world
    message = e.runtime / 'post' / 'store' / 'ordinary-message.json'
    message.write_text('{}\n')
    assert e.gate.allowed(e.service) and e.gate.scope == set()
    assert e.begin(set())
    e.finish(set())
    message.unlink()
    assert e.gate.allowed(e.service) and e.gate.scope == set()
    assert e.begin(set())
    e.finish(set())
    assert e.gate.partitions.baseline is None, 'a message that left breaks the post: nothing certified'
    assert e.gate.allowed(e.service) and e.gate.scope is None


def test_a_young_post_folder_keeps_the_post_due(tmp_path):
    """A post folder young in the reading a job's scopes were compared at may still be taking a message within the
    filesystem's tick: that reading stands for no post, and the next choice has the post due again."""
    store = str(tmp_path / 'post' / 'store')
    def reading(young):
        return Reading(((( store, (1, 2, 3)),), (), 'synthetic ship', {'fake': ['reader']}, 'synthetic epoch'),
                       (1, 0, 0, 0, 0), 0, ((store, 'post-folder'),), (store,) if young else (), 0)
    partitions = Partitions()
    partitions.seats, partitions.certified = {ALPHA}, {ALPHA: 'synthetic-scope'}
    partitions.baseline, partitions.due = reading(False), float('inf')
    assert post_of(reading(True)) is None and post_of(reading(False)) == ((store, (1, 2, 3)),)
    assert partitions.record_scopes(reading(True), {ALPHA: 'synthetic-scope'}, set(), tmp_path)
    assert partitions.choose(reading(False), 0) == set() and partitions.post_due, 'young: taken again'
    assert partitions.record_scopes(reading(False), {ALPHA: 'synthetic-scope'}, set(), tmp_path)
    assert partitions.choose(reading(False), 0) == set() and not partitions.post_due, 'settled: covered'
