"""Line text omits allowances while detailed and JSON readings retain the cached facts."""

from datetime import datetime, timedelta, timezone
import os
import json
from pathlib import Path
import subprocess

from quarterdeck import seat, ship
from quarterdeck.harness.antigravity_quota import parse_usage, unknown, wall
from quarterdeck.ledger import Ledger
from .waiting import wait_for
from ..unit.test_antigravity_quota import document


def test_each_session_omits_an_unused_pool_wall_and_keeps_the_json_facts(drydock, tmux):
    with (drydock/'ship.toml').open('a') as stream:
        stream.write('\n[pools.google]\nprovider = "antigravity"\n')
    declared=ship.load(drydock)
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
        assert 'status --line' in command and str(drydock) in command
        env=dict(os.environ,QD_TMUX_SOCKET=tmux.socket)
        env.pop('QD_SHIP',None)
        env.pop('QD_SEAT',None)
        result=subprocess.run([qd,'--ship',str(drydock),'status','--line'],env=env,capture_output=True,text=True,check=True)
        assert 'google' not in result.stdout and 'reset' not in result.stdout and 'usage' not in result.stdout
        result=subprocess.run([qd,'--ship',str(drydock),'--json','status','--line'],env=env,capture_output=True,text=True,check=True)
        facts=json.loads(result.stdout)
        assert any('google individual quota reached' in entry for entry in facts['walls'])
        assert facts['usage'][0]['reading']['used'] == .75


def test_last_known_and_recovery_reach_status_watch_and_ledger_without_query(drydock, tmux):
    with (drydock/'ship.toml').open('a') as stream:
        stream.write('\n[pools.google]\nprovider = "antigravity"\n')
    declared = ship.load(drydock)
    book = Ledger(declared.runtime/'ledger.sqlite')
    good_at = datetime.now(timezone.utc)-timedelta(minutes=12)
    book.record_usage('google', parse_usage(document(), good_at))
    book.record_usage('google', unknown('synthetic provider timeout', datetime.now(timezone.utc)))
    qd = str(Path(__import__('sys').executable).parent/'qd')
    env = dict(os.environ, QD_TMUX_SOCKET=tmux.socket)
    env.pop('QD_SHIP', None)
    env.pop('QD_SEAT', None)
    commands = [('status', '--line'), ('status',), ('watch', 'worker-1'), ('ledger',)]
    before = book.db.execute('SELECT * FROM usage_reads').fetchall()
    for args in commands:
        result = subprocess.run([qd, '--ship', str(drydock), *args], env=env,
                                capture_output=True, text=True, check=True)
        if args == ('status', '--line'):
            assert 'google' not in result.stdout and 'reset' not in result.stdout and 'usage' not in result.stdout
            assert len(result.stdout.rstrip('\n')) <= 160
        else:
            assert 'google Gemini 4 Argon / 7d 75%' in result.stdout and 'LAST KNOWN' in result.stdout
            assert '75% used' in result.stdout and 'synthetic provider timeout' in result.stdout
            assert good_at.isoformat() in result.stdout and '(12m ago)' in result.stdout
    assert book.db.execute('SELECT * FROM usage_reads').fetchall() == before
    recovered_at = datetime.now(timezone.utc)
    doc = document()
    doc['command']['data']['groups'][-1]['buckets'][0]['remaining_fraction'] = .5
    book.record_usage('google', parse_usage(doc, recovered_at))
    for args in commands:
        result = subprocess.run([qd, '--ship', str(drydock), *args], env=env,
                                capture_output=True, text=True, check=True)
        assert 'LAST KNOWN' not in result.stdout and 'synthetic provider timeout' not in result.stdout
        if args == ('status', '--line'):
            assert 'google' not in result.stdout and 'reset' not in result.stdout
        else:
            assert 'google Gemini 4 Argon / 7d 50%' in result.stdout
            assert '50% used' in result.stdout and recovered_at.isoformat() in result.stdout
    assert book.db.execute('SELECT count(*) FROM pools').fetchone() == (5,)
    book.close()
