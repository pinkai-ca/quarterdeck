"""One command moves an actual stand-in process, with only synthetic billing data."""

from datetime import timedelta
import tomllib

import pytest

from quarterdeck import cli, drill, idle, ledger, post, quota, record_owner, request_queue, service, ship as shipmod, trusted_files
from quarterdeck.harness.fake import FakeHarness
from quarterdeck.harness.limits import Usage, Window
from quarterdeck.harness.synthetic_control import Rendezvous, events
from quarterdeck.seat import EXPLICIT, Seat
from .progress import service_progress
from .waiting import wait_for


KEY = 'sk-synthetic-pool-selection-test'
SEAT = 'captain'


@pytest.fixture(params=['simple', 'inline'])
def ship(request, drydock, tmux, monkeypatch, key_file):
    key_file.write(paid=KEY)
    file = drydock / 'ship.toml'
    text = file.read_text() + '\n[pools.paid]\nprovider="synthetic"\napi_key="keys:paid"\n'
    # A declared unused subscription with real headline windows and an isolated synthetic sign-in directory.
    text += '\n[pools.standby]\nprovider="openai"\nconfig_dir="no-account"\n'
    pools = ['fake', 'paid']
    if request.param == 'inline':
        pools[1] = {'pool': 'paid', 'harness': 'fake', 'model': 'fake-1'}
    text = shipmod.edit(text, f'seats.{SEAT}', 'pool', pools)
    file.write_text(shipmod.edit(text, 'seats.desk', 'pool', pools))
    monkeypatch.setenv('QD_TMUX_SOCKET', tmux.socket)
    monkeypatch.setenv('QD_SHIP', str(drydock))
    monkeypatch.delenv('QD_SEAT', raising=False)
    tmux.new_session('Synthetic bootstrap')
    tmux.kill_session('Synthetic bootstrap')
    ship = shipmod.load(drydock)
    try:
        yield ship
    finally:
        service.stop(ship.runtime)


def qd(capsys, *args, code=0):
    result = cli.main(list(args))
    out = capsys.readouterr()
    assert result == code, (out.out, out.err)
    return out.out if code == 0 else out.err


def turn(seat, tmux, words):
    pane = seat.pane()
    tmux.paste(pane.id, words)
    tmux.keys(pane.id, 'Enter')


def assert_turns_once(seat, record, since, expected):
    submitted = [event['text'] for event in events(seat.home)
                 if event['event'] == 'submitted' and event['text'] in expected]
    assert submitted == expected, 'simulator submissions'
    inputs = seat.harness.inputs(record)
    for text in expected:
        assert drill.acceptances(inputs, text, since) == 1, f'record acceptance for {text!r}'


def at_rest(seat):
    state, hold = idle.reading(seat)
    return state if state is not None and hold is None else None


def allowance(ship, used):
    now = post.now()
    with ledger.Ledger(ship.runtime / 'ledger.sqlite') as book:
        book.record_usage('fake', Usage((Window('5h', used, now + timedelta(hours=1)),), now))


def test_a_live_choice_waits_and_keeps_the_record_both_ways(ship, tmux, capsys, monkeypatch):
    before = ship.file.read_bytes()
    qd(capsys, 'up', SEAT)
    def current():
        return Seat(ship, ship.seat(SEAT), tmux)

    seat = current()
    original = seat.launched()
    allowance(ship, 0.0)
    initial, on_key, on_subscription = (
        'initial synthetic turn', 'synthetic turn on the key', 'synthetic turn on the subscription',
    )
    since = post.now()
    with Rendezvous(seat.home, 'step', initial) as gate:
        turn(seat, tmux, initial)
        wait_for(gate.reached)
        record = wait_for(seat.record)
        assert seat.harness.state(record).turn_open
        reply = qd(capsys, 'set', SEAT, 'pool=paid')
        assert 'accepted:' in reply and 'move to pool paid' in reply
        assert 'move to pool paid: a turn is open' in qd(capsys, 'status')
        assert 'move to pool paid: a turn is open' in qd(capsys, 'watch', SEAT)
        assert current().launched() == original
        gate.release()

    paid = wait_for(lambda: (launch := current().launched()) and launch.pool == 'paid' and launch)
    service_progress(ship, seconds=20)
    assert current().launched() == paid and paid.pool_selected == 'paid'
    assert paid.resume == seat.harness.session_id(record) and current().record() == record
    with monkeypatch.context() as display:
        display.setattr(FakeHarness, 'reads_usage', True)
        display.setattr(FakeHarness, 'headline_window', (None, '7d'))
        detail = qd(capsys, 'watch', SEAT)
        assert 'pool paid' in detail and 'usage unknown' not in detail
        fleet = qd(capsys, 'status')
        assert 'fake 7d' not in fleet
        assert 'standby 5h usage unknown' in fleet
        qd(capsys, 'up', 'worker-1')
        assert 'fake 7d' in qd(capsys, 'status')
        assert 'fake 7d' not in qd(capsys, 'watch', SEAT)
        assert 'standby latest usage read:' in qd(capsys, 'ledger')
        assert 'usage unknown' not in qd(capsys, 'status', '--line')

    seat = current()
    wait_for(lambda: at_rest(seat) and any('moved to pool paid from fake' in i.text for i in seat.harness.inputs(record)))
    turn(seat, tmux, on_key)
    wait_for(lambda: (state := at_rest(seat)) and state.last_input == on_key)
    with Rendezvous(seat.home, 'step', 'moved to pool fake from paid') as notice:
        assert 'moved to fake from paid' in qd(capsys, 'set', SEAT, 'pool=fake')
        restored = current()
        assert restored.launched().pool == 'fake' and restored.launched().pool_selected is None
        assert restored.launched().resume == paid.resume and restored.record() == record
        wait_for(notice.reached)
        # A queued input can have both a dequeue entry and a user echo for one acceptance.
        turn(restored, tmux, on_subscription)
        wait_for(lambda: restored.harness.state(record).queued == (on_subscription,))
        notice.release()
    wait_for(lambda: (state := at_rest(restored)) and state.last_input == on_subscription)
    expected = [initial, on_key, on_subscription]
    assert_turns_once(restored, record, since, expected)
    assert not request_queue.pending(ship.runtime, SEAT)
    assert ship.file.read_bytes() == before

    # Submit the same text again and wait for that new turn to run before checking.
    with Rendezvous(restored.home, 'step', on_subscription) as duplicate:
        turn(restored, tmux, on_subscription)
        wait_for(duplicate.reached)
        duplicate.release()
    wait_for(lambda: at_rest(restored))
    with pytest.raises(AssertionError, match='simulator submissions'):
        assert_turns_once(restored, record, since, expected)


def test_a_later_pending_choice_replaces_the_first_on_the_same_launch(ship, tmux, capsys):
    text = ship.file.read_text()
    pools = tomllib.loads(text)['seats'][SEAT]['pool']
    text += '\n[pools.reserve]\nprovider="synthetic"\napi_key="keys:paid"\n'
    ship.file.write_text(shipmod.edit(text, f'seats.{SEAT}', 'pool', [*pools, 'reserve']))
    ship = shipmod.load(ship.folder)
    qd(capsys, 'up', SEAT)

    def current():
        return Seat(ship, ship.seat(SEAT), tmux)

    seat = current()
    original = seat.launched()
    allowance(ship, 0.0)
    with Rendezvous(seat.home, 'step', 'synthetic turn during both requests') as gate:
        turn(seat, tmux, 'synthetic turn during both requests')
        wait_for(gate.reached)
        record = wait_for(seat.record)
        assert 'accepted:' in qd(capsys, 'set', SEAT, 'pool=paid')
        [first] = request_queue.pending(ship.runtime, SEAT)
        later = request_queue.enqueue(
            ship.runtime, 'select', dict(seat=SEAT, pool='reserve', origin=request_queue.launch_binding(seat)),
            name='test-later-selection',
        )
        old_answer = later.with_name(first['id'] + '.done')
        wait_for(old_answer.exists)
        assert old_answer.read_text() == (
            f"{SEAT}: select request {first['id']} superseded by {later.stem}; never applied by this request\n"
        )
        service_progress(ship, seconds=20)
        [pending] = request_queue.pending(ship.runtime, SEAT)
        assert pending['id'] == later.stem and pending['dimension'] == 'turn-open'
        assert current().launched() == original
        gate.release()

    final = later.with_suffix('.done')
    wait_for(final.exists)
    answer = final.read_text()
    assert answer.splitlines()[0] == f'{SEAT} moved to reserve from fake'
    assert f"retired pending select request(s): {first['id']}" in answer
    moved = current()
    launch = moved.launched()
    assert launch.pool == launch.pool_selected == 'reserve'
    assert launch.resume == seat.harness.session_id(record) and moved.record() == record
    service_progress(ship, seconds=20)
    assert current().launched() == launch and not request_queue.pending(ship.runtime, SEAT)
    events = record_owner.events(moved.trusted / trusted_files.LAUNCHES)
    assert [(event['at'], event['pool']) for event in events if event['record'] == str(record)] == [
        (original.at, 'fake'), (launch.at, 'reserve'),
    ]


def test_automatic_fallback_remains_eligible_to_return(ship, tmux, capsys):
    qd(capsys, 'up', SEAT)
    def current():
        return Seat(ship, ship.seat(SEAT), tmux)

    seat = current()
    turn(seat, tmux, 'initial turn for automatic fallback')
    wait_for(lambda: at_rest(seat))
    record = seat.record()
    allowance(ship, 1.0)
    paid = wait_for(lambda: (launch := current().launched()) and launch.pool == 'paid' and launch)
    assert paid.pool_selected is None and current().record() == record
    allowance(ship, 0.0)
    restored = wait_for(lambda: (launch := current().launched()) and launch.pool == 'fake' and launch)
    assert restored.resume == paid.resume and current().record() == record


def test_refusal_keeps_the_live_seat_and_down_selection_is_one_launch(ship, tmux, capsys, key_file):
    before = ship.file.read_bytes()
    qd(capsys, 'up', SEAT)
    def current():
        return Seat(ship, ship.seat(SEAT), tmux)

    seat = current()
    turn(seat, tmux, 'initial turn before a refused move')
    wait_for(lambda: at_rest(seat))
    original, record = seat.launched(), seat.record()
    assert 'not one of its declared choices' in qd(capsys, 'set', SEAT, 'pool=undeclared', code=2)
    key_file.write(other=KEY)
    assert 'key paid is not in' in qd(capsys, 'set', SEAT, 'pool=paid')
    assert current().launched() == original and current().record() == record
    assert request_queue.pending(ship.runtime, SEAT) == []

    qd(capsys, 'down', SEAT)
    assert 'key paid is not in' in qd(capsys, 'set', SEAT, 'pool=paid', code=2)
    key_file.write(paid=KEY)
    assert 'pool paid chosen for its next launch' in qd(capsys, 'set', SEAT, 'pool=paid')
    assert current().launched() is None
    key_file.write(other=KEY)
    assert 'key paid is not in' in qd(capsys, 'up', SEAT, code=2)
    assert current().launched() is None
    assert (current().trusted / EXPLICIT).read_text() == 'paid\n'
    key_file.write(paid=KEY)
    allowance(ship, 0.0)
    assert 'chosen for this launch by qd set' in qd(capsys, 'up', SEAT)
    paid = current().launched()
    assert paid.pool == paid.pool_selected == 'paid'
    assert current().record() == record
    wait_for(lambda: at_rest(current()))
    with ledger.Ledger.reading(ship.runtime / 'ledger.sqlite') as book:
        assert book.usage_known('fake') and quota.cached_open(ship, book, 'fake', current())
    service_progress(ship, seconds=20)
    assert current().launched() == paid
    assert not (current().trusted / EXPLICIT).exists()
    qd(capsys, 'down', SEAT)
    qd(capsys, 'up', SEAT)
    assert current().launched().pool == 'fake'
    assert ship.file.read_bytes() == before


def test_up_pool_keeps_the_selected_key_even_with_room_on_a_subscription(ship, tmux, capsys, key_file):
    seat = Seat(ship, ship.seat(SEAT), tmux)
    key_file.write(other=KEY)
    assert 'key paid is not in' in qd(capsys, 'up', SEAT, '--pool', 'paid', code=2)
    assert seat.launched() is None and not (seat.trusted / EXPLICIT).exists()
    key_file.write(paid=KEY)
    allowance(ship, 0.0)
    assert 'chosen for this launch by qd up --pool' in qd(capsys, 'up', SEAT, '--pool', 'paid')
    seat = Seat(ship, ship.seat(SEAT), tmux)
    paid = seat.launched()
    assert paid.pool == paid.pool_selected == 'paid'
    turn(seat, tmux, 'synthetic turn on the explicitly launched key')
    wait_for(lambda: at_rest(seat))
    with ledger.Ledger.reading(ship.runtime / 'ledger.sqlite') as book:
        assert book.usage_known('fake') and quota.cached_open(ship, book, 'fake', seat)
    service_progress(ship, seconds=20)
    assert seat.launched() == paid
    assert not (seat.trusted / EXPLICIT).exists()


def test_recorded_relaunch_destination_does_not_mark_a_key_as_selected(ship, tmux):
    seat = Seat(ship, ship.seat(SEAT), tmux)
    with seat._lifecycle():
        seat._up_on_recorded_pool('paid', 'fake', resume=False)
    launched = seat.launched()
    assert launched.pool == 'paid' and launched.pool_selected is None


@pytest.mark.parametrize('how', ['set', 'up'])
def test_explicit_single_key_pool_launch_is_marked(ship, tmux, capsys, how):
    ship.file.write_text(shipmod.edit(ship.file.read_text(), 'seats.worker-1', 'pool', 'paid'))
    ship = shipmod.load(ship.folder)
    seat = Seat(ship, ship.seat('worker-1'), tmux)
    if how == 'set':
        qd(capsys, 'set', seat.name, 'pool=paid')
        qd(capsys, 'up', seat.name)
    else:
        qd(capsys, 'up', seat.name, '--pool', 'paid')
    started = seat.launched()
    assert started.pool == started.pool_selected == 'paid'
    assert not (seat.trusted / EXPLICIT).exists()


def test_a_persons_queued_turn_and_draft_keep_their_launch(ship, tmux, capsys):
    qd(capsys, 'up', 'desk')
    def current():
        return Seat(ship, ship.seat('desk'), tmux)

    seat = current()
    original = seat.launched()
    with Rendezvous(seat.home, 'step', 'first human turn') as first, \
            Rendezvous(seat.home, 'turn', 'queued human turn') as queued:
        turn(seat, tmux, 'first human turn')
        wait_for(first.reached)
        record = wait_for(seat.record)
        turn(seat, tmux, 'queued human turn')
        wait_for(lambda: seat.harness.state(record).queued == ('queued human turn',))
        assert 'accepted:' in qd(capsys, 'set', 'desk', 'pool=paid')
        first.release()
        wait_for(queued.reached)
        tmux.paste(seat.pane().id, 'a human draft')
        queued.release()
        wait_for(lambda: at_rest(seat))
        wait_for(lambda: any('box is not proved empty' in row['reason']
                             for row in request_queue.pending(ship.runtime, 'desk')))
        assert current().launched() == original
        assert seat.harness.screen(tmux.capture(seat.pane().id)).box == 'a human draft'
        tmux.keys(seat.pane().id, 'C-u')
    paid = wait_for(lambda: (launch := current().launched()) and launch.pool == 'paid' and launch)
    assert paid.resume == seat.harness.session_id(record) and current().record() == record
    assert any(entry.text == 'queued human turn' for entry in current().harness.inputs(record))
