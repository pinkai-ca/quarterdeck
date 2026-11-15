"""Notice settlement is derived history, without changing messages or read receipts."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import time
from types import SimpleNamespace

import pytest

from quarterdeck import carriage_plan, post, post_notices, post_threads, ship as shipmod
from quarterdeck.seat import Seat
from quarterdeck.service import Service


AT = datetime(2030, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    (tmp_path / 'ship.toml').write_text('''[ship]
name = "synthetic"
[pools.example]
provider = "openai"
[sessions.crew]
[seats.reader]
session = "crew"
role = "worker"
harness = "codex"
pool = "example"
''')
    return post.Store(tmp_path / 'runtime')


def notice(store, n=0, *, body=None, **fields):
    message = post.Message(fields.get('kind', 'report')[:3] + '-synthetic-' + f'{n:06x}', 'report', 'ship', 'reader',
                           AT + timedelta(seconds=n), body or "ship's orders changed · read runtime/brief/reader.md")
    message = replace(message, **fields)
    store.write(message)
    return message


def launch(store, at):
    ship = shipmod.load(store.dir.parent.parent.parent)
    seat = Seat(ship, ship.seat('reader'), None)
    path = getattr(seat, 'trusted', seat.folder) / 'launch.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(pane='%1', pid=1, at=at.isoformat(), pool='example',
                                    resume=None, argv=['example'], version=None)))
    return path


def reader(store):
    seat = object.__new__(Seat)
    seat.ship = SimpleNamespace(runtime=store.dir.parent.parent, carry_bytes=100000)
    seat.spec = SimpleNamespace(name='reader', role='worker')
    seat.folder = seat.ship.runtime / 'seats' / 'reader'
    seat._queued_unread = lambda store, attempts: []
    return seat


def test_three_notices_then_launch_are_history_not_waiting_or_carried(store, tmp_path):
    messages = [notice(store, n) for n in range(3)]
    before = {m.id: (store.dir / (m.id + '.json')).read_bytes() for m in messages}
    launch(store, AT + timedelta(seconds=4))
    assert not post.Attempts(store.dir.parent.parent).undelivered(store, 'reader')
    carriage = reader(store).carriage()
    assert not carriage.carried and not carriage.rows
    assert '3 changed orders: settled by a later launch' in carriage.history
    plan = carriage_plan.build(carriage.rows, handoff='', header=carriage.header,
                               history=carriage.history, directory=tmp_path / 'index', limit=100000)
    assert carriage.history in plan.opening
    assert all(m.id not in plan.opening and m.body not in plan.opening for m in messages)
    assert not store.read_ids('reader')
    assert before == {m.id: (store.dir / (m.id + '.json')).read_bytes() for m in messages}


def test_only_newest_of_two_notices_waits_without_a_launch(store):
    old, new = notice(store), notice(store, 1, body='brief changed · read runtime/brief/reader.md')
    assert post.Attempts(store.dir.parent.parent).undelivered(store, 'reader') == [new]
    carriage = reader(store).carriage()
    assert carriage.carried == [new]
    assert [row.active for row in carriage.rows] == [(new.id,)]
    assert old.id not in str(carriage.rows)
    assert '1 changed orders: settled by a newer notice' in carriage.history


@pytest.mark.parametrize('fields', [
    {'body': "ship's orders changed · read runtime/brief/reader.md\n"},
    {'body': "ship's orders changed · read runtime/brief/someone.md"},
    {'body': 'brief changed · read runtime/brief/reader.md and act'},
    {'sender': 'someone'}, {'kind': 'order'}, {'re': 'unrelated'},
])
def test_near_forms_and_replies_still_wait(store, fields):
    message = notice(store, **fields)
    launch(store, AT + timedelta(seconds=2))
    assert message.id not in store.settled_notices('reader')
    assert post.Attempts(store.dir.parent.parent).undelivered(store, 'reader') == [message]


@pytest.mark.parametrize('offset', [-1, 0, 1])
def test_only_strictly_later_launch_settles(store, offset):
    message = notice(store)
    launch(store, AT + timedelta(seconds=offset))
    assert (message.id in store.settled_notices('reader')) == (offset > 0)


def test_same_timestamp_notices_have_one_deterministic_newest(store):
    first = notice(store)
    second = notice(store, 1, at=AT)
    assert post.Attempts(store.dir.parent.parent).undelivered(store, 'reader') == [second]
    assert first.id in store.settled_notices('reader')


@pytest.mark.parametrize('bad', ['missing', 'invalid', 'naive'])
def test_unknown_launch_does_not_certify_a_notice(store, bad):
    message = notice(store)
    if bad != 'missing':
        path = launch(store, AT + timedelta(seconds=1))
        path.write_text('{}' if bad == 'invalid' else path.read_text().replace('+00:00', ''))
    assert message.id not in store.settled_notices('reader')


def test_provisional_launch_plans_history_without_persisting_it(store):
    message = notice(store)
    seat = reader(store)
    carriage = seat.carriage(brief_at=AT + timedelta(seconds=1))
    assert not carriage.carried and not carriage.rows
    assert '1 changed orders: settled by a later launch' in carriage.history
    # A failed startup leaves the old/no launch authoritative.
    assert post.Attempts(store.dir.parent.parent).undelivered(store, 'reader') == [message]
    launch(store, AT + timedelta(seconds=2))
    assert not post.Attempts(store.dir.parent.parent).undelivered(store, 'reader')


def test_launch_change_is_seen_without_post_or_store_changes(store):
    message = notice(store)
    assert not store.settled_notices('reader')
    launch(store, AT + timedelta(seconds=1))
    assert message.id in store.settled_notices('reader')
    launch(store, AT - timedelta(seconds=1))
    assert not store.settled_notices('reader')


def test_launch_time_comes_from_the_seat_api(store, monkeypatch):
    message = notice(store)
    calls = []

    def launched(seat):
        calls.append((seat.name, seat.ship.runtime))
        return SimpleNamespace(when=AT + timedelta(seconds=1))

    monkeypatch.setattr(Seat, 'launched', launched)
    assert message.id in store.settled_notices('reader')
    assert calls == [('reader', store.dir.parent.parent)]


def cached_pass(store):
    attempts = post.Attempts(store.dir.parent.parent)
    with post.reader_scope(store=store, attempts=attempts):
        with post.reading_pass(store=store, attempts=attempts):
            return attempts.undelivered(store, 'reader')


def count_launch_reads(monkeypatch):
    calls = []
    original = Seat.launched

    def launched(seat):
        calls.append(seat.name)
        return original(seat)

    monkeypatch.setattr(Seat, 'launched', launched)
    return calls


def age_observations(monkeypatch):
    # Advance the reader's clock past the real metadata tick; no timestamp is forged.
    future = time.time_ns() + post.POST_TIMESTAMP_TICK_NS + 1_000_000_000
    monkeypatch.setattr(time, 'time_ns', lambda: future)


def test_unchanged_deliveries_pass_reads_no_launch(store, monkeypatch):
    message = notice(store)
    launch(store, AT - timedelta(seconds=1))
    age_observations(monkeypatch)
    calls = count_launch_reads(monkeypatch)
    assert cached_pass(store) == [message]
    assert calls == ['reader']
    calls.clear()
    assert cached_pass(store) == [message]
    assert calls == []


def test_changed_launch_is_seen_in_the_next_deliveries_pass(store, monkeypatch):
    message = notice(store)
    launch(store, AT - timedelta(seconds=1))
    age_observations(monkeypatch)
    calls = count_launch_reads(monkeypatch)
    assert cached_pass(store) == [message]
    launch(store, AT + timedelta(seconds=1))
    calls.clear()
    assert not cached_pass(store)
    assert calls == ['reader']


def test_first_launch_is_seen_after_a_cached_absence(store, monkeypatch):
    message = notice(store)
    age_observations(monkeypatch)
    assert cached_pass(store) == [message]
    launch(store, AT + timedelta(seconds=1))
    assert not cached_pass(store)


def test_declaration_change_invalidates_the_cached_seat(store, monkeypatch):
    notice(store)
    launch(store, AT + timedelta(seconds=1))
    age_observations(monkeypatch)
    calls = count_launch_reads(monkeypatch)
    assert not cached_pass(store)
    declaration = store.dir.parent.parent.parent / 'ship.toml'
    declaration.write_text(declaration.read_text().replace('role = "worker"', 'role = "reader"'))
    calls.clear()
    assert not cached_pass(store)
    assert calls == ['reader']


def test_young_launch_does_not_seed_a_reusable_observation(store, monkeypatch):
    message = notice(store)
    path = launch(store, AT - timedelta(seconds=1))
    young = path.stat().st_ctime_ns
    monkeypatch.setattr(time, 'time_ns', lambda: young)
    calls = count_launch_reads(monkeypatch)
    assert cached_pass(store) == [message]
    assert cached_pass(store) == [message]
    assert calls == ['reader', 'reader']


def test_ordinary_waiting_messages_need_no_notice_observation(store, monkeypatch):
    report = notice(store, sender='origin', body='ordinary report')
    order = notice(store, 1, kind='order', sender='origin')
    monkeypatch.setattr(post.Store, 'settled_notices',
                        lambda *a, **k: pytest.fail('ordinary delivery needs no notice lookup'))
    assert cached_pass(store) == [report, order]


def test_thread_and_inbox_label_received_history_and_keep_complete_body(store):
    message = notice(store)
    launch(store, AT + timedelta(seconds=1))
    inbox = post.inbox_reading(store, 'reader')
    assert inbox['open'] == [] and inbox['received'] == [message]
    assert inbox['settled'][message.id] == 'changed orders: settled by a later launch'
    rendered = post.render_inbox_reading(inbox)
    assert rendered.index('## Received') < rendered.index('settled by a later launch')
    assert message.body in rendered
    thread = post_threads.whole(store, message.id, 'reader')
    text = post_threads.render(thread)
    assert 'settled by a later launch · received history' in text
    assert message.body in text
    assert not store.read_ids('reader')


def test_settled_notice_is_not_an_active_index_root(store):
    message = notice(store)
    launch(store, AT + timedelta(seconds=1))
    assert not post_threads.index_rows(store, (), (message,), frozenset(), seat='reader')


def test_thread_metadata_does_not_carry_unrelated_settled_history(store):
    settled = notice(store)
    unrelated = notice(store, 1, body='unrelated ordinary report')
    launch(store, AT + timedelta(seconds=2))
    assert post_threads.whole(store, unrelated.id, 'reader').settled == ()
    assert settled.id not in post_threads.render(post_threads.whole(store, unrelated.id, 'reader'))


def test_recent_inbox_does_not_copy_settlement_metadata_outside_its_tail(store, monkeypatch):
    notice(store)
    launch(store, AT + timedelta(seconds=2))
    monkeypatch.setattr(post, 'now', lambda: AT + timedelta(days=2))
    data = post.inbox_reading(store, 'reader', recent=True)
    assert not data['received'] and not data['settled']


def test_direct_door_delivery_does_not_open_a_settled_notice(store, monkeypatch):
    message = notice(store)
    launch(store, AT + timedelta(seconds=1))
    import quarterdeck.service as service
    monkeypatch.setattr(service, 'Door', lambda *a: pytest.fail('no door for settled notice'))
    subject = SimpleNamespace(store=store)
    assert 'settled notice; not delivered' in Service._deliver_current(subject, reader(store), message)


def test_direct_watch_does_not_offer_a_settled_notice(store, monkeypatch):
    notice(store)
    launch(store, AT + timedelta(seconds=1))
    monkeypatch.setattr(post.Attempts, 'open', lambda *a, **k: pytest.fail('no attempt for settled notice'))
    assert post.wait_batch(store.dir.parent.parent, 'reader', stop_after=0) is None


@pytest.mark.parametrize('ending', ['at its wall', 'by qd set'])
def test_pool_move_has_the_same_provable_supersession(store, ending):
    old = notice(store, body=f'moved to pool next from prior {ending}')
    new = notice(store, 1, body='moved to pool later from next by qd set')
    assert store.settled_notices('reader') == {old.id: 'pool move: settled by a newer notice'}
    assert post.Attempts(store.dir.parent.parent).undelivered(store, 'reader') == [new]
    launch(store, AT + timedelta(seconds=2))
    assert len(store.settled_notices('reader')) == 2


def test_unproven_other_reports_and_open_orders_are_not_settled_by_launch(store):
    report = notice(store, body='an observation still needing attention')
    order = notice(store, 1, kind='order')
    launch(store, AT + timedelta(seconds=2))
    assert not store.settled_notices('reader')
    assert store.is_open(order)
    assert post.Attempts(store.dir.parent.parent).undelivered(store, 'reader') == [report, order]


def test_saved_terminal_group_notice_settles_only_when_its_work_closes(store):
    outer = notice(store, kind='order', sender='origin', body='outer task')
    root = notice(store, 1, kind='order', sender='origin', re=outer.id, body='invented task')
    child = notice(store, 2, kind='order', sender='reader', to='peer', re=root.id, body='invented subtask')
    terminal = notice(store, 3, re=root.id, body='invented completion')
    path = store.dir.parent / 'groups' / (root.id + '.json')
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(dict(format=1, lead='reader', group='example', root=json.loads(root.to_json()),
                                    notice=json.loads(terminal.to_json()), copies=[json.loads(child.to_json())])))
    assert terminal.id not in store.settled_notices('reader')
    notice(store, 4, sender='reader', to='origin', re=root.id, body='reported')
    assert store.settled_notices('reader')[terminal.id] == 'terminal notice: settled by closure of its work'
    assert not store.is_open(root)
    rows = post_threads.index_rows(store, (outer,), (terminal,), frozenset(), seat='reader')
    assert len(rows) == 1 and rows[0].active == (outer.id,)
    assert rows[0].settled == (terminal.id + ': terminal notice: settled by closure of its work',)
