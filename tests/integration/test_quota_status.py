"""One persisted quota wall is visible from every private ship session."""

from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess

from quarterdeck import seat, ship
from quarterdeck.harness.antigravity_quota import parse_usage, wall
from quarterdeck.ledger import Ledger
from .test_seat import wait_for
from ..unit.test_antigravity_quota import document


def test_each_session_uses_shipwide_wall_and_cached_google_headline(example_ship, tmux):
    with (example_ship/'ship.toml').open('a') as stream:
        stream.write('\n[pools.google]\nprovider = "antigravity"\n')
    declared=ship.load(example_ship)
    seat.bring_up(declared,tmux)  # the declared programs are synthetic fake harnesses
    book=Ledger(declared.runtime/'ledger.sqlite')
    now=datetime.now(timezone.utc)
    book.record_usage('google',parse_usage(document(),now))
    event=wall('Individual quota reached. Resets in 2h.\nError ID: synthetic-status','synthetic-error',now)
    book.quota.observe('synthetic-record','worker-1','google',event,now)
    book.close()
    qd=str(Path(__import__('sys').executable).parent/'qd')
    for session in declared.sessions.values():
        panes=wait_for(lambda: tmux.panes(session.title))
        command=tmux.run('show-option','-v','-t',panes[0].id,'status-right')
        assert 'status --line' in command and str(example_ship) in command
        env=dict(os.environ,QD_TMUX_SOCKET=tmux.socket)
        env.pop('QD_SHIP',None)
        env.pop('QD_SEAT',None)
        result=subprocess.run([qd,'--ship',str(example_ship),'status','--line'],env=env,capture_output=True,text=True,check=True)
        assert 'wall: google individual quota reached' in result.stdout and 'reset ' in result.stdout
        assert 'google 7d 75% used' in result.stdout
