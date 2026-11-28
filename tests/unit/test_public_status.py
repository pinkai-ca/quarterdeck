"""The dashboard reads only the service's minimum publication."""

from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from quarterdeck import cli, post, presentation, service, servicectl, ship as shipmod, status_snapshot, watch

POLICY = 'office_seats = ["CoUnSeL"]\nprivate_ships = ["rEfErEeS"]\n'
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
        def make(folder_name, declared_name):
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
            for name in NAMES:
                declaration += f'''[seats.{name}]
session = "crew"
role = "worker"
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
            remote = 'lead@Referees'
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
    folder_private = make('ReFeReEs', 'Peer Review')
    declaration_private = make('renamed', 'ReFeReEs')
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
                          'programs': PROGRAMS}
        assert datetime.fromisoformat(result['observed_at']).tzinfo == timezone.utc
        # The emitter shares no full document. The private companion has no post text or activity details.
        for contents in (captured.out, status_snapshot.path(ship.runtime, status_snapshot.PUBLIC).read_text()):
            for secret in (TEXT, SUBJECT, ACTIVITY_DETAIL, PROGRAM_ERROR):
                assert secret not in contents
        for name in ('counsel', 'local-private', 'remote-private'):
            assert orders[name].id not in captured.out


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
    assert set(before) == set(after) == {'service', 'observed_at', 'open_orders', 'seats', 'programs'}
    before = {row['name']: row for row in before['seats']}
    after = {row['name']: row for row in after['seats']}
    assert 'order' not in before['counsel']
    assert before['local-private']['order'] is None and before['remote-private']['order'] is None
    for name in ('counsel', 'local-private', 'remote-private'):
        assert after[name]['order'] == orders[name].id


@pytest.mark.parametrize('damage', [
    'missing', 'stale', 'clock-skew', 'foreign-owner', 'foreign-start', 'foreign-ship', 'foreign-framework',
    'stopped', 'format', 'json', 'duplicate', 'schema', 'missing-seat', 'activity', 'order', 'count', 'program',
    'observed-at',
])
def test_unavailable_companion_never_reads_ordinary_status(fleet, monkeypatch, capsys, damage):
    make, policy = fleet
    ship, _, lock, _ = make('enterprise', 'Enterprise')
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
    'office_seats=[1]\nprivate_ships=[]', 'office_seats=[]\nprivate_ships=[["referees"]]',
    'office_seats=[]\nprivate_ships=[]\nextra=[]', 'office_seats=["Counsel","counsel"]\nprivate_ships=[]',
    'office_seats=[]\nprivate_ships=["Referees","referees"]', 'office_seats=[]\nprivate_ships=[" " ]',
    'office_seats=["counsel@enterprise"]\nprivate_ships=[]', 'office_seats=[]\nprivate_ships=["fleet/referees"]',
    'office_seats=[".counsel"]\nprivate_ships=[]', 'office_seats=[]\nprivate_ships=["referees "]',
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
