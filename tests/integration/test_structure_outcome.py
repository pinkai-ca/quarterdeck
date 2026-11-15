"""Ordinary fake pi programs through real doors and distinct service passes."""

from itertools import count
import shutil
from pathlib import Path

import pytest

from quarterdeck import cascade, group_post, post, service, ship as shipmod, structure_outcome
from quarterdeck.seat import Seat
from .waiting import wait_for

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def setup_structure(drydock, tmux, monkeypatch):
    serial = count()
    monkeypatch.setattr(post.Store, '_new_id', lambda self, kind, sender, at, **_: f'{kind[:3]}-synthetic-z{next(serial)}')
    shutil.copytree(ROOT / 'ships/cascade/roles', drydock / 'roles', dirs_exist_ok=True)
    shutil.copyfile(ROOT / 'ships/forum/roles/reviewer.md', drydock / 'roles/reviewer.md')
    (drydock / 'work').mkdir(exist_ok=True)

    def setup(kind, reason):
        text = '''[ship]
name = "synthetic-outcomes"
operator = "operator"
workspace = "work"
[pools.fake]
provider = "synthetic"
[sessions.crew]
title = "Synthetic outcomes"
'''
        text += ('''[groups.reviewers]
lead = "lead"
members = ["alpha"]
''' if kind == 'forum' else '''[cascade]
seats = ["alpha", "beta", "gamma"]
orchestrator = "conductor"
efforts = ["low"]
''')
        for name in ('alpha', 'beta', 'gamma', 'lead', 'conductor'):
            text += f'''[seats.{name}]
session = "crew"
role = "reviewer"
harness = "fake"
pool = "fake"
model = "synthetic-model"
effort = "low"
[seats.{name}.settings]
as = "pi"
steps = "0"
end_reason = "{reason}"
'''
        (drydock / 'ship.toml').write_text(text)
        ship = shipmod.load(drydock)
        svc = service.Service(ship, tmux)
        seat = Seat(ship, ship.seat('alpha'), tmux)
        seat.up(resume=False)  # only a private fake; no background service is started
        if kind == 'forum':
            root, copies = group_post.submit(ship, svc.store, 'reviewers', 'operator', 'Review a fictional plan.')
            order = copies[0]
        else:
            root = svc.store.new('question', 'operator', 'conductor', 'Improve a fictional plan.')
            cascade.start(svc, root.id, 'conductor')
            svc.cascades()
            path = cascade.folder(ship.runtime, root.id)
            order = svc.store.get(cascade.load(path)['steps'][-1]['order'])
        svc.reconcile()
        svc.deliveries()

        def idle():
            record = seat.record()
            if record is None:
                return False
            state = seat.harness.state(record)
            pane = seat.pane()
            screen = seat.harness.screen(tmux.capture(pane.id)) if pane else None
            return (state.completed_turns and state.turn_open is False and screen is not None
                    and screen.working is False and screen.box == '' and not screen.queued)
        assert wait_for(idle, seconds=15)
        assert any(a.result == 'delivered' for a in svc.attempts.for_message(order.id))
        return ship, svc, seat, root, order
    return setup


@pytest.mark.parametrize('kind', ['forum', 'cascade'])
@pytest.mark.parametrize('reason', ['stop', 'length'])
@pytest.mark.parametrize('file_in_grace', [False, True])
def test_fake_completed_turn_and_grace_report_drive_only_named_structure_outcome(
        setup_structure, kind, reason, file_in_grace):
    ship, svc, seat, root, order = setup_structure(kind, reason)
    recipient = 'lead' if kind == 'forum' else 'conductor'
    notices = lambda: [m for m in svc.store.for_seat(recipient) if m.sender == post.SHIP]
    svc.tick()
    assert notices() == []
    assert structure_outcome.waits(ship, [seat]) == ['waiting on alpha: turn ended, no report']
    if file_in_grace:
        filed = svc.store.new('report', order.to, order.sender,
                              '## Cascade note\nFiled during grace.\n## Answer\nWhole synthetic answer.', re=order.id)
    svc.tick()
    if file_in_grace and kind == 'cascade':
        data = cascade.load(cascade.folder(ship.runtime, root.id))
        assert data['status'] == 'running' and data['last'] == filed.id and len(data['steps']) == 2
        assert notices() == []
    else:
        assert len(notices()) == 1
        if file_in_grace:
            assert 'all filed' in notices()[0].body and filed.id in notices()[0].body
        else:
            named = structure_outcome.LENGTH if reason == 'length' else structure_outcome.NORMAL
            assert f'alpha: {named}' in notices()[0].body
            assert svc.store.is_open(order)
        svc.tick()
        assert len(notices()) == 1
    assert svc.store.is_open(root)
    assert structure_outcome.waits(ship, [seat]) == []
