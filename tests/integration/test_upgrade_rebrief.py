"""Re-brief notices use private fake seats' normal door/watch paths without relaunch."""

import json
import shutil

from quarterdeck import install, post, standing
from quarterdeck.seat import Seat
from quarterdeck.service import Service
from quarterdeck.ship import load
from .waiting import wait_for


def test_changed_upgrade_brief_uses_door_and_human_watch_and_down_seat_launch(drydock,tmux,monkeypatch,tmp_path):
    framework=tmp_path/'framework';shutil.copytree(standing.FRAMEWORK_DIR,framework)
    monkeypatch.setattr(standing,'FRAMEWORK_DIR',framework)
    monkeypatch.setenv('QD_TMUX_SOCKET',tmux.socket)
    monkeypatch.delenv('QD_SEAT',raising=False)
    ship=load(drydock)
    store=post.Store(ship.runtime)
    open_order=store.new('order','operator','captain','Original open work.')
    open_question=store.new('question','operator','desk','Original open question.')
    captain=Seat(ship,ship.seat('captain'),tmux);desk=Seat(ship,ship.seat('desk'),tmux)
    captain.up();desk.up()
    try:
        wait_for(lambda: captain.record() and desk.record(),seconds=15)
        wait_for(lambda:not captain.is_working() and not desk.is_working(),seconds=15)
        before={seat.name:(seat.pane().id,seat.pane().pid,seat.record(),seat.launched()) for seat in (captain,desk)}
        snapshot=install.brief_snapshot(ship)
        (framework/'laws.md').write_text((framework/'laws.md').read_text()+'\nChanged synthetic instruction.\n')
        commit='c'*40
        (ship.runtime/'installed.json').write_text(json.dumps(dict(commit=commit,ref='synthetic-current',at='2025-01-01T00:00:00+00:00',previous=None)))
        monkeypatch.setattr(install,'running_from',lambda path:True)
        path,data=install.queue_rebrief(ship,commit,None,snapshot)
        svc=Service(ship,tmux)
        try:
            svc.requests()
            notices=[m for m in svc.store.all() if m.sender==post.SHIP]
            assert sorted(m.to for m in notices)==['captain','desk']
            svc.deliveries()
            cap=next(m for m in notices if m.to=='captain')
            assert svc.attempts.latest()[cap.id]=='delivered'
            office=next(m for m in notices if m.to=='desk')
            assert office.id not in svc.attempts.latest(), 'the human session is reached only through its watch'
            assert desk.harness.screen(tmux.capture(desk.pane().id)).box==''
            delivered=post.wait(ship.runtime,'desk',interval=.05,stop_after=.5)
            assert delivered and 'runtime/brief/desk.md' in delivered[0]
            post.printed(ship.runtime,delivered[1])
            assert svc.attempts.latest()[office.id]=='watch'
            svc.requests();svc.rebrief();svc.deliveries()
            assert [m.id for m in svc.store.all() if m.sender==post.SHIP]==[m.id for m in notices]
            assert {seat.name:(seat.pane().id,seat.pane().pid,seat.record(),seat.launched()) for seat in (captain,desk)}==before
            assert svc.store.open_for('captain')==[open_order] and svc.store.open_for('desk')==[open_question]
            worker=Seat(ship,ship.seat('worker-1'),tmux)
            assert not worker.is_up
            worker.up()
            try:
                assert 'Changed synthetic instruction.' in (ship.runtime/'brief/worker-1.md').read_text()
                assert not [m for m in svc.store.for_seat('worker-1') if m.sender==post.SHIP]
            finally:worker.down()
        finally:svc.ledger.close()
    finally:
        captain.down();desk.down()
