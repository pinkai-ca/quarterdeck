"""Removed seats retire uncertainty without blocking another seat or inventing delivery."""

from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from quarterdeck import check, pending, post, ship as shipmod
from quarterdeck.drill import Check
from quarterdeck.harness.fake import FakeHarness
from quarterdeck.seat import Launched, Seat
from quarterdeck.service import Service
from quarterdeck.tmux import Pane

HEADER = '[pools.test]\nprovider="codex"\n[sessions.crew]\ntitle="Synthetic"\n'
REMOVED = '[seats.throwaway]\nsession="crew"\nharness="codex"\npool="test"\n'
SURVIVOR = '[seats.survivor]\nsession="crew"\nharness="codex"\npool="test"\n'


def launch(seat, pane='%1', pid=111):
    seat.home.mkdir(parents=True, exist_ok=True)
    launched = Launched(pane, pid, (post.now()-timedelta(minutes=1)).isoformat(), seat.pool.name,
                        None, [seat.harness.name], 'synthetic', harness=seat.harness.name)
    launched.write(seat.folder/'launch.json')
    return launched


def no_keys(*args, **kwargs):
    raise AssertionError('reconciliation and cleanup may not type or launch a program')


@pytest.mark.parametrize('result', [None, 'queued', 'pending', 'blind', 'withheld'])
@pytest.mark.parametrize('journal', [False, True])
@pytest.mark.parametrize('surviving_journal', [False, True])
def test_removed_seat_is_abandoned_once_and_survivor_confirms_on_same_tick(tmp_path, monkeypatch, result, journal, surviving_journal):
    file = tmp_path/'ship.toml'
    file.write_text(HEADER+REMOVED+SURVIVOR)
    tmux = SimpleNamespace(pane=lambda ident: Pane(ident, 'Synthetic', 'worker', 111, 80, 24, False, False, None),
                           paste=no_keys, keys=no_keys)
    svc = Service(shipmod.load(tmp_path), tmux)
    removed, survivor = svc.seat('throwaway'), svc.seat('survivor')
    for seat in (removed, survivor):
        launch(seat)
    old = svc.store.new('order', 'desk', removed.name, 'Synthetic removed delivery')
    opened = svc.attempts.open(old, removed.name, '%1', 111, removed.launched().at)
    proof = pending.Proof.begin(removed, [opened], post.typed([old])) if journal else None
    if proof:
        proof.save(phase='submitted', shown=proof.data['payload'])
    if result:
        svc.attempts.close(opened, result, 'synthetic unresolved result')
    confirmed = svc.store.new('order', 'desk', removed.name, 'Previously confirmed synthetic delivery')
    confirmed_attempt = svc.attempts.close(svc.attempts.open(confirmed, removed.name, '%1', 111, None), 'delivered')
    confirmed_before = confirmed_attempt.path.read_bytes()
    # A real exact input in the surviving seat's owned synthetic record.
    record = survivor.home/'sessions'/'synthetic.jsonl'
    record.parent.mkdir()
    record.write_text(json.dumps({'type': 'session_meta', 'timestamp': survivor.launched().at,
                                  'payload': {'id': 'synthetic'}})+'\n')
    (survivor.folder/'record').write_text(str(record))
    from quarterdeck.record_owner import register
    register(survivor.folder, record, 'codex')
    good = svc.store.new('order', 'desk', survivor.name, 'Synthetic surviving delivery')
    attempt = svc.attempts.open(good, survivor.name, '%1', 111, survivor.launched().at)
    payload = post.typed([good])
    if surviving_journal:
        intact = pending.Proof.begin(survivor, [attempt], payload)
        intact.save(phase='submitted', shown=payload)
    svc.attempts.close(attempt, 'pending' if surviving_journal else 'queued')
    with record.open('a') as stream:
        stream.write(json.dumps({'type': 'event_msg', 'timestamp': post.now().isoformat(),
                                'payload': {'type': 'task_started'}})+'\n')
        stream.write(json.dumps({'type': 'response_item', 'timestamp': post.now().isoformat(),
                                'payload': {'type': 'message', 'role': 'user',
                                            'content': [{'type': 'input_text', 'text': payload}]}})+'\n')
    for duty in ('account', 'inboxes', 'deliveries', 'requests', 'fresh_after_order', 'rebrief', 'credentials', 'failover'):
        monkeypatch.setattr(svc, duty, lambda: None)
    file.write_text(HEADER+SURVIVOR)
    svc.tick()  # reloads the actual synthetic ship.toml
    assert svc.attempts.latest()[old.id] == 'abandoned'
    assert svc.attempts.latest()[good.id] == 'delivered'
    assert confirmed_attempt.path.read_bytes() == confirmed_before
    assert not svc.attempts.for_message(old.id)[0].accepted
    assert svc.store.get(old.id).body == old.body and len(svc.attempts.for_message(old.id)) == 1
    if proof:
        assert json.loads(proof.path.read_text())['phase'] == 'abandoned'
    events = [json.loads(line) for line in svc.log_path.read_text().splitlines()]
    assert [(e['event'], e['seat']) for e in events] == [('abandoned', removed.name)]
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (opened.path, attempt.path, svc.log_path)}
    svc.tick()
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before}
    # Even a reused name or a stale delivery callback cannot revive abandoned transport.
    file.write_text(HEADER+REMOVED+SURVIVOR)
    svc.tick()
    assert svc.attempts.close(opened, 'delivered', 'late callback').result == 'abandoned'
    assert not svc.attempts.undelivered(svc.store, removed.name)
    assert not pending.active(removed)
    svc.ledger.close()


@pytest.mark.parametrize('outcome', ['failed', 'exception', 'stop-exception', 'success'])
def test_check_retires_all_unresolved_attempts_and_journals_before_deleting_seat(tmp_path, monkeypatch, outcome):
    pool = shipmod.Pool('fake', 'fake')
    ship = shipmod.Ship(tmp_path, 'synthetic', 'operator', tmp_path, {'fake': pool}, {}, {})
    state = {'session': False, 'seat': None, 'messages': []}
    tmux = SimpleNamespace(has_session=lambda name: state['session'],
                           kill_session=lambda name: state.update(session=False),
                           pane=lambda ident: Pane(ident, 'Synthetic', 'worker', 111, 80, 24, False, False, None),
                           keys=no_keys, paste=no_keys)
    monkeypatch.setattr(FakeHarness, 'version', lambda self: 'synthetic')
    def probe(checked, seat):
        state.update(session=True, seat=seat)
        launch(seat)
        store, attempts = post.Store(ship.runtime), post.Attempts(ship.runtime)
        for result in (None, 'queued', 'pending', 'blind', 'withheld', 'delivered'):
            msg = store.new('order', 'ship', seat.name, f'Synthetic {result} check delivery')
            state['messages'].append(msg)
            attempt = attempts.open(msg, seat.name, '%1', 111, seat.launched().at)
            proof = pending.Proof.begin(seat, [attempt], post.typed([msg]))
            proof.save(phase='delivered' if result == 'delivered' else 'submitted', shown=proof.data['payload'])
            if result:
                attempts.close(attempt, result)
        if outcome == 'exception':
            raise RuntimeError('synthetic probe exception')
        return [Check('synthetic probe', outcome == 'success')]
    def down(seat):
        assert seat.folder.exists(), 'cleanup must retire transport before deleting the seat'
        attempts = post.Attempts(ship.runtime).all()
        assert [a.result for a in attempts].count('abandoned') == 5
        assert not pending.active(seat)
        if outcome == 'stop-exception':
            raise RuntimeError('synthetic stop exception')
        return 'synthetic down'
    monkeypatch.setattr(check, 'probe', probe)
    monkeypatch.setattr(Seat, 'down', down)
    if 'exception' in outcome:
        with pytest.raises(RuntimeError, match='synthetic .* exception'):
            check.check(ship, 'fake', tmux)
    else:
        result = check.check(ship, 'fake', tmux)
        assert all(c.passed for c in result) == (outcome == 'success')
    seat = state['seat']
    assert not seat.folder.exists() and not state['session']
    attempts = post.Attempts(ship.runtime)
    assert len(attempts.all()) == 6 and len(attempts.accepted_ids()) == 1
    assert len(pending.active(seat, include_terminal=True)) == 6, 'terminal journal history stays in post'
    store = post.Store(ship.runtime)
    for message in state['messages']:
        assert store.get(message.id).body == message.body
        assert store.closing_report(message) is not None
    log = ship.runtime/'log'/'service.jsonl'
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(events) == 5 and all(e['event'] == 'abandoned' for e in events)
    before = log.read_bytes()
    pending.abandon(ship.runtime, seat.name, 'repeated teardown')
    assert log.read_bytes() == before
