"""Synthetic delivery/retirement interleavings and exact old-record receipts."""

from datetime import timedelta
import json
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from quarterdeck import check, credential, lifecycle, pending, post, post_retirement
from quarterdeck.harness.claude import ClaudeHarness
from quarterdeck.harness.synthetic_record import ClaudeShape
from quarterdeck.seat import Launched, Seat
from quarterdeck.service import Service
from quarterdeck.tmux import Pane


class SyntheticSeat(Seat):
    """Real join/lifecycle methods; only the native process and configuration are stand-ins."""

    def __init__(self, runtime, name):
        self.spec = SimpleNamespace(name=name, harness='claude', pool_choices=False, role='worker', pools=('claude',))
        self.ship = SimpleNamespace(runtime=runtime, session_of=lambda spec: SimpleNamespace(human=False))
        self.folder = runtime / 'seats' / name
        self.trusted = runtime / 'trusted' / name
        self.home = self.folder / 'home'
        self.home.mkdir(parents=True)
        self.trusted.mkdir(parents=True)
        self.harness = ClaudeHarness()
        self.harness.preflight_resume = lambda *args: None
        self._pane = Pane('%' + name, 'synthetic', '@' + name, 12345, 100, 40, False, False, None)
        self.tmux = SimpleNamespace(pane=lambda identifier: self._pane if self._pane and self._pane.id == identifier else None)
        self.retire_hook = None
        self._write_launch(post.now() - timedelta(seconds=1), [])

    def _write_launch(self, at, prior):
        Launched(self._pane.id, self._pane.pid, at.isoformat(), 'synthetic-pool', None,
                 ['claude'], 'synthetic', 'claude', prior).write(self.trusted / 'launch.json')

    def _accept_pool(self):
        pass

    def _observe_selection(self):
        return 'synthetic', None

    def adapter_spec(self, spec=None):
        return self.spec if spec is None else spec  # a stand-in configuration: it declares no folder to give

    def _retire_window(self, launched):
        if self.retire_hook:
            self.retire_hook()
        self._pane = None

    def _up(self, resume=True, *, observation=None):
        pin = self.trusted / 'record'
        prior = [pin.read_text().strip()] if pin.exists() else []
        pin.unlink(missing_ok=True)
        self._pane = Pane('%' + self.name + '-next', 'synthetic', '@' + self.name,
                          12346, 100, 40, False, False, None)
        self._write_launch(post.now(), prior)
        return 'synthetic up'


def service_for(runtime, *seats):
    service = object.__new__(Service)
    service.runtime = runtime
    service.ship = SimpleNamespace(runtime=runtime, seats={seat.name: seat.spec for seat in seats})
    service.store = post.Store(runtime)
    service.attempts = post.Attempts(runtime)
    service.withheld = {}
    service.log_path = runtime / 'log' / 'service.jsonl'
    by_name = {seat.name: seat for seat in seats}
    service.seat = by_name.__getitem__
    return service


@pytest.fixture
def no_credentials(monkeypatch):
    monkeypatch.setattr(credential, 'reseed', lambda *args, **bounds: None)


def open_attempt(service, seat, message):
    launch = seat.launched()
    return service.attempts.open(message, seat.name, launch.pane, launch.pid, launch.at)


def install_door(monkeypatch, action):
    class Door:
        def __init__(self, seat):
            self.seat = seat

        def reading(self):
            return SimpleNamespace(open=True, clears=None)

        def screen(self):
            return SimpleNamespace(text='', box='')

        def deliver(self, text, speed=None, proof=None):
            action(self.seat, proof)
            return SimpleNamespace(result='withheld', detail='synthetic transport ended without input')

    monkeypatch.setattr('quarterdeck.service.Door', Door)


def test_relaunch_waits_only_for_its_own_delivery(tmp_path, monkeypatch, no_credentials):
    first, other = SyntheticSeat(tmp_path, 'first'), SyntheticSeat(tmp_path, 'other')
    service = service_for(tmp_path, first, other)
    message = service.store.new('report', 'sender', first.name, 'synthetic work')
    entered, release = Event(), Event()
    errors = []

    def held_delivery(seat, proof):
        entered.set()
        assert release.wait(5)

    install_door(monkeypatch, held_delivery)

    def deliver():
        try:
            service.deliver(first, [message])
        except BaseException as error:
            errors.append(error)

    worker = Thread(target=deliver)
    worker.start()
    try:
        assert entered.wait(5)
        original = first.launched()
        with pytest.raises(lifecycle.Busy):
            first.fresh()
        assert first.launched() == original
        assert 'synthetic up' in other.fresh()
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert errors == []
    assert 'synthetic up' in first.fresh()
    assert first.launched() != original


@pytest.mark.parametrize('missing', ['pane', 'pid', 'launch'])
def test_incomplete_launch_opens_neither_attempt_nor_journal(tmp_path, monkeypatch, missing):
    seat = SyntheticSeat(tmp_path, 'worker')
    service = service_for(tmp_path, seat)
    message = service.store.new('report', 'sender', seat.name, 'synthetic work')
    install_door(monkeypatch, lambda *args: pytest.fail('no transport without a complete launch'))
    if missing == 'pane':
        seat._pane = None
    elif missing == 'pid':
        data = json.loads((seat.trusted / 'launch.json').read_text())
        data['pid'] = None
        (seat.trusted / 'launch.json').write_text(json.dumps(data))
    else:
        (seat.trusted / 'launch.json').unlink()
    assert 'message waits unread' in service._deliver(seat, message)
    assert service.attempts.all() == []
    assert not list((tmp_path / 'post/pending').glob('*/*.json'))


def test_binding_changed_before_reservation_is_withheld_without_a_journal(tmp_path, monkeypatch):
    seat = SyntheticSeat(tmp_path, 'worker')
    service = service_for(tmp_path, seat)
    message = service.store.new('report', 'sender', seat.name, 'synthetic work')
    install_door(monkeypatch, lambda *args: pytest.fail('changed launch must not receive input'))

    def changed_record():
        seat._pane = None
        return None

    monkeypatch.setattr(seat, 'record', changed_record)
    assert 'launch changed before reservation' in service._deliver(seat, message)
    assert service.attempts.last_results(seat.name)[message.id] == 'withheld'
    assert not list((tmp_path / 'post/pending').glob('*/*.json'))


def test_begin_refuses_an_attempt_from_a_previous_complete_launch(tmp_path):
    seat = SyntheticSeat(tmp_path, 'worker')
    service = service_for(tmp_path, seat)
    message = service.store.new('report', 'sender', seat.name, 'synthetic work')
    attempt = open_attempt(service, seat, message)
    seat._write_launch(post.now(), [])
    assert pending.current_identity(seat) is not None
    with pytest.raises(pending.NoCurrentLaunch):
        pending.Proof.begin(seat, attempt, post.typed([message]))
    assert not list((tmp_path / 'post/pending').glob('*/*.json'))


def test_probe_holds_a_lost_binding_without_typing_or_waiting_for_a_task(tmp_path, monkeypatch, no_credentials):
    seat = SyntheticSeat(tmp_path, 'worker')
    begin = pending.Proof.begin
    waits = []

    def lose_binding(current, attempt, text, **kwargs):
        seat._pane = None
        return begin(current, attempt, text, **kwargs)

    def wait_for(predicate, timeout):
        waits.append(timeout)
        return predicate()

    monkeypatch.setattr(pending.Proof, 'begin', lose_binding)
    monkeypatch.setattr(check, 'ready_for_probe', lambda *args: True)
    monkeypatch.setattr(check, 'wait_for', wait_for)
    monkeypatch.setattr(check, 'Door', lambda current: SimpleNamespace(
        reading=lambda: 'synthetic empty box',
        deliver=lambda *args, **kwargs: pytest.fail('no input under an unproved launch')))

    checks = check.probe(seat.ship, seat)

    assert len(waits) == 1
    assert checks[-2].name == 'paste held: current launch unconfirmed' and not checks[-2].passed
    assert checks[-1].name == 'record joined, the turn ended and read' and not checks[-1].passed
    assert '? turns' in checks[-1].detail
    assert [attempt.result for attempt in post.Attempts(tmp_path).all()] == ['withheld']
    assert not list((tmp_path / 'post/pending').glob('*/*.json'))


def legacy_journal(service, seat, *, extra=None, missing=('pane', 'pid', 'launch')):
    message = service.store.new('report', 'sender', seat.name, 'synthetic old work')
    launch = seat.launched()
    identity = dict(pane=launch.pane, pid=launch.pid, launch=launch.at, harness='claude')
    identity.update((key, None) for key in missing)
    attempt = service.attempts.open(message, seat.name, identity['pane'], identity['pid'], identity['launch'])
    data = dict(seat=seat.name, identity=identity,
                at=attempt.at, since=attempt.at, payload=post.typed([message]), phase='cancelled',
                record=None, shown=None, attempts=[attempt.path.name])
    data.update(extra or {})
    path = service.runtime / 'post/pending' / seat.name / attempt.path.name
    path.parent.mkdir(parents=True, exist_ok=True)
    original = (json.dumps(data, indent=3) + '\n\n').encode()
    path.write_bytes(original)
    return message, attempt, path, original


@pytest.mark.parametrize('act', ['restart', 'fresh'])
@pytest.mark.parametrize('missing', [('pane',), ('pid',), ('launch',), ('pane', 'pid', 'launch')])
def test_service_retires_legacy_identity_without_resend_or_lifecycle_hold(tmp_path, no_credentials, act, missing):
    seat = SyntheticSeat(tmp_path, 'worker')
    service = service_for(tmp_path, seat)
    message, attempt, path, original = legacy_journal(service, seat, missing=missing)
    pending.reconcile(service)
    pending.reconcile(service)
    assert not path.exists()
    assert (path.parent / 'retired' / path.name).read_bytes() == original
    assert service.attempts.last_results(seat.name)[message.id] == 'abandoned'
    assert 'outcome unknown; never resent' in service.attempts.for_message(message.id)[0].detail
    assert service.attempts.undelivered(service.store, seat.name) == []
    assert pending.journals(seat) == ([], [])
    assert pending.detached(seat) == []
    rows = [json.loads(line) for line in service.log_path.read_text().splitlines()]
    assert [row['event'] for row in rows] == ['pending-retired']
    assert 'synthetic up' in getattr(seat, act)()


def test_legacy_retirement_preserves_an_intervening_delivered_attempt(tmp_path, monkeypatch):
    seat = SyntheticSeat(tmp_path, 'worker')
    service = service_for(tmp_path, seat)
    _, attempt, path, original = legacy_journal(service, seat)
    settle = post.Attempts.settle_missing_identity
    accepted = []

    def intervening_receipt(self, expected, detail):
        service.attempts.close(attempt, 'delivered', 'synthetic prior positive receipt')
        accepted.append(attempt.path.read_bytes())
        return settle(self, expected, detail)

    monkeypatch.setattr(post.Attempts, 'settle_missing_identity', intervening_receipt)
    pending.reconcile(service)
    assert attempt.path.read_bytes() == accepted[0]
    assert (path.parent / 'retired' / path.name).read_bytes() == original


@pytest.mark.parametrize('extra', [dict(payload=None), dict(recovery={}), dict(receipt_refresh={}),
                                  dict(paste_clear={}), dict(paste_refusal={})])
def test_malformed_or_separately_owned_evidence_stays_held(tmp_path, extra):
    seat = SyntheticSeat(tmp_path, 'worker')
    service = service_for(tmp_path, seat)
    _, attempt, path, original = legacy_journal(service, seat, extra=extra)
    pending.reconcile(service)
    assert path.read_bytes() == original
    assert service.attempts.all()[0].result is None
    assert pending.journals(seat)[1] == [path.name]


@pytest.mark.parametrize('when', ['before-retire', 'at-retire'])
def test_fresh_links_and_settles_submission_from_the_exact_old_record(tmp_path, monkeypatch, no_credentials, when):
    seat = SyntheticSeat(tmp_path, 'worker')
    service = service_for(tmp_path, seat)
    message = service.store.new('report', 'sender', seat.name, 'synthetic delivered payload')
    attempt = open_attempt(service, seat, message)
    proof = pending.Proof.begin(seat, attempt, post.typed([message]))
    proof.save(phase='submitted')
    assert proof.data['record'] is None
    writer = ClaudeShape(seat.home, tmp_path, 'synthetic-session', 'synthetic-model')

    def write_record():
        writer.begin(post.typed([message]), 'synthetic-model', '')

    if when == 'before-retire':
        write_record()
    else:
        seat.retire_hook = write_record
    monkeypatch.setattr(service, 'deliver', lambda *args, **kwargs: pytest.fail('no resend during retirement'))
    seat.fresh()
    old = json.loads(proof.path.read_text())
    assert old['record'] == str(writer.path)
    assert old['phase'] == 'submitted'
    later = open_attempt(service, seat, message)
    service.attempts.close(later, 'launch', 'synthetic indexed context; not a receipt')
    carried = later.path.read_bytes()
    original = writer.path.read_bytes()
    pending.reconcile(service)
    assert json.loads(proof.path.read_text())['phase'] == 'delivered'
    assert service.attempts.for_message(message.id)[0].result == 'delivered'
    assert later.path.read_bytes() == carried
    assert writer.path.read_bytes() == original
    assert seat.record() is None
    assert pending.detached(seat) == []


@pytest.mark.parametrize('reply,current,settled', [
    ('report', False, True), (None, False, False), ('ack', False, False),
    ('report', True, False), ('foreign-report', False, False),
    ('answer', False, False), ('missing-message', False, False),
    ('unreadable-report', False, False),
])
def test_report_settles_only_a_detached_journal(tmp_path, monkeypatch, reply, current, settled):
    seat = SyntheticSeat(tmp_path, 'worker')
    service = service_for(tmp_path, seat)
    service.ledger = SimpleNamespace(catching_up=lambda: False)
    service.heartbeat = lambda: None
    message = service.store.new('order', 'sender', seat.name, 'synthetic work')
    attempt = open_attempt(service, seat, message)
    proof = pending.Proof.begin(seat, attempt, post.typed([message]))
    proof.save(phase='submitted')
    service.attempts.close(attempt, 'pending', 'synthetic unresolved submission')
    original = proof.path.read_bytes()
    if not current:
        seat._write_launch(post.now(), [])
    if reply in ('report', 'ack', 'answer', 'foreign-report', 'unreadable-report'):
        report = service.store.new('report' if 'report' in reply else reply,
                                   'other' if reply == 'foreign-report' else seat.name,
                                   'sender', 'synthetic reply', re=message.id)
        if reply == 'unreadable-report':
            (service.store.dir / (report.id + '.json')).write_text('{')
    if reply == 'missing-message':
        (service.store.dir / (message.id + '.json')).unlink()
    monkeypatch.setattr(service, 'deliver', lambda *a, **k: pytest.fail('settlement must never type'))
    monkeypatch.setattr(pending.Proof, 'confirmed', lambda *a: None)
    monkeypatch.setattr('quarterdeck.harness.closed_record.not_in_closed_record', lambda *a: False)

    pending.reconcile(service)
    pending.reconcile(service)

    if settled:
        from quarterdeck.pending_lifecycle import REPORTED
        data = json.loads(proof.path.read_text())
        assert data['phase'] == 'abandoned' and data['resolution'] == REPORTED
        assert pending.detached(seat) == []
        saved = proof.path.read_bytes()
        receipt = service.attempts.for_message(message.id)[0]
        assert receipt.result == 'abandoned' and receipt.detail == REPORTED
        assert message.id not in service.attempts.accepted_ids()
        rows = [json.loads(line) for line in service.log_path.read_text().splitlines()]
        assert [row['event'] for row in rows] == ['pending-settled']
        assert post_retirement.run(service) == dict(attempts=1, journals=1)
        assert not proof.path.exists()
        assert pending.journal_path(tmp_path, seat.name, proof.path.name).read_bytes() == saved
        assert post_retirement.run(service) == dict(attempts=0, journals=0)
    else:
        assert proof.path.read_bytes() == original
        assert service.attempts.for_message(message.id)[0].result == 'pending'
        assert bool(pending.detached(seat)) is not current
        if reply not in ('missing-message', 'unreadable-report'):
            assert post_retirement.run(service) == dict(attempts=0, journals=0)


def test_report_settlement_recovers_after_attempts_written(tmp_path, monkeypatch):
    seat = SyntheticSeat(tmp_path, 'worker')
    service = service_for(tmp_path, seat)
    message = service.store.new('order', 'sender', seat.name, 'synthetic work')
    attempt = open_attempt(service, seat, message)
    proof = pending.Proof.begin(seat, attempt, post.typed([message]))
    proof.save(phase='submitted')
    seat._write_launch(post.now(), [])
    service.store.new('report', seat.name, 'sender', 'done', re=message.id)
    with monkeypatch.context() as patch:
        patch.setattr(pending.Proof, 'save', lambda *a, **k: (_ for _ in ()).throw(OSError('synthetic interruption')))
        with pytest.raises(OSError, match='synthetic interruption'):
            pending.reconcile(service)
    assert service.attempts.for_message(message.id)[0].result == 'abandoned'
    assert json.loads(proof.path.read_text())['phase'] == 'submitted'
    pending.reconcile(service)
    assert json.loads(proof.path.read_text())['phase'] == 'abandoned'
    assert pending.detached(seat) == []
