"""One native continuation for proved thinking exhaustion, through the existing Door journal.

Adapters own detection and request controls. This module owns only the durable
reservation, publication, receipt and unresolved-mode hold. No uncertain key or
input is replayed, and a launch never silently retires the reservation.
"""

from dataclasses import asdict
from datetime import datetime
import time

from . import post, record_owner
from .harness.types import CappedGeneration
from .harness.thinking import digest, validate_launch
from .ship import ShipError

MODES = {'claimed', 'disabling', 'off', 'submitted', 'restoring', 'done', 'held'}
MODE_SECONDS = 2.0


def validate(meta):
    if (not isinstance(meta, dict) or set(meta) != {'format', 'source', 'client', 'evidence', 'mode', 'post', 'recovery_input', 'error'}
            or type(meta['format']) is not int or meta['format'] != 1 or meta['mode'] not in MODES
            or not isinstance(meta['post'], str) or not meta['post'] or '/' in meta['post']
            or not isinstance(meta['client'], str) or len(meta['client']) != 64
            or (meta['recovery_input'] is not None and not isinstance(meta['recovery_input'], str))
            or (meta['error'] is not None and not isinstance(meta['error'], str))):
        raise ValueError('invalid thinking-recovery journal')
    validate_launch(meta['evidence'])
    if digest(meta['evidence']) != meta['client']:
        raise ValueError('thinking-recovery launch proof conflicts with its identity')
    source = meta['source']
    if not isinstance(source, dict) or set(source) != set(CappedGeneration.__dataclass_fields__):
        raise ValueError('invalid thinking-recovery source')
    for field in ('conversation', 'work', 'user', 'generation', 'model', 'provider', 'signature'):
        if not isinstance(source[field], str) or not source[field]:
            raise ValueError('unknown thinking-recovery source identity')
    if (source['reasoning'] is not None and (type(source['reasoning']) is not int or source['reasoning'] <= 0)
            or type(source['written']) is not int or source['written'] < 0):
        raise ValueError('invalid thinking-recovery usage evidence')


def mode_hold(seat, proofs, ignored=None):
    for proof in proofs:
        meta = proof.data.get('recovery')
        if meta is None or meta['mode'] == 'done' or (ignored is not None and proof.path == ignored.path):
            continue
        missing = not (seat.ship.runtime / 'post/store' / f"{meta['post']}.json").is_file()
        reason = ('continuation post missing; reservation retained' if missing else
                  meta['error'] or f"{meta['mode']}; delivery phase {proof.data['phase']}")
        return f'thinking recovery mode unresolved ({reason}); inspect retained journal {proof.path.name}'
    return None


def held(seat):
    from . import pending
    proofs, errors = pending.journals(seat)
    if errors:
        return 'pending delivery journal unreadable; recovery/lifecycle held'
    return mode_hold(seat, proofs)


def require_clear(seat):
    if reason := held(seat):
        raise ShipError(f'{seat.name}: {reason}; identity-checked qd down remains available')


def _idle(state, screen):
    return (state.turn_open is False and state.pending_tools == () and not state.queued
            and screen is not None and screen.box == '' and not screen.working and not screen.queued
            and not screen.unknown and not screen.interruption and not screen.starting)


def _client(seat):
    launch = seat.launched()
    if launch is None or seat.session.human or seat.pool.kind != 'endpoint':
        return None
    return seat.harness.thinking_recovery(seat.home, getattr(launch, 'recovery', None), launch.version, seat.spec, seat.pool)


def _current(seat, proof=None, *, occupied=False):
    from .door import Door
    client = _client(seat)
    if client is None:
        return None
    record = seat.record()
    if (record is None or seat.home not in record.parents or record.resolve() != record.absolute()
            or record_owner.pinned_reader(seat, record) != seat.harness.name
            or (proof is not None and (str(record) != proof.data['record'] or not proof.same_launch(seat)))):
        return None
    state = seat.harness.state(record)
    door = Door(seat)
    door.proof = proof
    screen = door.screen()
    if occupied:
        from . import pending
        proofs, errors = pending.journals(seat)
        if (errors or any(p.path != proof.path and p.data['phase'] not in pending.TERMINAL for p in proofs)
                or mode_hold(seat, proofs, proof) or door.wall(screen)
                or state.turn_open is not False or state.pending_tools != () or state.queued
                or screen is None or screen.working or screen.queued or screen.unknown or screen.interruption or screen.starting
                or not door.matches(seat.harness.box(proof.data['payload']), screen)):
            return None  # only this already-verified paste may occupy the box before its submit check
    elif not _idle(state, screen) or not door.reading(screen).open:
        return None
    return client, state, screen, door


def _source(seat, proof, mode):
    if not _publication(seat, proof):
        return None
    found = _current(seat, proof, occupied=proof.data['phase'] in ('pasting', 'submitting'))
    if found is None:
        return None
    client, state, screen, _ = found
    meta = proof.data['recovery']
    if (client.signature != meta['client'] or state.capped is None or asdict(state.capped) != meta['source']
            or not client.eligible(state.capped) or client.mode(state, screen) != mode):
        return None
    return found


def _publication(seat, proof):
    try:
        message = post.Store(seat.ship.runtime).get(proof.data['recovery']['post'])
    except (OSError, ValueError, ShipError):
        return False
    return (message.kind == 'report' and message.sender == post.SHIP and message.to == seat.name
            and post.typed([message]) == proof.data['payload'])


def _received(seat, proof):
    if proof.confirmed(seat) != 'delivered':
        return None
    proof.finish('delivered')  # reconcile a receipt even if interruption left its transport phase uncertain
    found = _current(seat, proof)
    if found is None:
        return None
    client, state, screen, _ = found
    meta = proof.data['recovery']
    if client.signature != meta['client'] or state.conversation != meta['source']['conversation']:
        return None
    native = client.accepted_input(seat.record(), proof.data['payload'], datetime.fromisoformat(proof.data['since']),
                                   meta['source']['conversation'])
    if native is None or state.work_id != native or meta['recovery_input'] not in (None, native):
        return None
    if not any(t.timed and t.started is not None and t.ended is not None and state.last_input_at is not None
               and t.started <= state.last_input_at <= t.ended for t in state.completed_turns):
        return None
    return found, native


def _save(proof, **fields):
    meta = {**proof.data['recovery'], **fields}
    validate(meta)
    proof.save(recovery=meta)


def _wait(check):
    deadline = time.monotonic() + MODE_SECONDS
    while True:
        if found := check():
            return found
        if time.monotonic() >= deadline:
            return None
        time.sleep(.05)


def _restore(seat, proof):
    meta = proof.data['recovery']
    if proof.data['phase'] == 'delivered' and meta['mode'] == 'submitted' and meta['error'] is not None:
        return  # a previously exposed uncertainty is not erased by later waiting evidence
    received = _received(seat, proof)
    if received is None:
        if proof.data['phase'] == 'delivered':
            client, record = _client(seat), seat.record()
            state = seat.harness.state(record) if record is not None else None
            from .door import Door
            screen = Door(seat).screen()
            meta = proof.data['recovery']
            native = (client.accepted_input(record, proof.data['payload'], datetime.fromisoformat(proof.data['since']),
                      meta['source']['conversation']) if client is not None and record is not None else None)
            known_work = (client is not None and client.signature == meta['client'] and proof.same_launch(seat)
                          and state is not None and state.conversation == meta['source']['conversation']
                          and state.pending_tools is not None and native is not None and state.work_id == native
                          and meta['recovery_input'] in (None, native))
            wait_fact = getattr(client, 'waiting_for_work', None)
            safe_wait = (callable(wait_fact) and meta['mode'] == 'submitted' and meta['error'] is None
                         and client.signature == meta['client'] and proof.same_launch(seat)
                         and record is not None and seat.home in record.parents and record.resolve() == record.absolute()
                         and record_owner.pinned_reader(seat, record) == seat.harness.name
                         and state is not None and state.turn_open is True and state.pending_tools == () and not state.queued
                         and native is not None and meta['recovery_input'] in (None, native)
                         and screen is not None and screen.box == '' and screen.working and not screen.queued
                         and not screen.unknown and not screen.interruption and not screen.starting
                         and not Door(seat).wall(screen) and client.mode(state, screen) == 'off'
                         and wait_fact(record, meta['source'], native, proof.data['payload'],
                                       datetime.fromisoformat(proof.data['since']), state))
            if safe_wait or (known_work and (state.turn_open or state.pending_tools or state.queued or (screen and screen.working))):
                _save(proof, recovery_input=native, error=None)  # ordinary work, not a request for a human
            else:
                _save(proof, error='fresh recovery source/receipt/idle/box evidence unknown; original mode not restored')
        return  # work/queue/receipt still unresolved; no key or input is repeated
    (client, state, screen, door), native = received
    if proof.data['recovery']['recovery_input'] is None:
        _save(proof, recovery_input=native)
    proof.finish('delivered')  # exact receipt, never a mode-control label
    mode = client.mode(state, screen)
    if mode == 'original':
        _save(proof, mode='done', error=None)
        return
    if mode != 'off' or proof.data['recovery']['mode'] == 'restoring':
        _save(proof, mode='held', error='restore outcome or intervening native setting unknown; no key repeated')
        return
    _save(proof, mode='restoring')  # before the sole return-control queue

    def verify():
        current = _received(seat, proof)
        return current is not None and current[0][0].mode(current[0][1], current[0][2]) == 'off'

    result = door.control_keys(client.restore_keys, proof, verify)
    if result.result != 'typed':
        _save(proof, mode='held', error='restore refused: ' + str(result))
        return

    def restored():
        current = _received(seat, proof)
        return current if current is not None and current[0][0].mode(current[0][1], current[0][2]) == 'original' else None

    if _wait(restored):
        _save(proof, mode='done', error=None)
    else:
        _save(proof, mode='held', error='restore not confirmed; no key repeated')


def _advance(service, seat, proof, new=False):
    meta = proof.data['recovery']
    if meta['mode'] == 'done':
        return
    if meta['mode'] == 'held':
        # Late proof may confirm an already restored mode, never repeat a key.
        received = _received(seat, proof)
        if received is not None and received[0][0].mode(received[0][1], received[0][2]) == 'original':
            _save(proof, mode='done', error=None)
        return
    if not _publication(seat, proof):
        _save(proof, mode='held', error='continuation post missing or conflicts with reservation; retained')
        return
    if meta['mode'] in ('claimed', 'disabling'):
        if not new or (current := _source(seat, proof, 'original')) is None:
            _save(proof, mode='held', error='control interrupted or source/idle changed; no key repeated')
            return
        client, _, _, door = current
        _save(proof, mode='disabling')
        result = door.control_keys(client.off_keys, proof, lambda: _source(seat, proof, 'original') is not None)
        if result.result != 'typed' or not _wait(lambda: _source(seat, proof, 'off')):
            _save(proof, mode='held', error='thinking-off control unconfirmed; no key repeated')
            return
        _save(proof, mode='off')
    if proof.data['recovery']['mode'] == 'off' and proof.data['phase'] == 'prepared':
        current = _source(seat, proof, 'off')
        if current is None:
            _save(proof, mode='held', error='source or idle changed before continuation; never replayed')
            return
        delivery = current[3].deliver(proof.data['payload'], proof=proof,
                    verify=lambda: _source(seat, proof, 'off') is not None)
        proof.finish(delivery.result)
        attempt = next(a for a in service.attempts.all() if a.path.name == proof.data['attempts'][0])
        service.attempts.close(attempt, delivery.result, delivery.detail)
        _save(proof, mode='submitted' if delivery.result in ('delivered', 'pending', 'blind', 'queued') else 'held',
              error=None if delivery.result == 'delivered' else str(delivery))
        service.log('thinking-recovery', seat=seat.name, result=delivery.result, message=meta['post'])
    _restore(seat, proof)


def run(service):
    from . import pending
    for seat in service.resolved_seats():
        try:
            _seat(service, seat, pending)
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            service.log('error', step='thinking-recovery', seat=seat.name, error=str(error))
            proofs, errors = pending.journals(seat)
            if not errors:
                for proof in proofs:
                    if 'recovery' in proof.data and proof.data['recovery']['mode'] != 'done':
                        _save(proof, error='fresh recovery evidence unavailable: ' + str(error))


def _seat(service, seat, pending):
    proofs, errors = pending.journals(seat)
    if errors or seat.session.human:
        return
    active = [p for p in proofs if 'recovery' in p.data and p.data['recovery']['mode'] != 'done']
    if active:
        for proof in active:
            _advance(service, seat, proof)
        return
    current = _current(seat)
    if current is None:
        return
    client, state, screen, _ = current
    candidate = state.capped
    if candidate is None or not client.eligible(candidate) or client.mode(state, screen) != 'original':
        return
    for proof in proofs:
        meta = proof.data.get('recovery')
        if (meta is not None and proof.data['record'] == str(seat.record())
                and meta['source']['conversation'] == candidate.conversation
                and (candidate.work in (meta['source']['work'], meta['recovery_input'])
                     or state.last_input == proof.data['payload'])):
            return  # original work, recovery input and native continuation share one consumed allowance
    at = post.now()
    notice = post.Message(service.store._new_id('report', post.SHIP, at), 'report', post.SHIP, seat.name, at, client.continuation,
                          re=service.store.claimed(seat.name, at))
    launch = seat.launched()
    attempt = service.attempts.open(notice, seat.name, launch.pane, launch.pid, launch.at)
    proof = pending.Proof.begin(seat, attempt, post.typed([notice]))
    _save_initial = dict(format=1, source=asdict(candidate), client=client.signature, evidence=launch.recovery, mode='claimed',
                         post=notice.id, recovery_input=None, error=None)
    validate(_save_initial)
    proof.save(recovery=_save_initial)  # durable once claim BEFORE publication or any control
    service.store.write(notice)
    _advance(service, seat, proof, new=True)
