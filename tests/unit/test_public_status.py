"""The dashboard reads only the service's minimum publication."""

from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from quarterdeck import cli, post, presentation, service, servicectl, ship as shipmod, status_snapshot, watch

POLICY = 'office_seats = ["CoUnSeL"]\nprivate_ships = ["pRiVaTe-rEvIeW"]\n'
TEXT = 'Synthetic private message text'
SUBJECT = 'Synthetic private subject'
ACTIVITY_DETAIL = 'Synthetic private activity detail'
PROGRAM_ERROR = 'Synthetic private program error'
PROGRAMS = [
    {'harness': 'fake', 'working_now': False, 'last_turn_ended': '2026-11-27T20:01:00+00:00'},
    {'harness': 'claude', 'working_now': None, 'last_turn_ended': None},
    {'harness': 'codex', 'working_now': True, 'last_turn_ended': None},
]
NAMES = ('desk', 'counsel', 'crew', 'unassigned', 'local-private', 'remote-private')


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    policy = tmp_path / 'post-privacy.toml'
    policy.write_text(POLICY)
    monkeypatch.delenv('QD_SHIP', raising=False)
    monkeypatch.delenv('QD_SEAT', raising=False)
    monkeypatch.setattr(watch.program_use, 'collect', lambda *args: (PROGRAMS, None))
    monkeypatch.setattr(watch, 'activity', lambda seat: watch.Activity('off', ACTIVITY_DETAIL))
    monkeypatch.setattr(cli, 'handoff', lambda *args: None)
    with ExitStack() as stack:
        def make(folder_name, declared_name, *, security=None):
            folder = tmp_path / 'ships' / folder_name
            folder.mkdir(parents=True)
            declaration = f'''[ship]
name = "{declared_name}"
code = "checkout"
[pools.fake]
provider = "synthetic"
[sessions.crew]
title = "{declared_name} crew"
'''
            for name in (*NAMES, *((security,) if security else ())):
                role = 'security' if name == security else 'worker'
                declaration += f'''[seats.{name}]
session = "crew"
role = "{role}"
harness = "fake"
pool = "fake"
up = false
'''
            (folder / 'ship.toml').write_text(declaration)
            ship = shipmod.load(folder)
            ship.runtime.mkdir()
            lock = stack.enter_context(servicectl.acquire_lock(ship.runtime))
            actor = service.Service(ship, SimpleNamespace())
            stack.callback(actor.ledger.close)
            orders = {name: actor.store.new('order', sender, name, TEXT, subject=SUBJECT)
                      for name, sender in [('desk', 'pike'), ('counsel', 'desk'), ('crew', 'pike'),
                                           ('local-private', 'counsel')]}
            # An imported order keeps its source's fleet identity in its canonical ID.
            imported = actor.store.prepare('order', 'lead', 'remote-private', TEXT, subject=SUBJECT)
            remote = 'lead@Private-Review'
            orders['remote-private'] = replace(imported, id=imported.id.replace('-lead-', f'-{remote}-'),
                                               sender=remote)
            actor.store.write(orders['remote-private'])
            actor.first_beat()
            assert actor.publish_status()
            return ship, actor, lock, orders

        yield make, policy


def public(ship, policy):
    return cli.main(['--ship', str(ship.folder), '--json', 'status', '--public', str(policy)])


def fail(*args, **kwargs):
    raise AssertionError('dashboard status touched an independent reader')


def no_fallback(monkeypatch):
    read = status_snapshot._bounded_bytes
    def allowed(file, *args, **kwargs):
        assert file.name in (status_snapshot.PUBLIC, 'post-privacy.toml'), file
        return read(file, *args, **kwargs)
    monkeypatch.setattr(status_snapshot, '_bounded_bytes', allowed)
    monkeypatch.setattr(watch, 'status_reading', fail)
    monkeypatch.setattr(post.Store, 'all', fail)
    monkeypatch.setattr(cli.Tmux, 'for_ship', fail)


def test_dashboard_schema_and_privacy_using_one_policy(fleet, monkeypatch, capsys):
    make, policy = fleet
    public_ship = make('enterprise', 'Enterprise')
    folder_private = make('PrIvAtE-ReViEw', 'Peer Review')
    declaration_private = make('renamed', 'PrIvAtE-ReViEw')
    ships = (public_ship, folder_private, declaration_private)
    full = {}
    for ship, _, _, _ in ships:
        assert cli.main(['--ship', str(ship.folder), '--json', 'status']) == 0
        full[ship.folder] = json.loads(capsys.readouterr().out)
        assert full[ship.folder]['reading']['mode'] == 'served'
        assert full[ship.folder]['open_orders'][0]['body'] == TEXT
    no_fallback(monkeypatch)

    for ship, _, _, orders in ships:
        original = full[ship.folder]
        assert public(ship, policy) == 0
        captured = capsys.readouterr()
        assert captured.err == ''
        public_rows = []
        for row in original['seats']:
            name = row['name']
            expected = {key: row[key] for key in ('name', 'harness', 'model', 'pool')}
            expected['activity'] = row['activity']['state']
            if ship == public_ship[0] and name != 'counsel':
                expected['order'] = (orders[name].id if name in ('desk', 'crew') else None)
            public_rows.append(expected)
        result = json.loads(captured.out)
        assert result == {'service': original['service'], 'observed_at': original['reading']['at'],
                          'open_orders': len(original['open_orders']), 'seats': public_rows,
                          'programs': PROGRAMS, 'withheld_titles': []}
        assert datetime.fromisoformat(result['observed_at']).tzinfo == timezone.utc
        # The emitter shares no full document. The private companion has no post text or activity details.
        for contents in (captured.out, status_snapshot.path(ship.runtime, status_snapshot.PUBLIC).read_text()):
            for secret in (TEXT, SUBJECT, ACTIVITY_DETAIL, PROGRAM_ERROR):
                assert secret not in contents
        for name in ('counsel', 'local-private', 'remote-private'):
            assert orders[name].id not in captured.out


def test_non_order_current_items_leave_public_status_available(fleet, monkeypatch, capsys):
    make, policy = fleet
    ship, actor, _, orders = make('fleet-crew', 'Sample Crew', security='sentry')
    question = actor.store.new('question', 'sentry', 'desk', TEXT, re=orders['desk'].id)
    actor.store.new('ack', 'desk', 'sentry', '', re=question.id)
    malformed = 'ord-1234'
    claimed = post.Store.claimed

    def claim(store, seat, before):
        return malformed if seat == 'unassigned' else claimed(store, seat, before)

    monkeypatch.setattr(post.Store, 'claimed', claim)
    assert actor.publish_status()
    ordinary = status_snapshot.served(ship)
    assert ordinary is not None
    full_rows = {row['name']: row for row in ordinary['seats']}
    assert full_rows['desk']['order'] == question.id
    assert full_rows['unassigned']['order'] == malformed
    assert full_rows['crew']['order'] == orders['crew'].id

    no_fallback(monkeypatch)
    assert public(ship, policy) == 0
    captured = capsys.readouterr()
    assert captured.err == ''
    public_rows = []
    for name, row in full_rows.items():
        expected = {key: row[key] for key in ('name', 'harness', 'model', 'pool')}
        expected['activity'] = row['activity']['state']
        if name != 'counsel':
            expected['order'] = orders['crew'].id if name == 'crew' else None
        public_rows.append(expected)
    assert json.loads(captured.out) == {
        'service': ordinary['service'],
        'observed_at': ordinary['reading']['at'],
        'open_orders': len(ordinary['open_orders']),
        'seats': public_rows,
        'programs': PROGRAMS,
        'withheld_titles': [orders['desk'].id],
    }


def test_explicit_empty_policy_is_reread_and_default_ship_adds_no_fields(fleet, monkeypatch, capsys):
    make, policy = fleet
    ship, _, _, orders = make('enterprise', 'Enterprise')
    monkeypatch.setattr(shipmod, 'ships_folder', lambda project=None: ship.folder.parent)
    no_fallback(monkeypatch)
    argv = ['--json', 'status', '--public', str(policy)]
    assert cli.main(argv) == 0
    before = json.loads(capsys.readouterr().out)
    policy.write_text('office_seats=[]\nprivate_ships=[]\n')
    assert cli.main(argv) == 0
    after = json.loads(capsys.readouterr().out)
    assert set(before) == set(after) == {'service', 'observed_at', 'open_orders', 'seats', 'programs',
                                       'withheld_titles'}
    before = {row['name']: row for row in before['seats']}
    after = {row['name']: row for row in after['seats']}
    assert 'order' not in before['counsel']
    assert before['local-private']['order'] is None and before['remote-private']['order'] is None
    for name in ('counsel', 'local-private', 'remote-private'):
        assert after[name]['order'] == orders[name].id


def test_security_thread_withholds_titles_across_private_replies(fleet, monkeypatch, capsys):
    make, policy = fleet
    ship, actor, _, orders = make('fleet-crew', 'Sample Crew', security='sentry')
    store = actor.store
    root = orders['crew']
    bridge = store.new('question', 'counsel', 'operator', TEXT, re=root.id)
    store.new('answer', 'operator', 'SeNtRy@FLEET-CREW', TEXT, re=bridge.id)
    sibling = store.new('order', 'ship', 'unassigned', TEXT, subject=SUBJECT, re=root.id)
    store.new('report', 'unassigned', 'ship', TEXT, re=sibling.id)
    direct = store.new('order', 'desk', 'sentry', TEXT, subject=SUBJECT)
    from_security = store.new('order', 'sentry', 'unassigned', TEXT, subject=SUBJECT)
    local_private = store.new('order', 'counsel', 'unassigned', TEXT, subject=SUBJECT, re=bridge.id)
    prepared = store.prepare('order', 'peer-lead', 'unassigned', TEXT, subject=SUBJECT, re=bridge.id)
    remote_private = replace(prepared, id=prepared.id.replace('-peer-lead-', '-peer-lead@Private-Review-'),
                             sender='peer-lead@Private-Review')
    store.write(remote_private)
    other_ship = store.new('order', 'desk', 'sentry@another-ship', TEXT, subject=SUBJECT)
    wrong_ship_convention = store.new('order', 'desk', 'laan', TEXT, subject=SUBJECT)
    assert actor.publish_status()

    ordinary = status_snapshot.served(ship)
    assert ordinary is not None and 'withheld_titles' not in ordinary
    assert root.id in {order['id'] for order in ordinary['open_orders']}
    assert sibling.id not in {order['id'] for order in ordinary['open_orders']}
    expected = sorted(order.id for order in (root, sibling, direct, from_security))
    companion = status_snapshot.path(ship.runtime, status_snapshot.PUBLIC).read_text()
    assert json.loads(companion)['reading']['withheld_titles'] == sorted(
        [*expected, local_private.id, remote_private.id])
    no_fallback(monkeypatch)
    assert public(ship, policy) == 0
    stdout = capsys.readouterr().out
    result = json.loads(stdout)
    assert result['withheld_titles'] == expected
    assert result['observed_at'] == ordinary['reading']['at']
    assert result['open_orders'] == len(ordinary['open_orders'])
    assert result['programs'] == PROGRAMS
    assert next(row['order'] for row in result['seats'] if row['name'] == 'crew') == root.id
    for value in (TEXT, SUBJECT, bridge.id, local_private.id, remote_private.id, other_ship.id,
                  wrong_ship_convention.id):
        assert value not in stdout
    assert TEXT not in companion and SUBJECT not in companion


@pytest.mark.parametrize(('folder', 'name'), [('enterprise', 'Sample Crew'), ('renamed', 'Enterprise')])
def test_ship_name_does_not_imply_a_security_seat(fleet, capsys, folder, name):
    make, policy = fleet
    ship, actor, _, _ = make(folder, name)
    actor.store.new('order', 'desk', 'laan', TEXT, subject=SUBJECT)
    assert actor.publish_status()
    assert public(ship, policy) == 0
    assert json.loads(capsys.readouterr().out)['withheld_titles'] == []


@pytest.mark.parametrize('unavailable', ['held-store', 'missing-ancestor', 'cycle'])
def test_unproven_graph_is_null_and_preserves_ordinary_status(fleet, monkeypatch, capsys, unavailable):
    make, policy = fleet
    ship, actor, _, orders = make('enterprise', 'Enterprise')
    if unavailable == 'held-store':
        def held(_store):
            raise OSError('synthetic listing unavailable')
        monkeypatch.setattr(post.Store, '_load_current', held)
    else:
        message = actor.store.prepare('answer', 'laan', 'desk', TEXT, re=orders['crew'].id)
        absent = actor.store.prepare('question', 'desk', 'laan', TEXT)
        parent = message.id if unavailable == 'cycle' else absent.id
        actor.store.write(replace(message, re=parent))
    assert actor.publish_status()
    full = status_snapshot.served(ship)
    assert full is not None and 'withheld_titles' not in full
    if unavailable == 'held-store':
        assert full['open_orders'] == [] and full['facts']['post_held']
    no_fallback(monkeypatch)
    assert public(ship, policy) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['withheld_titles'] is None
    assert result['open_orders'] == len(full['open_orders'])
    assert result['programs'] == PROGRAMS


def test_known_post_change_only_invalidates_title_set_until_next_whole_reading(fleet, monkeypatch, capsys):
    make, policy = fleet
    ship, actor, _, orders = make('enterprise', 'Enterprise', security='sentry')
    assert public(ship, policy) == 0
    before = json.loads(capsys.readouterr().out)
    assert before['withheld_titles'] == []

    actor.store.new('question', 'desk', 'sentry', TEXT, re=orders['crew'].id)
    actor.status_request = ('post', frozenset())
    assert actor.publish_status() and actor.status_done == 'post'
    with monkeypatch.context() as patch:
        no_fallback(patch)
        assert public(ship, policy) == 0
    assert json.loads(capsys.readouterr().out) == dict(before, withheld_titles=None)

    assert actor.publish_status() and actor.status_done == 'full'
    assert public(ship, policy) == 0
    assert json.loads(capsys.readouterr().out)['withheld_titles'] == [orders['crew'].id]


@pytest.mark.parametrize('damage', [
    'missing', 'stale', 'clock-skew', 'foreign-owner', 'foreign-start', 'foreign-ship', 'foreign-framework',
    'stopped', 'format', 'json', 'duplicate', 'schema', 'missing-seat', 'activity', 'order', 'count', 'program',
    'observed-at', 'missing-titles', 'wrapped-title', 'short-title', 'duplicate-title', 'unsorted-titles',
])
def test_unavailable_companion_never_reads_ordinary_status(fleet, monkeypatch, capsys, damage):
    make, policy = fleet
    ship, _, lock, orders = make('enterprise', 'Enterprise')
    snapshot = status_snapshot.path(ship.runtime, status_snapshot.PUBLIC)
    data = json.loads(snapshot.read_text())
    assert status_snapshot.path(ship.runtime).exists()
    assert status_snapshot.served(ship) is not None

    def update():
        if damage == 'stale':
            monkeypatch.setattr(status_snapshot, 'time', SimpleNamespace(
                time_ns=lambda: data['wall'] + status_snapshot.MAX_AGE_NS,
                monotonic_ns=lambda: data['monotonic'] + status_snapshot.MAX_AGE_NS))
        elif damage == 'clock-skew':
            monkeypatch.setattr(status_snapshot, 'time', SimpleNamespace(
                time_ns=lambda: data['wall'] + 1_000_000_000, monotonic_ns=lambda: data['monotonic']))
        elif damage == 'stopped':
            lock.close()
        else:
            fields = {'foreign-owner': ('pid', data['pid'] + 1), 'foreign-start': ('process_start', '-1'),
                      'foreign-ship': ('ship', []), 'foreign-framework': ('framework', []), 'format': ('format', 2)}
            if damage in fields:
                key, value = fields[damage]
                data[key] = value
            else:
                reading = data['reading']
                row = reading['seats'][0]
                if damage == 'schema':
                    row['subject'] = SUBJECT
                elif damage == 'missing-seat':
                    reading['seats'].pop()
                elif damage == 'activity':
                    row['activity'] = ACTIVITY_DETAIL
                elif damage == 'order':
                    row['order'] = 'ord-1234'
                elif damage == 'count':
                    reading['open_orders'] = True
                elif damage == 'program':
                    reading['programs'][0]['working_now'] = 0
                elif damage == 'observed-at':
                    reading['observed_at'] = '2026-11-27T20:01:00+00:00'
                elif damage == 'missing-titles':
                    del reading['withheld_titles']
                elif damage == 'wrapped-title':
                    reading['withheld_titles'] = [dict(id=orders['desk'].id, subject=SUBJECT)]
                elif damage == 'short-title':
                    reading['withheld_titles'] = ['ord-1234']
                elif damage == 'duplicate-title':
                    reading['withheld_titles'] = [orders['desk'].id] * 2
                elif damage == 'unsorted-titles':
                    reading['withheld_titles'] = sorted([orders['desk'].id, orders['crew'].id], reverse=True)
            snapshot.write_text(json.dumps(data))

    if damage == 'missing':
        snapshot.unlink()
    elif damage == 'json':
        snapshot.write_text('{')
    elif damage == 'duplicate':
        snapshot.write_text(snapshot.read_text().replace('"format": 1', '"format": 2, "format": 1'))
    else:
        update()
    no_fallback(monkeypatch)
    assert public(ship, policy) == 2
    result = capsys.readouterr()
    assert result.out == ''
    assert result.err == 'qd: public status unavailable\n'


@pytest.mark.parametrize('contents', [
    None, 'office_seats=[]', 'office_seats="counsel"\nprivate_ships=[]',
    'office_seats=[1]\nprivate_ships=[]', 'office_seats=[]\nprivate_ships=[["private-review"]]',
    'office_seats=[]\nprivate_ships=[]\nextra=[]', 'office_seats=["Counsel","counsel"]\nprivate_ships=[]',
    'office_seats=[]\nprivate_ships=["Private-Review","private-review"]', 'office_seats=[]\nprivate_ships=[" " ]',
    'office_seats=["counsel@enterprise"]\nprivate_ships=[]', 'office_seats=[]\nprivate_ships=["fleet/private-review"]',
    'office_seats=[".counsel"]\nprivate_ships=[]', 'office_seats=[]\nprivate_ships=["private-review "]',
])
def test_invalid_policy_fails_before_reading_snapshot(fleet, monkeypatch, capsys, contents):
    make, policy = fleet
    ship, _, _, _ = make('enterprise', 'Enterprise')
    if contents is None:
        policy.unlink()
    else:
        policy.write_text(contents)
    monkeypatch.setattr(status_snapshot, '_read', fail)
    monkeypatch.setattr(status_snapshot, 'request', fail)
    assert public(ship, policy) == 2
    result = capsys.readouterr()
    assert result.out == ''
    assert result.err == 'qd: public status unavailable\n'


def test_invalidation_and_malformed_programs_leave_ordinary_status_available(fleet, monkeypatch, capsys):
    make, policy = fleet
    ship, actor, _, _ = make('enterprise', 'Enterprise')
    companion = status_snapshot.path(ship.runtime, status_snapshot.PUBLIC)
    assert companion.exists()
    monkeypatch.setattr(watch.program_use, 'collect', lambda *args: ([{'harness': 'fake'}], PROGRAM_ERROR))
    assert actor.publish_status()
    assert not companion.exists()
    assert public(ship, policy) == 2
    assert capsys.readouterr().out == ''
    assert cli.main(['--ship', str(ship.folder), '--json', 'status']) == 0
    original = json.loads(capsys.readouterr().out)
    assert original['reading']['mode'] == 'served'
    assert original['programs'] == [{'harness': 'fake'}]
    monkeypatch.setattr(watch.program_use, 'collect', lambda *args: (PROGRAMS, None))
    assert actor.publish_status()
    assert status_snapshot.path(ship.runtime).exists() and companion.exists()
    status_snapshot.invalidate(ship.runtime)
    assert not status_snapshot.path(ship.runtime).exists() and not companion.exists()


def test_program_history_error_clears_last_use_but_keeps_activity(fleet, monkeypatch, capsys):
    make, policy = fleet
    ship, actor, _, _ = make('enterprise', 'Enterprise')
    monkeypatch.setattr(watch.program_use, 'collect', lambda *args: (PROGRAMS, PROGRAM_ERROR))
    assert actor.publish_status()
    assert status_snapshot.served(ship)['programs'] == PROGRAMS
    no_fallback(monkeypatch)
    assert public(ship, policy) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)['programs'] == [dict(row, last_turn_ended=None) for row in PROGRAMS]
    assert PROGRAM_ERROR not in captured.out
    assert PROGRAM_ERROR not in status_snapshot.path(ship.runtime, status_snapshot.PUBLIC).read_text()


def test_unwritable_demand_leaves_the_dashboard_unavailable(fleet, monkeypatch, capsys):
    make, policy = fleet
    ship, _, _, _ = make('enterprise', 'Enterprise')
    no_fallback(monkeypatch)
    monkeypatch.setattr(status_snapshot, 'request', lambda ship: False)
    assert public(ship, policy) == 2
    result = capsys.readouterr()
    assert result.out == ''
    assert result.err == 'qd: public status unavailable\n'
