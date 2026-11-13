"""Persistent observed walls, actual launch provenance, delivery and status, all synthetic."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from quarterdeck import quota, service, watch
from quarterdeck.door import Door
from tests.antigravity_shapes import AntigravityShape, _pb_bytes, _pb_str, _pb_ts, _pb_uint
from quarterdeck.harness.antigravity import AntigravityHarness
from quarterdeck.harness.antigravity_quota import parse_usage, unknown, wall
from quarterdeck.harness.base import State
from quarterdeck.ledger import Ledger
from quarterdeck.post import Store
from quarterdeck.seat import Launched, Seat
from quarterdeck.service import Service
from quarterdeck.ship import Pool, SeatSpec, Session, Ship
from quarterdeck.tmux import Pane
from .test_antigravity_quota import document

TEXT = 'Individual quota reached. Please upgrade your subscription. Resets in 2h3m4s.\nError ID: synthetic-error'
IDLE = '─'*80+'\n>\n'+'─'*80+'\n                                         Gemini 4 Argon · high\n'


def synthetic_seat(tmp_path, monkeypatch):
    pools = {name: Pool(name,'antigravity') for name in ('google-a','google-b')}
    sessions = {name: Session(name,name,human=name=='office') for name in ('bridge','crew','office')}
    spec = SeatSpec('worker','crew','worker','antigravity',tuple(pools),cwd=tmp_path)
    ship = Ship(tmp_path,'example','admiral',tmp_path,pools,sessions,{'worker':spec})
    shown = {'text':IDLE}
    actions = []
    tmux = SimpleNamespace(pane=lambda ident: Pane('%1','crew','worker',4242,80,24,False,False,None),
                           capture=lambda *a,**k: shown['text'], paste=lambda *a:actions.append(('paste',a)),
                           keys=lambda *a:actions.append(('keys',a)))
    seat = Seat(ship,spec,tmux)
    seat.home.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    launched = Launched('%1',4242,(now-timedelta(hours=1)).isoformat(),'google-a',None,['synthetic-agy'],'synthetic', harness='antigravity')
    launched.write(seat.folder/'launch.json')
    rec = AntigravityShape(seat.home,tmp_path,'synthetic-conversation','synthetic-model')
    rec.begin('synthetic input','synthetic-model','high')
    rec.end('synthetic reply')
    (seat.folder/'record').write_text(str(rec.path))
    monkeypatch.setattr(service,'seats',lambda *args:[seat])
    return seat,rec,shown,actions


def tick(seat):
    """One accounting pass of the service, the only process that writes a wall into the book."""
    svc = Service(seat.ship, seat.tmux)
    svc.account()
    svc.ledger.close()


def append_error(rec, text, at, index=8):
    details = _pb_str(1,text) + _pb_str(3,text)
    payload = _pb_bytes(24,_pb_bytes(3,details))
    with rec._conn() as db:  # close the writer before source-unchanged assertions
        last = db.execute('SELECT MAX(idx) FROM steps').fetchone()[0]
        executor = _pb_uint(1,1) + _pb_uint(2,1) + _pb_uint(3,last) + _pb_str(9,rec.exec_id)
        db.execute('UPDATE executor_metadata SET data=? WHERE idx=(SELECT MAX(idx) FROM executor_metadata)', (executor,))
        db.execute('INSERT INTO steps VALUES (?,17,3,0,?,?,?)', (index,_pb_ts(1,at) if at else b'',b'',payload))


@pytest.mark.usefixtures('synthetic_disk_room')  # the machine's disk is not this test's subject
def test_record_wall_survives_idle_later_input_restart_and_blocks_delivery(tmp_path, monkeypatch):
    seat,rec,shown,actions = synthetic_seat(tmp_path,monkeypatch)
    at = datetime.now(timezone.utc)-timedelta(minutes=1)
    append_error(rec,TEXT,at)
    event = seat.harness.state(rec.path).walls[0]
    assert event.key == 'step:8' and event.error_id == 'synthetic-error'
    assert abs((event.resets_at-(at+timedelta(hours=2,minutes=3,seconds=4))).total_seconds()) < .001
    shown['text']=TEXT+'\n'+IDLE
    door=Door(seat)
    assert not door.reading().open
    assert door.deliver('must remain queued',speed='interrupt').result == 'withheld' and actions == []
    tick(seat)
    book=Ledger(seat.ship.runtime/'ledger.sqlite')
    original=book.db.execute('SELECT source,event,pool,at,resets_at,observed_at FROM wall_events').fetchall()
    assert original[0][:3] == (str(rec.path),'step:8','google-a')
    book.close()
    shown['text']=IDLE
    rec.begin('a later synthetic input','synthetic-model','high')
    rec.end('a later synthetic reply')
    for _ in range(3): assert not Door(seat).reading().open
    book=Ledger(seat.ship.runtime/'ledger.sqlite')
    assert book.db.execute('SELECT source,event,pool,at,resets_at,observed_at FROM wall_events').fetchall() == original
    assert book.quota.active(now=at+timedelta(hours=3)) == []
    book.close()
    restored=Seat(seat.ship,seat.spec,seat.tmux)
    assert not Door(restored).reading().open
    # Status line is ship-wide, independent of the querying tmux session and of provider calls.
    monkeypatch.setattr(seat.harness,'usage',lambda *a: (_ for _ in ()).throw(AssertionError('status fetched usage')))
    for session in seat.ship.sessions:
        monkeypatch.setenv('TMUX',f'synthetic-{session}')
        line=watch.line(seat.ship,seat.tmux)
        assert f'{seat.name} held at limit' in line  # law: nothing-waits-silently
        assert 'google-a' not in line and 'reset' not in line


@pytest.mark.usefixtures('synthetic_disk_room')  # the machine's disk is not this test's subject
@pytest.mark.parametrize('expired', [True, False])
def test_screen_fallback_is_stable_then_yields_to_matching_record_time(tmp_path, monkeypatch, expired):
    seat,rec,shown,_=synthetic_seat(tmp_path,monkeypatch)
    shown['text']=TEXT+'\n'+IDLE
    reading = Door(seat).reading()
    assert not reading.open and 'estimated from first screen observation' in reading.reason, 'the banner alone holds the door, before anything is written'
    tick(seat)
    assert f'{seat.name} held at limit' in watch.line(seat.ship,seat.tmux)
    book=Ledger(seat.ship.runtime/'ledger.sqlite')
    first=book.db.execute('SELECT at,resets_at,observed_at FROM wall_events').fetchone()
    assert 'estimated from first screen observation' in book.quota.walls()[0]
    book.close()
    assert not Door(seat).reading().open
    book=Ledger(seat.ship.runtime/'ledger.sqlite')
    assert book.db.execute('SELECT at,resets_at,observed_at FROM wall_events').fetchone() == first
    book.close()
    # Canonical time replaces the estimate, including when it proves expiration.
    at = datetime.now(timezone.utc) - (timedelta(hours=5) if expired else timedelta(minutes=5))
    append_error(rec,TEXT,at)
    tick(seat)
    reading = Door(seat).reading()
    assert reading.open == expired
    assert 'estimated' not in reading.reason
    assert 'estimated' not in watch.line(seat.ship,seat.tmux)
    book=Ledger(seat.ship.runtime/'ledger.sqlite')
    if expired:
        assert not book.quota.active() and not book.walls()
    else:
        expected = quota.reset_text((at + timedelta(hours=2,minutes=3,seconds=4)).isoformat())
        assert expected in reading.reason and expected in book.quota.walls()[0]
        assert 'estimated' not in book.quota.walls()[0]
    assert book.db.execute("SELECT superseded FROM wall_events WHERE source LIKE 'screen:%'").fetchone() == (1,)
    # The original fallback times are retained, not rewritten while changing presentation.
    assert book.db.execute("SELECT at,resets_at,observed_at FROM wall_events WHERE source LIKE 'screen:%'").fetchone() == first
    book.close()
    assert Door(seat).reading().open == expired, 'an expired historical banner must not resurrect'


def test_unknown_time_and_reset_stay_unknown_and_wrong_pool_is_never_charged(tmp_path, monkeypatch):
    seat,rec,shown,_=synthetic_seat(tmp_path,monkeypatch)
    assert quota.reset_text(None, from_screen=True) == 'reset unknown'
    append_error(rec,'Individual quota reached.',None)
    tick(seat)
    assert not Door(seat).reading().open
    book=Ledger(seat.ship.runtime/'ledger.sqlite')
    assert book.db.execute('SELECT pool,at,resets_at FROM wall_events').fetchone() == ('-',None,None)
    assert not book.walled('google-a') and not book.walled('google-b')
    assert 'worker (pool unknown)' in book.quota.walls()[0] and 'reset unknown' in book.quota.walls()[0]
    book.close()


def test_launch_pool_at_event_time_survives_pool_switch_and_ambiguous_history(tmp_path, monkeypatch):
    seat,rec,shown,_=synthetic_seat(tmp_path,monkeypatch)
    first=seat.launched()
    at=datetime.now(timezone.utc)-timedelta(minutes=5)
    seat.record()  # preserve the first proven launch
    seat.move_to('google-b')
    replace(first,at=(at+timedelta(minutes=1)).isoformat(),pool='google-b').write(seat.folder/'launch.json')
    append_error(rec,TEXT,at)
    tick(seat)
    assert Door(seat).reading().open, 'current pool-b is not blocked by pool-a evidence'
    book=Ledger(seat.ship.runtime/'ledger.sqlite')
    assert book.walled('google-a') and not book.walled('google-b')
    book.provenance.remember(rec.path,'worker','google-b',first.when)
    # A separate event at a conflicting launch boundary has unknown pool provenance.
    new=wall('Individual quota reached.','another-event',first.when)
    book.quota.record(rec.path,'worker',(new,),book.provenance,datetime.now(timezone.utc))
    assert book.db.execute("SELECT pool FROM wall_events WHERE event='another-event'").fetchone() == ('-',)
    book.close()


def test_background_refresh_cadence_restart_wall_trigger_and_failures(tmp_path, monkeypatch):
    seat,rec,shown,_=synthetic_seat(tmp_path,monkeypatch)
    calls=[]
    def usage(pool):
        calls.append(pool.name)
        return parse_usage(document(),datetime.now(timezone.utc))
    monkeypatch.setattr(AntigravityHarness,'usage',lambda self,pool:usage(pool))
    monkeypatch.setattr(service.machine,'processes',lambda:{})
    svc=Service(seat.ship,seat.tmux)
    svc.account()
    assert calls == ['google-a']
    svc.account()
    assert calls == ['google-a']
    svc.ledger.close()
    svc=Service(seat.ship,seat.tmux)
    svc.account()
    assert calls == ['google-a'], 'restart retains the background cadence'
    append_error(rec,TEXT,datetime.now(timezone.utc))
    svc.account()
    assert calls == ['google-a','google-a'], 'new observed wall triggers one background refresh'
    assert svc.ledger.walled('google-a'), 'available numeric quota does not clear an observed wall'
    for _ in range(3): svc.account()
    assert len(calls)==2
    # Failed refresh retains the old percentage explicitly as LAST KNOWN, never current.
    monkeypatch.setattr(AntigravityHarness,'usage',lambda self,pool:unknown('synthetic timeout',datetime.now(timezone.utc)))
    svc.ledger.quota.usage_read('google-a',datetime.now(timezone.utc)-timedelta(seconds=301),None)
    svc.account()
    assert not svc.ledger.usage_known('google-a') and svc.ledger.walled('google-a')
    assert 'synthetic timeout' in svc.ledger.quota.usage_notes()[0]
    assert 'LAST KNOWN' in watch.ledger_usage(seat.ship)[0]
    svc.ledger.close()


def test_ambiguous_join_never_decodes_foreign_records_for_wall_attribution(tmp_path, monkeypatch):
    seat,rec,shown,_=synthetic_seat(tmp_path,monkeypatch)
    (seat.folder/'record').unlink()
    monkeypatch.setattr(AntigravityHarness,'_appeared',lambda *a:[tmp_path/'one.db',tmp_path/'two.db'])
    monkeypatch.setattr(seat.harness,'state',lambda *a: (_ for _ in ()).throw(AssertionError('foreign record opened')))
    shown['text']=TEXT+'\n'+IDLE
    assert not Door(seat).reading().open
    tick(seat)
    book=Ledger(seat.ship.runtime/'ledger.sqlite')
    rows=book.db.execute('SELECT source,pool FROM wall_events').fetchall()
    assert len(rows)==1 and rows[0][0].startswith('screen:') and rows[0][1]=='google-a'
    book.close()


def test_expired_record_event_recognized_after_restart_and_record_failure(tmp_path, monkeypatch):
    seat, rec, shown, _ = synthetic_seat(tmp_path, monkeypatch)
    append_error(rec, TEXT, datetime.now(timezone.utc) - timedelta(hours=5))
    tick(seat)
    assert Door(seat).reading().open
    prior = seat.launched()
    replace(prior, at=datetime.now(timezone.utc).isoformat()).write(seat.folder/'launch.json')
    shown['text'] = TEXT + '\n' + IDLE
    def unreadable(*args):
        raise OSError('synthetic record temporarily unreadable')
    monkeypatch.setattr(seat.harness, 'state', unreadable)
    assert Door(seat).wall(seat.harness.screen(shown['text'])) is None, 'a banner the book already placed in time is not news'
    book = Ledger(seat.ship.runtime/'ledger.sqlite')
    assert book.db.execute('SELECT COUNT(*) FROM wall_events').fetchone() == (1,)
    assert not book.quota.active()
    book.close()


def test_wrapped_canonical_banner_keeps_identity_and_does_not_resurrect(tmp_path, monkeypatch):
    seat, rec, shown, _ = synthetic_seat(tmp_path, monkeypatch)
    canonical = ('Individual quota reached. Please upgrade your subscription to increase your limits. '
                 'Resets in 2h.\nError ID: synthetic-wrapped')
    wrapped = ('⚠ Individual quota reached. Please upgrade your subscription to increase your\n'
               'limits. Resets in 2h.\nError ID: synthetic-wrapped\n')
    at = datetime.now(timezone.utc) - timedelta(hours=3)
    append_error(rec, canonical, at)
    shown['text'] = wrapped + IDLE
    screen = seat.harness.screen(shown['text'])
    parsed = wall(screen.quota, 'screen', at)
    persisted = seat.harness.state(rec.path).walls[0]
    assert parsed.error_id == persisted.error_id == 'synthetic-wrapped'
    assert parsed.signature == persisted.signature and parsed.resets_at == at + timedelta(hours=2)
    tick(seat)
    assert Door(seat).reading().open
    book = Ledger(seat.ship.runtime/'ledger.sqlite')
    assert book.db.execute('SELECT COUNT(*) FROM wall_events').fetchone() == (1,)
    assert not book.quota.active()
    book.close()


def test_wrapped_current_screen_has_fixed_reset_across_reflow_and_polls(tmp_path, monkeypatch):
    seat, rec, shown, _ = synthetic_seat(tmp_path, monkeypatch)
    shown['text'] = ('Individual quota reached. Please upgrade your subscription to increase your\n'
                     'limits. Resets in 2h.\nError ID: synthetic-wrapped-current\n' + IDLE)
    before = datetime.now(timezone.utc)
    assert not Door(seat).reading().open
    tick(seat)
    book = Ledger(seat.ship.runtime/'ledger.sqlite')
    initial = book.db.execute('SELECT at,resets_at,error_id,signature FROM wall_events').fetchone()
    started, reset = map(datetime.fromisoformat, initial[:2])
    assert started >= before and reset == started + timedelta(hours=2)
    assert initial[2] == 'synthetic-wrapped-current'
    book.close()
    shown['text'] = shown['text'].replace('your\nlimits.', 'your limits.').replace('subscription to', 'subscription\nto')
    for _ in range(3):
        assert not Door(seat).reading().open
    book = Ledger(seat.ship.runtime/'ledger.sqlite')
    assert book.db.execute('SELECT at,resets_at,error_id,signature FROM wall_events').fetchall() == [initial]
    book.close()
