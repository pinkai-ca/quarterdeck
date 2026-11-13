"""Ordinary request files and presentation; human/lifecycle permission gates run on Claude."""

from datetime import datetime, timezone
import json
from itertools import count
from types import SimpleNamespace

import pytest

from quarterdeck import cli, durable, post, request_queue, servicectl, watch
from quarterdeck.service import Service
from quarterdeck.ship import ShipError


AT = datetime(2032, 4, 5, 6, 7, tzinfo=timezone.utc)
SEAT = 'seat-orchid'
IDS = count()


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(post, 'now', lambda: AT)
    return tmp_path / 'runtime'


def enqueue(runtime, kind='relaunch', **extra):
    data = dict(seat=SEAT, resume=True, **extra)
    if kind == 'relaunch':
        data['caller'] = SEAT
    return request_queue.enqueue(runtime, kind, data,
                                 name=f'request-orchid-{next(IDS)}-zz')


def service(runtime, answer):
    result = Service.__new__(Service)
    result.runtime = runtime
    result.answer = answer
    result.log = lambda *args, **kwargs: None
    return result


def test_listing_absent_queue_is_read_only(runtime):
    assert request_queue.pending(runtime) == []
    assert not runtime.exists()


def test_legacy_publication_keeps_one_original_payload_write(runtime, monkeypatch):
    writes = []
    replace = durable.replace_file

    def record(path, text):
        writes.append((path, text))
        replace(path, text)

    monkeypatch.setattr(durable, 'replace_file', record)
    path = request_queue.enqueue(runtime, 'control', dict(seat=SEAT, value='synthetic value'),
                                 name='request-orchid-legacy-zz')
    assert writes == [(path, json.dumps(dict(kind='control', seat=SEAT, value='synthetic value')) + '\n')]
    assert not request_queue.state_path(path).exists()
    assert request_queue.pending(runtime)[0]['since'] == request_queue.created(path, {})


def test_wait_metadata_names_kind_since_reason_and_cancel_without_arguments(runtime):
    path = enqueue(runtime, 'control', value='synthetic private setting')
    with request_queue.coordination(runtime):
        data = request_queue.metadata(path)
        request_queue.hold(path, data, 'tools-unknown', 'tool state unknown')
    [row] = request_queue.pending(runtime)
    text = request_queue.describe(row)
    assert row['kind'] == 'control' and row['since'] == request_queue.created(path, request_queue.metadata(path))
    assert row['reason'] == 'tool state unknown' and not row['claimed']
    assert path.stem in text and 'qd restart seat-orchid --cancel' in text
    assert 'synthetic private setting' not in text and 'value' not in row


def test_service_wait_is_visible_and_cancelled_request_is_settled(runtime):
    path = enqueue(runtime)
    raw = path.read_text()
    calls = []
    runner = service(runtime, None)

    def wait(data):
        assert set(data) == {'kind', 'seat', 'resume'}
        calls.append(data['kind'])
        runner.request_wait = ('tools-unknown', 'tool state unknown')
        return None

    runner.answer = wait
    runner.requests()
    [row] = request_queue.pending(runtime)
    assert row['reason'] == 'tool state unknown'
    assert path.read_text() == raw
    assert not path.with_suffix('.claimed').exists()
    proof = request_queue.cancel(runtime, SEAT, path.stem)
    runner.requests()
    assert calls == ['relaunch']
    assert 'cancelled' in proof and path.stem in proof
    assert not path.exists() and 'cancelled' in path.with_suffix('.done').read_text()


def test_superseding_request_settles_previous_one_with_same_owner(runtime):
    first = enqueue(runtime)
    second = enqueue(runtime)
    assert first != second and not first.exists()
    assert 'superseded' in first.with_suffix('.done').read_text()
    assert request_queue.metadata(second)['retired'] == [first.stem]
    assert [row['id'] for row in request_queue.pending(runtime)] == [second.stem]


def test_claimed_outcome_unknown_is_retained(runtime):
    path = enqueue(runtime)
    path.with_suffix('.claimed').write_text(path.read_text())
    with pytest.raises(ShipError, match='outcome unknown'):
        request_queue.cancel(runtime, SEAT, path.stem)
    assert path.exists() and path.with_suffix('.claimed').exists()
    assert not path.with_suffix('.done').exists()
    assert request_queue.pending(runtime)[0]['claimed']


def test_cancel_intent_recovers_after_interrupted_settlement(runtime, monkeypatch):
    path = enqueue(runtime)
    settle = request_queue.settle

    def interrupted(*args, **kwargs):
        raise OSError('synthetic interrupted settlement')

    monkeypatch.setattr(request_queue, 'settle', interrupted)
    with pytest.raises(OSError, match='interrupted settlement'):
        request_queue.cancel(runtime, SEAT, path.stem)
    assert request_queue.metadata(path)['terminal']
    assert path.with_suffix('.claimed').exists()
    monkeypatch.setattr(request_queue, 'settle', settle)
    runner = service(runtime, lambda data: pytest.fail('terminal request was dispatched'))
    runner.requests()
    assert not path.exists() and not path.with_suffix('.claimed').exists()
    assert 'cancelled' in path.with_suffix('.done').read_text()


def test_legacy_request_since_survives_wait_rewrites(runtime, monkeypatch):
    path = enqueue(runtime)
    request_queue.state_path(path).unlink()
    data = request_queue.metadata(path)
    before = request_queue.created(path, data)
    with request_queue.coordination(runtime):
        request_queue.hold(path, data, 'turn-open', 'a turn is open')
    monkeypatch.setattr(post, 'now', lambda: AT.replace(minute=8))
    with request_queue.coordination(runtime):
        request_queue.hold(path, request_queue.metadata(path), 'tools-unknown', 'tool state unknown')
    assert request_queue.pending(runtime)[0]['since'] == before


def test_timeout_uses_service_wait_reason(runtime, monkeypatch):
    real_enqueue = request_queue.enqueue

    def waiting(*args, **kwargs):
        path = real_enqueue(*args, **kwargs)
        with request_queue.coordination(runtime):
            request_queue.hold(path, request_queue.metadata(path), 'tools-unknown', 'tool state unknown')
        return path

    monkeypatch.setattr(request_queue, 'enqueue', waiting)
    monkeypatch.setattr(servicectl, 'alive', lambda target: True)
    monkeypatch.setattr(servicectl, 'stale', lambda target: None)
    monkeypatch.setattr(servicectl, 'ANSWER_SECONDS', 0)
    proof = servicectl.request(runtime, 'relaunch', seat=SEAT, resume=True)
    assert 'pending:' in proof and 'tool state unknown' in proof
    assert 'since ' in proof and '--cancel' in proof


def test_status_overflow_keeps_request_and_reason(runtime):
    path = enqueue(runtime)
    row = request_queue.view(path)
    row.update(reason='tool state unknown')
    facts = watch.Facts(service='ok', waiting=0, oldest=0, blind=0,
                        pending=['synthetic detail ' * 40],
                        request_waits=[request_queue.describe(row)])
    text = facts.line()
    assert SEAT in text and path.stem in text and 'tool state unknown' in text and '--cancel' in text


@pytest.mark.parametrize('verb', ['restart', 'fresh'])
def test_existing_acts_cancel_by_id_without_resolving_a_program(runtime, monkeypatch, verb):
    path = enqueue(runtime)
    ship = SimpleNamespace(runtime=runtime, seat=lambda name: SimpleNamespace(name=name))
    monkeypatch.setattr(cli, 'load_ship', lambda args: ship)
    monkeypatch.setattr(cli, 'seat_of', lambda *args: pytest.fail('cancel resolved a native program'))
    args = cli.build_parser().parse_args([verb, SEAT, '--cancel', path.stem])
    assert 'cancelled' in args.run(args)
    assert not path.exists()


def test_bare_cancel_retires_relaunches_not_other_requests(runtime):
    relaunch = enqueue(runtime)
    control = enqueue(runtime, 'control')
    proof = request_queue.cancel(runtime, SEAT)
    assert relaunch.stem in proof and not relaunch.exists()
    assert control.exists()


def test_direct_act_retires_queued_relaunch_before_existing_manual_action(runtime, monkeypatch):
    path = enqueue(runtime)

    def restart():
        assert not path.exists()
        assert 'superseded' in path.with_suffix('.done').read_text()
        return 'synthetic direct restart'

    ship = SimpleNamespace(runtime=runtime, seat=lambda name: SimpleNamespace(name=name))
    seat = SimpleNamespace(name=SEAT, is_working=lambda: False, restart=restart)
    monkeypatch.setattr(cli, 'seat_of', lambda *args: seat)
    proof = cli.relaunch(ship, SEAT, True)
    assert proof.startswith('synthetic direct restart') and path.stem in proof


def test_direct_act_rechecks_work_after_coordination_and_keeps_asking_time(runtime, monkeypatch):
    path = enqueue(runtime)
    working = iter([False, True])
    ship = SimpleNamespace(runtime=runtime, seat=lambda name: SimpleNamespace(name=name))
    seat = SimpleNamespace(name=SEAT, is_working=lambda: next(working),
                           launched=lambda: None,
                           restart=lambda: pytest.fail('direct action after work appeared'))
    monkeypatch.setattr(cli, 'seat_of', lambda *args: seat)
    observed = []

    def request(target, kind, **data):
        observed.append((target, kind, data))
        assert not path.exists()
        return 'synthetic queued answer'

    monkeypatch.setattr(cli.service, 'request', request)
    proof = cli.relaunch(ship, SEAT, True)
    assert proof.startswith('synthetic queued answer') and path.stem in proof
    assert len(observed) == 1 and observed[0][:2] == (runtime, 'relaunch')
    assert observed[0][2]['requested_at'] == AT.isoformat()


def test_unknown_time_keeps_known_target_visible_and_withdrawable(runtime):
    path = enqueue(runtime)
    data = request_queue.metadata(path)
    data['created_at'] = 'synthetic unknown time'
    request_queue.state_path(path).write_text(json.dumps(data))
    [row] = request_queue.pending(runtime, SEAT)
    assert row['seat'] == SEAT and row['since'] is None and row['claimed'] is False
    assert 'unknown' in row['dimension'] and path.stem in request_queue.describe(row)
    assert 'cancelled' in request_queue.cancel(runtime, SEAT, path.stem)


def test_complete_answer_precedes_log_and_original_retirement(runtime):
    path = enqueue(runtime)
    raw = path.read_text()
    proof = 'synthetic complete answer\n' * 100
    runner = service(runtime, lambda data: proof)
    observed = []

    def log(*args, **kwargs):
        observed.append(path.with_suffix('.done').read_text())
        assert path.read_text() == raw
        assert path.with_suffix('.claimed').exists()

    runner.log = log
    runner.requests()
    assert observed == [proof + '\n']
    assert path.with_suffix('.done').read_text() == proof + '\n'
    assert not path.exists() and not path.with_suffix('.claimed').exists()
    assert not request_queue.state_path(path).exists()
