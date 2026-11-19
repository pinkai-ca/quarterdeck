"""A running launch keeps its program when its pool leaves the declaration."""

from dataclasses import replace

import pytest

from quarterdeck import cli, credential, post, quota, ship as shipmod, watch
from quarterdeck.harness.claude import ClaudeHarness
from quarterdeck.harness.codex import CodexHarness
from quarterdeck.harness.types import Screen
from quarterdeck.seat import EXPLICIT, Seat, SelectionError
from quarterdeck.service import Service
from quarterdeck.ship import ShipError
from .test_cross_program_failover import usage
from .test_pool_choices import declaration
from .test_saved_pool_selection import migrated  # noqa: F401 (fixture)


CHOICES = ('[{pool="second",harness="claude",model="synthetic-target"}, '
           '{pool="third",harness="claude",model="synthetic-other"}]')


@pytest.fixture
def withdrawn(migrated, monkeypatch, request):
    r = migrated
    mode = getattr(request, 'param', 'choices')
    (r.worker.trusted / 'pool').write_text('first\n')
    if mode == 'plain':
        raw = r.ship.file.read_text().split('\n[seats.healthy]', 1)[0]
        for key, value in [('harness', 'claude'), ('effort', 'low'), ('pool', 'second')]:
            raw = shipmod.edit(raw, 'seats.worker', key, value)
        r.ship.file.write_text(raw)
    else:
        declaration(r.ship.folder, CHOICES)
        if mode == 'missing-pool':
            r.ship.file.write_text(r.ship.file.read_text().replace('[pools.first]\nprovider="openai"\n', ''))
            (r.worker.trusted / 'pool').unlink()
    r.ship = shipmod.load(r.ship.folder)
    r.worker = Seat(r.ship, r.ship.seat('worker'), r.tmux)
    monkeypatch.setattr(CodexHarness, 'screen', lambda self, raw: Screen(raw, '', working=False))
    r.svc = Service(r.ship, r.tmux)
    try:
        yield r
    finally:
        r.svc.ledger.close()


@pytest.mark.usefixtures('synthetic_disk_room')
@pytest.mark.parametrize('withdrawn', ['choices', 'missing-pool', 'plain'], indirect=True)
def test_status_names_the_running_program_and_holds_automatic_moves(withdrawn, monkeypatch):
    r = withdrawn
    launch = r.worker.launched()
    usage(r.svc, 'first', 1.0)
    usage(r.svc, 'second', 0.0)
    usage(r.svc, 'third', 0.0)
    assert (r.worker.pool.name, r.worker.harness.name, r.worker.record()) == ('first', 'codex', r.source.path)
    assert quota.candidate_pools(r.worker) == ()
    monkeypatch.setattr(r.svc, 'known_open', lambda *a: pytest.fail('automatic move looked for a destination'))
    r.svc.failover()
    assert r.worker.is_up and r.worker.launched() == launch and not r.launches
    hold = "worker: automatic pool move held; launch pool 'first' is no longer declared"
    reading = watch.status_reading(r.ship, r.tmux)
    [worker] = reading['seats']
    assert (worker.pool, worker.harness, worker.activity.state) == ('first', 'codex', 'idle')
    assert hold in watch.line(r.ship, r.tmux)
    assert any(hold in row for row in watch.render_table(reading))
    assert credential.source_refusal(r.ship, r.worker), 'the observed pool grants no credential eligibility'
    with pytest.raises(ShipError, match='automatic pool move held'):
        r.worker.fresh()
    assert r.worker.launched() == launch and not r.launches


@pytest.mark.parametrize('withdrawn', ['choices', 'plain'], indirect=True)
def test_explicit_selection_uses_a_declared_destination(withdrawn, monkeypatch):
    r = withdrawn
    original = r.ship.file.read_bytes()
    launch = r.worker.launched()
    usage(r.svc, 'second', 0.0)
    monkeypatch.setattr(cli.service, 'request', lambda runtime, kind, **data: r.svc.answer({'kind': kind, **data}))
    with pytest.raises(ShipError, match='not one of its declared choices'):
        cli.set_seat(r.ship, 'worker', ['pool=first'])
    assert 'not one of its declared choices' in r.svc.answer({'kind': 'select', 'seat': 'worker', 'pool': 'first'})
    assert r.worker.launched() == launch and not r.launches
    [proof] = cli.set_seat(r.ship, 'worker', ['pool=second'])
    assert 'moved to second from first' in proof
    successor = Seat(r.ship, r.ship.seat('worker'), r.tmux)
    assert (successor.launched().pool, successor.launched().harness, successor.launched().resume) == ('second', 'claude', None)
    assert successor.pool_move_hold is None and r.ship.file.read_bytes() == original


def test_fresh_up_uses_a_declared_choice_after_retiring_the_old_launch(withdrawn):
    r = withdrawn
    r.worker.down()
    seat = Seat(r.ship, r.ship.seat('worker'), r.tmux, launching=True)
    with pytest.raises(ShipError, match='not one of its declared choices'):
        seat.up(resume=False, pool='first')
    assert seat.launched() is None and not r.launches
    proof = seat.up(resume=False, pool='third')
    assert 'launched on third' in proof
    assert (seat.launched().pool, seat.launched().harness, seat.launched().resume) == ('third', 'claude', None)


@pytest.mark.parametrize('withdrawn', ['plain'], indirect=True)
def test_explicit_fresh_applies_the_new_plain_declaration(withdrawn):
    r = withdrawn
    assert 'up in' in cli.relaunch(r.ship, 'worker', False)
    launch = r.worker.launched()
    assert (launch.pool, launch.harness, launch.resume) == ('second', 'claude', None)


def test_explicit_fresh_ignores_a_marker_for_the_withdrawn_pool(withdrawn):
    r = withdrawn
    marker = r.worker.trusted / EXPLICIT
    marker.write_text('first\n')
    before = r.worker.launched()
    assert 'up in' in cli.relaunch(r.ship, 'worker', False)
    launch = r.worker.launched()
    assert launch.pane != before.pane
    assert (launch.pool, launch.harness, launch.resume) == ('second', 'claude', None)
    assert not marker.exists()


def test_post_reaches_the_original_program_and_is_confirmed_by_its_record(withdrawn, monkeypatch):
    r = withdrawn
    launch = r.worker.launched()
    box, pasted, presses = '', [], []

    def capture(pane, **options):
        assert pane == launch.pane
        return box

    def paste(pane, text):
        nonlocal box
        assert pane == launch.pane and not box
        box = text
        pasted.append(text)

    def submit(pane, *keys):
        nonlocal box
        assert pane == launch.pane and keys == ('Enter',) and box
        presses.append(keys)
        r.source.begin(box, 'synthetic-source', 'high')
        r.source.end('Synthetic reply to the delivered post')
        box = ''

    r.tmux.capture, r.tmux.paste, r.tmux.keys = capture, paste, submit
    monkeypatch.setattr(CodexHarness, 'screen', lambda self, raw: Screen('Codex composer', raw, working=False))
    monkeypatch.setattr(ClaudeHarness, 'screen', lambda *a: pytest.fail('the successor read the running pane'))
    message = r.svc.store.new('order', 'issuer', 'worker', 'Synthetic work for the running program')
    r.svc.deliveries()
    r.svc.deliveries()
    [attempt] = r.svc.attempts.for_message(message.id)
    assert pasted == [post.typed([message])] and presses == [('Enter',)]
    assert (attempt.pane, attempt.pid, attempt.launched_at, attempt.result) == (launch.pane, launch.pid, launch.at, 'delivered')
    assert r.worker.launched() == launch and not r.launches


def test_conflicting_selection_is_still_held_and_a_replaced_pane_is_unread(withdrawn):
    r = withdrawn
    launch = r.worker.launched()
    (r.worker.trusted / 'pool').write_text('second\n')
    with pytest.raises(SelectionError, match='saved launch and selected pool disagree'):
        Seat(r.ship, r.ship.seat('worker'), r.tmux)
    r.live[launch.pane] = replace(r.live[launch.pane], pid=launch.pid + 1)
    assert r.worker.pane() is None and r.worker.read_pane() is None
