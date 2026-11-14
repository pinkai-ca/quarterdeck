"""Ordinary brief publication from synthetic declarations; no running service or program."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import harness, standing
from quarterdeck.harness.fake import FakeHarness
from quarterdeck.seat import Seat
from quarterdeck.service import Service
from quarterdeck.ship import Pool, SeatSpec, Session, Ship


@pytest.fixture
def case(tmp_path, monkeypatch):
    framework = tmp_path / 'framework-synthetic'
    folder = tmp_path / 'ship-synthetic'
    for root in (framework, folder):
        (root / 'roles').mkdir(parents=True)
        (root / 'harnesses').mkdir()
    (framework / 'laws.md').write_text('Synthetic laws\n')
    (folder / 'orders.md').write_text('Synthetic orders\n')
    (framework / 'roles' / 'worker-synthetic.md').write_text('Synthetic worker card\n')
    (framework / 'harnesses' / 'fake.md').write_text('Synthetic program card\n')
    (framework / 'harnesses' / 'other-synthetic.md').write_text('Other synthetic card\n')
    monkeypatch.setattr(standing, 'FRAMEWORK_DIR', framework)
    policies = {'fake': 'Synthetic question policy.', 'other-synthetic': 'Other question policy.'}

    def reader(name):
        adapter = FakeHarness()
        adapter.name = name
        adapter.human_tool_policy = policies[name]
        adapter.accepts = lambda pool: None
        return adapter

    monkeypatch.setattr(harness, 'get', reader)
    spec = SeatSpec('worker-synthetic', 'crew-synthetic', 'worker-synthetic', 'fake',
                    ('alpha-synthetic',))
    ship = Ship(folder, 'ship-synthetic', 'operator-synthetic', tmp_path,
                {'alpha-synthetic': Pool('alpha-synthetic', 'synthetic'),
                 'beta-synthetic': Pool('beta-synthetic', 'synthetic')},
                {'crew-synthetic': Session('crew-synthetic', 'Synthetic Crew'),
                 'office-synthetic': Session('office-synthetic', 'Synthetic Office', True)},
                {spec.name: spec})
    service = object.__new__(Service)  # no Ledger, process, provider or service start
    service.ship, service.runtime, service.tmux = ship, ship.runtime, object()
    service.framework_commit, service.told = None, {}
    notices, logs, writes, up = [], [], [], set()
    service.store = SimpleNamespace(new=lambda *args: notices.append(args))
    service.log = lambda *args, **kwargs: logs.append((args, kwargs))
    monkeypatch.setattr(Seat, 'is_up', property(lambda seat: seat.name in up))
    standing.render(ship, spec)
    service.brief_stamp = service._brief_stamp()
    original_write = Path.write_text

    def write(path, text, *args, **kwargs):
        if path.parent == service.runtime / 'brief':
            writes.append(path.name)
        return original_write(path, text, *args, **kwargs)

    monkeypatch.setattr(Path, 'write_text', write)
    return SimpleNamespace(service=service, framework=framework, spec=spec, policies=policies,
                           path=ship.runtime / 'brief' / f'{spec.name}.md', notices=notices,
                           logs=logs, writes=writes, up=up)


def change_seat(case, **fields):
    ship = case.service.ship
    spec = replace(ship.seats[case.spec.name], **fields)
    case.service.ship = replace(ship, seats={spec.name: spec})


def same_mtime(path, text):
    previous = path.stat()
    path.write_text(text)
    os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))


def assert_refresh(case, expected):
    case.service.rebrief()
    assert expected in case.path.read_text()
    assert case.writes == [case.path.name]
    case.service.rebrief()
    assert case.writes == [case.path.name]


@pytest.mark.parametrize('running', [False, True])
def test_next_tick_follows_session_in_ship_file_both_ways_once(case, running):
    service = case.service
    if running:
        case.up.add(case.spec.name)
    # Exercise the real tick reload and rebrief; every unrelated duty is inert.
    for name in ('reconcile', 'account', 'groups', 'cascades', 'fleet_post', 'inboxes', 'recoveries',
                 'refusals', 'looks', 'deliveries', 'follow', 'requests', 'compactions',
                 'fresh_after_order', 'credentials', 'failover', 'heartbeat', 'status_due', 'retire_post'):
        setattr(service, name, lambda: None)
    service.structure_passes = SimpleNamespace(begin=lambda: None, finish=lambda: None)
    template = '''[ship]
name = "ship-synthetic"
operator = "operator-synthetic"
code = "checkout"
[pools.alpha-synthetic]
provider = "synthetic"
endpoint = "http://synthetic.invalid"
[sessions.crew-synthetic]
[sessions.office-synthetic]
human = true
[seats.worker-synthetic]
session = "SESSION"
role = "worker-synthetic"
harness = "fake"
pool = "alpha-synthetic"
'''
    for index, session in enumerate(('office-synthetic', 'crew-synthetic'), start=1):
        service.ship.file.write_text(template.replace('SESSION', session))
        service.tick()
        text = case.path.read_text()
        assert ('Your session belongs to the human' in text) == (session == 'office-synthetic')
        assert ('Messages reach you typed into your pane in full' in text) == (session == 'crew-synthetic')
        assert len(case.writes) == index
        assert len(case.notices) == (index if running else 0)
        service.tick()
        assert len(case.writes) == index
        assert len(case.notices) == (index if running else 0)
    assert not any(args == ('error',) for args, _ in case.logs)


def test_session_human_flag_follows_both_ways_once(case):
    service = case.service
    original = service.ship.sessions['crew-synthetic']
    for index, human in enumerate((True, False), start=1):
        service.ship = replace(service.ship, sessions={**service.ship.sessions,
            original.name: replace(original, human=human)})
        service.rebrief()
        assert ('Your session belongs to the human' in case.path.read_text()) == human
        service.rebrief()
        assert len(case.writes) == index


@pytest.mark.parametrize(('scope', 'field', 'value', 'expected'), [
    ('ship', 'name', 'renamed-synthetic', 'on renamed-synthetic'),
    ('ship', 'operator', 'owner-synthetic', 'human is its owner-synthetic'),
    ('seat', 'persona', 'Reader Synthetic', 'Standing orders for Reader Synthetic'),
    ('seat', 'role', 'reader-synthetic', 'role reader-synthetic'),
    ('seat', 'harness', 'other-synthetic', 'Other synthetic card'),
])
def test_rendered_declaration_fields_refresh_without_card_edit(case, scope, field, value, expected):
    if scope == 'ship':
        case.service.ship = replace(case.service.ship, **{field: value})
    else:
        change_seat(case, **{field: value})
    assert_refresh(case, expected)


def test_seat_rename_renders_new_destination_once(case):
    change_seat(case, name='renamed-synthetic')
    case.service.rebrief()
    path = case.path.with_name('renamed-synthetic.md')
    assert 'qd brief renamed-synthetic' in path.read_text()
    assert case.writes == [path.name]
    case.service.rebrief()
    assert case.writes == [path.name]


def test_folder_changes_rendered_path_and_missing_orders_fallback(case, tmp_path):
    # A stamp is content based even when there are no cards/orders at a new folder.
    original = case.service._brief_stamp()
    case.service.ship = replace(case.service.ship, folder=tmp_path / 'elsewhere-synthetic')
    assert case.service._brief_stamp() != original
    text = standing.brief(case.service.ship, case.spec).text
    assert str(case.service.ship.folder) in text
    assert '(none written yet:' in text


def test_selected_program_change_refreshes_without_card_edit(case):
    change_seat(case, pools=('alpha-synthetic', 'beta-synthetic'), pool_choices={
        'alpha-synthetic': {'harness': 'fake'},
        'beta-synthetic': {'harness': 'other-synthetic'}})
    case.service.brief_stamp = case.service._brief_stamp()
    selection = case.service.runtime / 'seats' / case.spec.name / 'pool'
    selection.parent.mkdir(parents=True)
    selection.write_text('beta-synthetic\n')
    assert_refresh(case, 'Other synthetic card')
    assert 'Other question policy.' in case.path.read_text()


def test_selection_with_same_rendered_program_stays_quiet(case):
    change_seat(case, pools=('alpha-synthetic', 'beta-synthetic'), pool_choices={
        'alpha-synthetic': {'harness': 'fake', 'model': 'first-synthetic'},
        'beta-synthetic': {'harness': 'fake', 'model': 'second-synthetic'}})
    case.service.brief_stamp = case.service._brief_stamp()
    selection = case.service.runtime / 'seats' / case.spec.name / 'pool'
    selection.parent.mkdir(parents=True)
    selection.write_text('beta-synthetic\n')
    case.service.rebrief()
    assert not case.writes and not case.notices


def test_harness_policy_text_is_a_render_dependency(case):
    case.policies['fake'] = 'Revised synthetic question policy.'
    assert_refresh(case, 'Revised synthetic question policy.')


@pytest.mark.parametrize('relative', ['laws.md', 'roles/worker-synthetic.md', 'harnesses/fake.md'])
def test_framework_content_changes_even_at_same_mtime(case, relative):
    same_mtime(case.framework / relative, 'Changed synthetic framework content\n')
    assert_refresh(case, 'Changed synthetic framework content')


def test_orders_content_changes_even_at_same_mtime(case):
    same_mtime(case.service.ship.folder / 'orders.md', 'Changed synthetic orders\n')
    assert_refresh(case, 'Changed synthetic orders')


@pytest.mark.parametrize('relative', ['roles/worker-synthetic.md', 'harnesses/fake.md'])
def test_override_creation_edit_and_removal_follow_precedence(case, relative):
    path = case.service.ship.folder / relative
    path.write_text('Ship synthetic override\n')
    assert_refresh(case, 'Ship synthetic override')
    same_mtime(path, 'Revised ship synthetic override\n')
    case.service.rebrief()
    assert 'Revised ship synthetic override' in case.path.read_text()
    assert len(case.writes) == 2
    same_mtime(case.framework / relative, 'Revised fallback synthetic card\n')
    case.service.rebrief()
    assert len(case.writes) == 2
    path.unlink()
    case.service.rebrief()
    assert 'Revised fallback synthetic card' in case.path.read_text()
    case.service.rebrief()
    assert len(case.writes) == 3


@pytest.mark.parametrize('relative', ['roles/worker-synthetic.md', 'harnesses/fake.md'])
def test_missing_card_fallback_and_reappearance(case, relative):
    path = case.framework / relative
    path.unlink()
    assert_refresh(case, '(no ' + ('role' if relative.startswith('roles') else 'harness') + ' card for')
    path.write_text('Reappeared synthetic card\n')
    case.service.rebrief()
    assert 'Reappeared synthetic card' in case.path.read_text()
    assert len(case.writes) == 2


def test_missing_orders_fallback_and_reappearance(case):
    path = case.service.ship.folder / 'orders.md'
    path.unlink()
    assert_refresh(case, '(none written yet:')
    path.write_text('Reappeared synthetic orders\n')
    case.service.rebrief()
    assert 'Reappeared synthetic orders' in case.path.read_text()
    assert len(case.writes) == 2


@pytest.mark.parametrize(('scope', 'changes'), [
    ('seat', {'model': 'model-synthetic', 'effort': 'high', 'settings': {'mode': 'synthetic'}}),
    ('seat', {'cwd': Path('/synthetic-workspace'), 'fresh': 'after-order', 'remote_control': True}),
    ('ship', {'workspace': Path('/other-synthetic-workspace')}),
])
def test_unrendered_declaration_changes_stay_quiet(case, scope, changes):
    case.up.add(case.spec.name)
    if scope == 'seat':
        change_seat(case, **changes)
    else:
        case.service.ship = replace(case.service.ship, **changes)
    case.service.rebrief()
    assert not case.writes and not case.notices


def test_same_ownership_session_title_and_unused_cards_stay_quiet(case):
    case.up.add(case.spec.name)
    ship = case.service.ship
    case.service.ship = replace(ship, sessions={**ship.sessions,
        'crew-synthetic': replace(ship.sessions['crew-synthetic'], title='New Synthetic Crew'),
        'second-synthetic': Session('second-synthetic', 'Second Synthetic Crew')})
    change_seat(case, session='second-synthetic')
    (case.framework / 'roles' / 'unused-synthetic.md').write_text('Unused synthetic card\n')
    case.service.rebrief()
    assert not case.writes and not case.notices


def test_timestamps_and_stripped_whitespace_do_not_write_or_notice(case):
    case.up.add(case.spec.name)
    path = case.service.ship.folder / 'orders.md'
    path.write_text('\n\nSynthetic orders\n\n')
    previous = path.stat()
    os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1000000000))
    case.service.rebrief()
    assert not case.writes and not case.notices
    # The renderer itself retains its independent identical-byte no-op.
    standing.render(case.service.ship, case.spec)
    assert not case.writes


def test_card_change_keeps_existing_notice_policy_for_affected_running_seat(case):
    service = case.service
    other = replace(case.spec, name='other-worker-synthetic', role='reader-synthetic')
    service.ship = replace(service.ship, seats={**service.ship.seats, other.name: other})
    standing.render(service.ship, other)
    case.writes.clear()
    service.brief_stamp = service._brief_stamp()
    case.up.update((case.spec.name, other.name))
    (case.framework / 'roles' / 'worker-synthetic.md').write_text('Revised synthetic role\n')
    service.rebrief()
    assert case.writes == [case.path.name]
    assert case.notices == [('report', 'ship', case.spec.name,
        f"ship's orders changed · read runtime/brief/{case.spec.name}.md")]
    service.rebrief()
    assert len(case.writes) == len(case.notices) == 1


def test_pending_upgrade_keeps_publication_ownership(case):
    service = case.service
    service.framework_commit = 'revision-synthetic'
    request = service.runtime / 'requests' / 'upgrade-rebrief-revision-synthetic.json'
    request.parent.mkdir()
    request.write_text('{}\n')
    change_seat(case, session='office-synthetic')
    service.rebrief()
    assert not case.writes and not case.notices
    request.unlink()
    assert_refresh(case, 'Your session belongs to the human')


@pytest.mark.parametrize('owner', ['ordinary', 'upgrade'])
def test_publication_reuses_one_render_per_seat_per_stamp_pass(case, monkeypatch, owner):
    service = case.service
    before = hashlib.sha256(case.path.read_bytes()).hexdigest()
    (service.ship.folder / 'orders.md').write_text('Changed synthetic orders\n')
    brief, render, calls = standing.brief, standing.render, []

    def read(*args, **kwargs):
        calls.append(args[1].name)
        return brief(*args, **kwargs)

    def publish(*args, **kwargs):
        assert 'Changed synthetic orders' in kwargs['text']
        return render(*args, **kwargs)

    monkeypatch.setattr(standing, 'brief', read)
    monkeypatch.setattr(standing, 'render', publish)
    if owner == 'ordinary':
        service.rebrief()
    else:
        data = {'before': {case.spec.name: before}, 'launches': {case.spec.name: None},
                'token': 'upgrade-synthetic'}
        assert json.loads(service.upgrade_rebrief(data))['status'] == 'complete'
    assert calls == [case.spec.name]
    assert case.writes == [case.path.name]
    service.rebrief()
    assert calls == [case.spec.name, case.spec.name]
    assert case.writes == [case.path.name]


def test_upgrade_content_stamp_leaves_later_edits_for_ordinary_rebrief(case, monkeypatch):
    service = case.service
    before = hashlib.sha256(case.path.read_bytes()).hexdigest()
    data = {'before': {case.spec.name: before}, 'launches': {case.spec.name: None},
            'token': 'upgrade-synthetic'}
    render = standing.render

    def render_then_edit(*args, **kwargs):
        path = render(*args, **kwargs)
        (service.ship.folder / 'orders.md').write_text('Later synthetic orders\n')
        return path

    monkeypatch.setattr(standing, 'render', render_then_edit)
    assert json.loads(service.upgrade_rebrief(data))['status'] == 'complete'
    assert not case.writes and not case.notices
    monkeypatch.setattr(standing, 'render', render)
    assert_refresh(case, 'Later synthetic orders')


def test_unresolved_selection_does_not_hide_healthy_edits_or_repair(case):
    service = case.service
    broken = replace(case.spec, name='held-synthetic', pools=('alpha-synthetic', 'beta-synthetic'),
                     pool_choices={'alpha-synthetic': {'harness': 'fake'},
                                   'beta-synthetic': {'harness': 'other-synthetic'}})
    service.ship = replace(service.ship, seats={**service.ship.seats, broken.name: broken})
    selection = service.runtime / 'seats' / broken.name / 'pool'
    selection.parent.mkdir(parents=True)
    selection.write_text('missing-synthetic\n')
    service._brief_stamp()
    assert not case.notices and not case.logs  # reading cannot publish selection alarms
    baseline = service.brief_stamp
    (service.ship.folder / 'orders.md').write_text('Healthy synthetic edit\n')
    service.rebrief()
    assert 'Healthy synthetic edit' in case.path.read_text()
    assert service.brief_stamp == baseline
    service.rebrief()
    assert case.writes == [case.path.name]
    selection.write_text('beta-synthetic\n')
    service.rebrief()
    repaired = case.path.with_name('held-synthetic.md')
    assert 'Other synthetic card' in repaired.read_text()
    assert case.writes == [case.path.name, repaired.name]
    assert service.brief_stamp != baseline
    service.rebrief()
    assert case.writes == [case.path.name, repaired.name]


def test_unreadable_render_input_does_not_acknowledge_stamp(case, monkeypatch):
    baseline = case.service.brief_stamp

    def unreadable(*args, **kwargs):
        raise OSError('synthetic card unavailable')

    monkeypatch.setattr(standing, 'brief', unreadable)
    with pytest.raises(OSError, match='synthetic card unavailable'):
        case.service.rebrief()
    assert case.service.brief_stamp == baseline
    assert not case.writes and not case.notices
