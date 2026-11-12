"""Independent validation of ended-turn outcomes: grace, status purity, admission and compatibility.

Every case is synthetic: invented seats, invented fake pi records and invented ids with nonhex suffixes.
"""

from contextlib import nullcontext
from dataclasses import fields, replace
from datetime import timedelta
import errno
import fcntl
import hashlib
from itertools import count
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

import quarterdeck
from quarterdeck import cascade, group_post, post, record_owner, structure_outcome as outcome, watch
from quarterdeck.harness.fake import FakeHarness
from quarterdeck.harness.simulator import Program
from quarterdeck.harness.types import Tokens, Turn
from quarterdeck.seat import Launched
from quarterdeck.ship import Cascade, Group, Pool, SeatSpec, Session, Ship
from quarterdeck.ship import ShipError
from quarterdeck.tmux import Pane

ROOT = Path(__file__).resolve().parents[2]
ENDED = 'turn ended, no report'


@pytest.fixture
def crew(tmp_path, monkeypatch):
    """A ship with a forum of the given members and a three-seat ring, all on the fake program."""
    serial = count()
    monkeypatch.setattr(post.Store, '_new_id', lambda self, kind, sender, at: f'{kind[:3]}-validation-q{next(serial)}')
    monkeypatch.setenv('QD_FAKE_STEPS', '0')
    monkeypatch.setenv('QD_FAKE_ACCEPT_SECONDS', '0')
    monkeypatch.setattr(Program, 'say', lambda *args: None)
    monkeypatch.setattr(Program, 'draw', lambda *args: None)

    def make(kind, members=('alpha',)):
        names = ('alpha', 'beta', 'gamma', 'lead', 'conductor')
        specs = {name: SeatSpec(name, 'crew', 'ring', 'fake', ('fake',), effort='low') for name in names}
        ship = Ship(tmp_path, 'validation', 'operator', tmp_path, {'fake': Pool('fake', 'synthetic')},
                    {'crew': Session('crew', 'Validation')}, specs,
                    groups={'reviewers': Group('reviewers', tuple(members), 'lead')},
                    cascade=Cascade(('alpha', 'beta', 'gamma'), 'conductor', ('low',)))
        svc = SimpleNamespace(ship=ship, runtime=ship.runtime, store=post.Store(ship.runtime),
                              structure_passes=outcome.Passes())
        seats = {}
        svc.seat = seats.__getitem__

        def take(order, reason='stop'):
            name = order.to
            home = ship.runtime / 'seats' / name / 'home'
            home.mkdir(parents=True, exist_ok=True)
            monkeypatch.setenv('QD_FAKE_HOME', str(home))
            monkeypatch.setenv('QD_FAKE_END_REASON', reason)
            program = Program(SimpleNamespace(shape='pi', model='validation-model', effort='low',
                                              resume=f'validation-{name}-session', brief=None, first=None))
            launch = Launched('%validation', 23456, (order.at - timedelta(seconds=1)).isoformat(), 'fake', None,
                              ['fake'], 'fake', 'fake')
            launch.write(home.parent / 'launch.json')
            (home.parent / 'record').write_text(str(program.record.path))
            record_owner.register(home.parent, program.record.path, 'fake')
            locks = ship.runtime / 'post/lifecycle'
            locks.mkdir(parents=True, exist_ok=True)
            (locks / f'{name}.lock').touch()
            pane = Pane(launch.pane, 'Validation', 'Validation', launch.pid, 100, 40, False, False, None)
            seat = SimpleNamespace(name=name, ship=ship, spec=specs[name], pool=ship.pools['fake'],
                                   home=home, folder=home.parent, session=ship.sessions['crew'],
                                   harness=FakeHarness(), _lifecycle=lambda **kwargs: nullcontext(),
                                   current_launch=launch, current_pane=pane,
                                   shown='────────────────\n> \n────────────────\n'
                                         'fake pi · model validation-model · effort low · idle')
            seat.launched = lambda: seat.current_launch
            seat.pane = lambda: seat.current_pane
            seat.record = lambda: program.record.path
            seat.tmux = SimpleNamespace(capture=lambda *args, **kwargs: seat.shown)
            seats[name] = seat
            program.turn(post.typed([order]))
            return seat, program

        if kind == 'forum':
            root, copies = group_post.submit(ship, svc.store, 'reviewers', 'operator', 'Review a fictional bridge.')
            orders = {copy.to: copy for copy in copies}
            advance = lambda: group_post.advance(ship, svc.store, outcomes=lambda n, m: outcome.observe(svc, n, m))
            finished = lambda: bool(json.loads((ship.runtime / 'post/groups' / f'{root.id}.json').read_text())['notice'])
            recipient = 'lead'
        else:
            root = svc.store.new('question', 'operator', 'conductor', 'Improve a fictional bridge.')
            cascade.start(svc, root.id, 'conductor')
            path = cascade.folder(ship.runtime, root.id)
            runner = cascade.Runner(ship, svc.store, lambda *args: None,
                                    lambda n, e: dict(requested=e, actual=e, detail='Validation effort.'),
                                    outcomes=lambda n, m: outcome.observe(svc, n, m))
            runner.advance(path)
            first = svc.store.get(cascade.load(path)['steps'][-1]['order'])
            orders = {first.to: first}
            advance = lambda: runner.advance(path)
            finished = lambda: cascade.load(path)['status'] == 'stopped'
            recipient = 'conductor'

        def notices():
            return [m for m in svc.store.for_seat(recipient) if m.sender == post.SHIP]

        def one_pass():
            svc.structure_passes.begin()
            try:
                advance()
            finally:
                svc.structure_passes.finish()

        def report(order):
            body = '## Cascade note\nValidation note.\n## Answer\nA whole fictional bridge answer.'
            return svc.store.new('report', order.to, order.sender, body, re=order.id)

        return SimpleNamespace(kind=kind, ship=ship, service=svc, root=root, orders=orders, take=take,
                               seats=seats, advance=advance, finished=finished, notices=notices,
                               one_pass=one_pass, report=report)
    return make


def tree(folder):
    """Every entry's identity, size and modification time: what a pure reading must leave alone."""
    entries = []
    for path in sorted(folder.rglob('*')):
        info = path.lstat()
        entries.append((str(path.relative_to(folder)), info.st_mode, info.st_ino, info.st_size, info.st_mtime_ns))
    return entries


def admission_lock(runtime, name):
    return runtime / 'post' / 'admission' / (hashlib.sha256(name.encode()).hexdigest() + '.lock')


def try_exclusive(path):
    """True when an exclusive flock on path is free now; never waits."""
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            assert error.errno in (errno.EWOULDBLOCK, errno.EAGAIN)
            return False
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return True
    finally:
        os.close(descriptor)


# --- pass arithmetic, in memory only ---------------------------------------------------------------

def test_passes_grant_one_whole_completed_pass_and_never_age_within_a_pass():
    passes = outcome.Passes()
    passes.begin()
    assert passes.observe('ord-validation-q', ('first',)) is False
    assert passes.observe('ord-validation-q', ('first',)) is False  # a second call in the same pass
    passes.seen.add('ord-validation-q')
    passes.finish()
    passes.begin()
    assert passes.observe('ord-validation-q', ('first',)) is True


def test_passes_reset_on_a_changed_signature_and_forget_unseen_requests():
    passes = outcome.Passes()
    passes.begin()
    passes.observe('ord-validation-q', ('first',))
    passes.seen.add('ord-validation-q')
    passes.finish()
    passes.begin()
    assert passes.observe('ord-validation-q', ('renewed',)) is False  # renewed work starts its own grace
    passes.finish()  # not seen in this pass: forgotten
    assert 'ord-validation-q' not in passes.candidates
    passes.begin()
    assert passes.observe('ord-validation-q', ('renewed',)) is False


def test_passes_outside_an_active_pass_never_settle_and_drop_the_candidate():
    passes = outcome.Passes()
    passes.begin()
    passes.observe('ord-validation-q', ('first',))
    passes.seen.add('ord-validation-q')
    passes.finish()
    assert passes.observe('ord-validation-q', ('first',)) is False
    assert 'ord-validation-q' not in passes.candidates


def test_an_unfinished_pass_does_not_count_as_grace():
    passes = outcome.Passes()
    passes.begin()
    passes.observe('ord-validation-q', ('first',))
    passes.seen.add('ord-validation-q')
    # The pass never finished (a step outside the guarded loop raised); the next one begins anyway.
    passes.begin()
    assert passes.observe('ord-validation-q', ('first',)) is False
    passes.seen.add('ord-validation-q')
    passes.finish()
    passes.begin()
    assert passes.observe('ord-validation-q', ('first',)) is True


# --- derived wait files: fixed shape, no authority --------------------------------------------------

def valid_wait():
    return dict(format=1, request='ord-validation-q', member='alpha', reason=ENDED,
                observation=['%validation', 23456, 'at', 'fake', '/record', 'conversation', [1, 2, 3, 4, 5], 0])


@pytest.mark.parametrize('change', [
    dict(format=True), dict(format=2), dict(format='1'), dict(request=''), dict(member=None),
    dict(reason='turn ended without a report'), dict(reason='anything a seat typed'),
    dict(extra='field'), dict(observation=None), dict(observation=['%validation'] * 8),
    dict(observation=['%validation', True, 'at', 'fake', '/record', 'conversation', [1, 2, 3, 4, 5], 0]),
    dict(observation=['%validation', 23456, 'at', 'fake', '/record', 'conversation', [1, 2, 3, 4], 0]),
    dict(reason='ownership unknown'),  # an unknown wait carrying an observation
])
def test_wait_shape_rejects_every_deviation(change):
    data = {**valid_wait(), **change}
    with pytest.raises(ValueError):
        outcome._wait_data(data)


def test_wait_shape_accepts_the_two_fixed_forms():
    assert outcome._wait_data(valid_wait()) == valid_wait()
    unknown = {**valid_wait(), 'reason': 'ownership unknown', 'observation': None}
    assert outcome._wait_data(unknown) == unknown


@pytest.mark.parametrize('request_id', ['', None, 7])
def test_wait_path_refuses_an_unusable_request(tmp_path, request_id):
    with pytest.raises(ValueError):
        outcome._path(tmp_path, request_id)


# --- the contract, through both structures ----------------------------------------------------------

def test_forum_member_outcome_is_saved_alone_and_the_notice_waits_for_every_member(crew):
    case = crew('forum', members=('alpha', 'beta'))
    alpha, beta = case.orders['alpha'], case.orders['beta']
    case.take(alpha)
    case.one_pass()
    case.one_pass()
    # Alpha's outcome is positive and saved; beta has no proof either way, so no notice yet.
    journal = json.loads((case.ship.runtime / 'post/groups' / f'{case.root.id}.json').read_text())
    assert journal['failed'] == {alpha.id: dict(member='alpha', reason=outcome.NORMAL)}
    assert journal['notice'] is None and case.notices() == []
    assert outcome.waits(case.ship, list(case.seats.values())) == [
        'waiting on beta: turn outcome unknown (ownership unknown)']
    filed = case.report(beta)
    case.one_pass()
    final = case.notices()
    assert len(final) == 1
    body = final[0].body
    assert 'member failed' in body and f'- beta: {filed.id}' in body and f'- alpha: {outcome.NORMAL}' in body
    assert case.service.store.is_open(alpha) and case.service.store.is_open(case.root)
    assert outcome.waits(case.ship, list(case.seats.values())) == []


def test_forum_with_every_member_ended_unfiled_names_each_and_files_none(crew):
    case = crew('forum', members=('alpha', 'beta'))
    case.take(case.orders['alpha'])
    case.take(case.orders['beta'], 'length')
    case.one_pass()
    assert sorted(outcome.waits(case.ship, list(case.seats.values()))) == [
        f'waiting on alpha: {ENDED}', f'waiting on beta: {ENDED}']
    case.one_pass()
    final = case.notices()
    assert len(final) == 1
    body = final[0].body
    assert 'Filed reports:\n- none' in body
    assert f'- alpha: {outcome.NORMAL}' in body and f'- beta: {outcome.LENGTH}' in body


@pytest.mark.parametrize('kind', ['forum', 'cascade'])
def test_status_line_says_exactly_who_is_waited_on(crew, kind):
    case = crew(kind)
    case.take(case.orders['alpha'])
    case.one_pass()
    lines = outcome.waits(case.ship, list(case.seats.values()))
    assert lines == [f'waiting on alpha: {ENDED}']
    assert f'waiting on alpha: {ENDED}' in watch.Facts('ok', 0, 0, 0, structure_waiting=lines).line()


@pytest.mark.parametrize('kind', ['forum', 'cascade'])
def test_status_reading_writes_nothing_and_creates_no_lifecycle_inode(crew, kind):
    case = crew(kind)
    seat, _ = case.take(case.orders['alpha'])
    case.one_pass()
    before = tree(case.ship.folder)
    for _ in range(3):
        assert outcome.waits(case.ship, [seat]) == [f'waiting on alpha: {ENDED}']
    assert tree(case.ship.folder) == before
    lock = case.ship.runtime / 'post/lifecycle/alpha.lock'
    lock.unlink()
    before = tree(case.ship.folder)
    assert outcome.waits(case.ship, [seat]) == ['waiting on alpha: turn outcome unknown (record proof unknown)']
    assert not lock.exists() and tree(case.ship.folder) == before
    # Status never consumed a pass: the restored seat still needs only the one completed pass.
    lock.touch()
    case.one_pass()
    assert case.finished() and len(case.notices()) == 1


@pytest.mark.parametrize('kind', ['forum', 'cascade'])
def test_status_never_waits_on_a_held_lifecycle_and_shows_it_unknown(crew, kind):
    case = crew(kind)
    seat, _ = case.take(case.orders['alpha'])
    case.one_pass()
    lock = case.ship.runtime / 'post/lifecycle/alpha.lock'
    holder = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX)
        assert outcome.waits(case.ship, [seat]) == [
            'waiting on alpha: turn outcome unknown (record proof unknown)']
    finally:
        os.close(holder)
    assert outcome.waits(case.ship, [seat]) == [f'waiting on alpha: {ENDED}']


@pytest.mark.parametrize('kind', ['forum', 'cascade'])
def test_observer_holds_the_members_report_admission_until_the_coordinator_is_done(crew, kind):
    case = crew(kind)
    order = case.orders['alpha']
    case.take(order)
    case.one_pass()
    lock = admission_lock(case.ship.runtime, 'alpha')
    tracker = case.service.structure_passes
    tracker.begin()
    try:
        with outcome.observe(case.service, 'alpha', order) as ended:
            assert ended == outcome.NORMAL
            assert not try_exclusive(lock)  # a closing report from alpha cannot be admitted now
        assert try_exclusive(lock)
    finally:
        tracker.finish()


@pytest.mark.parametrize('kind', ['forum', 'cascade'])
def test_a_forged_wait_file_grants_no_head_start(crew, kind):
    case = crew(kind)
    order = case.orders['alpha']
    seat, _ = case.take(order)
    found, unknown = outcome._proof(seat, order)
    assert unknown is None
    outcome._wait(case.service, 'alpha', order, ENDED, found[0])
    case.one_pass()  # a fresh service: the file is display only and grace starts now
    assert not case.finished() and case.notices() == []
    case.one_pass()
    assert case.finished() and len(case.notices()) == 1


def test_malformed_and_deeply_nested_evidence_never_hides_a_healthy_wait(crew):
    case = crew('forum')
    seat, _ = case.take(case.orders['alpha'])
    case.one_pass()
    deep = '[' * 200000 + ']' * 200000
    (case.ship.runtime / 'post/groups/zz-validation-broken.json').write_text('{')
    (case.ship.runtime / 'post/structure-waits/zz-validation-deep.json').write_text(deep)
    lines = outcome.waits(case.ship, [seat])
    assert f'waiting on alpha: {ENDED}' in lines
    assert lines.count('structure wait evidence unreadable') == 2


def test_forum_cut_between_saved_notice_and_publication_publishes_it_once(crew, monkeypatch):
    case = crew('forum')
    case.take(case.orders['alpha'])
    case.one_pass()
    publish, cut = group_post._publish, []

    def interrupted(store, message):
        if message.sender == post.SHIP and not cut:
            cut.append(message.id)
            raise ShipError('validation cut before publication')
        publish(store, message)
    monkeypatch.setattr(group_post, '_publish', interrupted)
    case.one_pass()  # the group is held, not lost
    assert cut and case.notices() == []
    hold = case.ship.runtime / 'post/group-holds' / f'{case.root.id}.json'
    assert hold.exists()
    case.one_pass()
    final = case.notices()
    assert [m.id for m in final] == cut and f'alpha: {outcome.NORMAL}' in final[0].body
    assert not hold.exists()
    case.one_pass()
    assert [m.id for m in case.notices()] == cut


def test_cascade_cut_between_saved_stop_and_notice_publishes_it_once(crew, monkeypatch):
    case = crew('cascade')
    case.take(case.orders['alpha'], 'length')
    case.one_pass()
    publish, cut = cascade.Runner.publish, []

    def interrupted(self, *args, **kwargs):
        if not cut:
            cut.append(True)
            raise ShipError('validation cut before publication')
        return publish(self, *args, **kwargs)
    monkeypatch.setattr(cascade.Runner, 'publish', interrupted)
    case.service.structure_passes.begin()
    with pytest.raises(ShipError):
        case.advance()
    case.service.structure_passes.finish()
    data = cascade.load(cascade.folder(case.ship.runtime, case.root.id))
    assert data['status'] == 'stopped' and data['stop_category'] == 'seat'
    assert data['reason'] == f'alpha: {outcome.LENGTH} (lap 1)' and case.notices() == []
    case.one_pass()
    case.one_pass()
    final = case.notices()
    assert len(final) == 1 and f'alpha: {outcome.LENGTH} (lap 1)' in final[0].body
    assert case.service.store.is_open(case.orders['alpha'])


# --- compatibility -------------------------------------------------------------------------------

@pytest.mark.parametrize('module', ['structure_outcome', 'group_post', 'cascade', 'service', 'watch', 'headlines'])
def test_each_module_imports_first_in_a_fresh_interpreter(tmp_path, module):
    source = Path(quarterdeck.__file__).resolve().parents[1]
    env = {**os.environ, 'PYTHONPATH': str(source)}
    code = f'import quarterdeck.{module}, quarterdeck; print(quarterdeck.__file__)'
    done = subprocess.run([sys.executable, '-c', code], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert Path(done.stdout.strip()).resolve() == Path(quarterdeck.__file__).resolve()


def test_turn_keeps_its_old_positional_form_and_new_fields_default_unknown():
    names = [item.name for item in fields(Turn)]
    assert names[-2:] == ['inputs', 'end_reason']
    assert all(item.default is None for item in fields(Turn)[-2:])
    now = post.now()
    old = Turn(3, now, now + timedelta(seconds=2), 'An old input', 'An old reply', 'validation-model', Tokens())
    assert old.inputs is None and old.end_reason is None
    assert replace(old, end_reason='stop') != old and replace(old) == old


def test_group_failure_reasons_keep_the_existing_ones_and_add_only_the_two_named():
    assert group_post.FAILURE_REASONS == frozenset({
        'member no longer declared', 'provider refused this copy', 'owned pane is dead',
        'held at limit with no eligible destination',
        'turn ended without a report', 'turn ended on a length cap without a report'})
    assert outcome.REASONS == {outcome.NORMAL, outcome.LENGTH}


def test_ring_and_reviewer_cards_say_where_to_file_and_that_pane_text_is_not_filed():
    ring = (ROOT / 'ships/cascade/roles/ring.md').read_text()
    reviewer = (ROOT / 'ships/forum/roles/reviewer.md').read_text()
    sentence = 'An answer typed in the pane is not filed. Only `qd post report` files it.'
    assert sentence in ring and sentence in reviewer
    assert '$QD_SHIP/runtime/seats/$QD_SEAT/' in ring and '$QD_SHIP/runtime/seats/$QD_SEAT/' in reviewer
    assert 'qd post report --re <current-order> --body-file "$QD_SHIP/runtime/seats/$QD_SEAT/' in ring
