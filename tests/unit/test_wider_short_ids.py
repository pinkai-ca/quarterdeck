"""Invented mixed-width messages, readers and native record receipts."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
from types import SimpleNamespace

import pytest

from quarterdeck import cascade_runtime, cli, input_receipt, output, pending, post, post_threads, post_view
from quarterdeck.door import Door
from quarterdeck.harness import get, synthetic_record
from quarterdeck.harness.base import Screen
from quarterdeck.harness.synthetic_record import ClaudeShape, CodexShape, PiShape
from tests import antigravity_shapes


AT = datetime(2040, 1, 1, tzinfo=timezone.utc)
SUFFIXES = ('ab12', 'ab12cd', '0123456789abcdef')


def message(suffix, kind='order', parent=None):
    identity = (f'{kind[:3]}-ordinary-{suffix}' if len(suffix)==16 else
                f'{kind[:3]}-20400101-0000-ordinary-lead-{suffix}')
    return post.Message(identity, kind, 'ordinary-lead', 'ordinary-worker', AT,
                        'An ordinary synthetic task.\n', re=parent, subject='An ordinary task')


def test_new_allocator_uses_six_digits_and_old_prefix_remains_a_distinct_reference(tmp_path, monkeypatch):
    store = post.Store(tmp_path/'runtime')
    old = message('ab12')
    store.write(old)
    draws = []
    def suffix(size):
        draws.append(size)
        return 'ab12cd'
    monkeypatch.setattr(post, 'secrets', SimpleNamespace(token_hex=suffix))
    new = store.new('order', 'ordinary-lead', 'ordinary-worker', 'A new ordinary task.')
    assert draws == [3]
    assert re.fullmatch(r'ord-[0-9a-f]{6}', new.short)
    assert new.short.startswith(old.short)
    assert store.get(old.short) == old and store.get(new.short) == new
    assert store.get(old.id) == old and store.get(new.id) == new


@pytest.mark.parametrize('reference_width', SUFFIXES)
def test_mixed_widths_resolve_in_store_thread_cli_inbox_and_view(tmp_path, monkeypatch, reference_width):
    runtime = tmp_path/'runtime'
    store = post.Store(runtime)
    old = message(SUFFIXES[0])
    new = message(SUFFIXES[1], 'answer', old.id)
    legacy = message(SUFFIXES[2], 'report', new.id)
    messages = (old, new, legacy)
    for item in messages:
        store.write(item)
    target = next(item for item in messages if item.id.endswith(reference_width))
    assert store.get(target.short) == target
    thread = post_threads.whole(store,target.short,'ordinary-worker')
    assert [item.id for item in thread.messages] == [item.id for item in messages]
    assert all(item.short in post_threads.render(thread) for item in messages)
    monkeypatch.setattr(cli, 'load_ship', lambda args: SimpleNamespace(runtime=runtime,seats={}))
    monkeypatch.setattr(cli, 'this_seat', lambda *args,**kwargs:'ordinary-worker')
    reading = cli.cmd_thread(SimpleNamespace(message=target.short))
    assert reading.data.requested == target.id
    assert all(item.short in reading.render(reading.data) for item in messages)
    monkeypatch.setattr(cli,'_inbox_target',lambda *args:('ordinary-worker',True,None))
    inbox = cli.cmd_inbox(SimpleNamespace(seat='ordinary-worker',wait=False,all=True))
    assert all(item.short in inbox.render(inbox.data) for item in messages)
    wire = output.value(inbox.data)
    assert {row['id'] for row in [*wire['open'],*wire['received']]} == {item.id for item in messages}
    snapshot = post_view.Snapshot(runtime)
    assert set(snapshot.shorts.splitlines()) == {item.short for item in messages}
    view = tmp_path/'ordinary-view'/'post'
    view.mkdir(parents=True)
    (view/post_view.SHORTS).write_text(snapshot.shorts)
    assert post_view.taken_shorts(view) == {item.short for item in messages}


@pytest.mark.parametrize('suffix', SUFFIXES[:2])
def test_canonical_cascade_and_legacy_pending_labels_keep_both_widths(tmp_path,suffix):
    item = message(suffix,'question')
    assert post.id_sender(item.id) == item.sender
    assert cascade_runtime.key(item.id) == item.id
    seat = SimpleNamespace(name='ordinary-worker',ship=SimpleNamespace(runtime=tmp_path/'runtime'))
    label = pending.inspection(seat,'Ordinary diagnostic',[item.id+'-1.json'])
    assert item.short in label and 'message ID unknown' not in label


def test_legacy_deterministic_reference_keeps_its_pending_label(tmp_path):
    item = message(SUFFIXES[2],'report')
    seat = SimpleNamespace(name='ordinary-worker',ship=SimpleNamespace(runtime=tmp_path/'runtime'))
    label = pending.inspection(seat,'Ordinary diagnostic',[item.id+'-1.json'],message=item.id)
    assert item.short in label and 'message ID unknown' not in label


@pytest.mark.parametrize('suffix', SUFFIXES)
@pytest.mark.parametrize('kind', post.KINDS)
def test_antigravity_queue_preview_names_the_complete_typed_short(suffix,kind):
    item = message(suffix,kind)
    payload = post.typed([item])
    header = payload.splitlines()[0]
    adapter = get('antigravity')
    after = Screen(f'▸ {header} (alt+m to send immediately)\nPress up to edit queued messages',
                   box='',working=True,queued=1)
    assert adapter.queued_input(payload,Screen('',box='',working=False),after) is not None


def native_record(program, root, payload, monkeypatch):
    stamped = AT+timedelta(seconds=1)
    monkeypatch.setattr(synthetic_record,'now',lambda:stamped)
    if program in ('claude','codex','pi','antigravity'):
        monkeypatch.setattr(antigravity_shapes,'now',lambda:stamped)
        cls = {'claude':ClaudeShape,'codex':CodexShape,'pi':PiShape,
               'antigravity':antigravity_shapes.AntigravityShape}[program]
        writer = cls(root/'home',root/'workspace','ordinary-session','ordinary-model')
        writer.begin(payload,'ordinary-model','high')
        writer.end('An ordinary result.')
        return writer.path
    if program == 'grok':
        path = root/'home'/'sessions'/'ordinary'/'ordinary-session'/'updates.jsonl'
        path.parent.mkdir(parents=True)
        row = dict(method='session/update',timestamp=stamped.isoformat(),
                   params=dict(sessionId='ordinary-session',update=dict(sessionUpdate='user_message_chunk',
                               content=dict(type='text',text=payload))))
        path.write_text(json.dumps(row)+'\n')
        return path
    assert program == 'opencode'
    path = root/'home'/'data-1'/'opencode'/'opencode.db'
    path.parent.mkdir(parents=True)
    milliseconds = int(stamped.timestamp()*1000)
    with sqlite3.connect(path) as db:
        db.executescript('''CREATE TABLE session(id TEXT,time_created INTEGER,time_updated INTEGER,parent_id TEXT);
            CREATE TABLE message(id TEXT,session_id TEXT,time_created INTEGER,time_updated INTEGER,data TEXT);
            CREATE TABLE part(id TEXT,message_id TEXT,session_id TEXT,time_created INTEGER,time_updated INTEGER,data TEXT);''')
        db.execute('INSERT INTO session VALUES (?,?,?,NULL)',('ordinary-session',milliseconds,milliseconds))
        db.execute('INSERT INTO message VALUES (?,?,?,?,?)',('ordinary-user','ordinary-session',milliseconds,
                   milliseconds,json.dumps(dict(role='user',time=dict(created=milliseconds)))))
        db.execute('INSERT INTO part VALUES (?,?,?,?,?,?)',('ordinary-text','ordinary-user','ordinary-session',
                   milliseconds,milliseconds,json.dumps(dict(type='text',text=payload))))
    return path


@pytest.mark.parametrize('suffix', SUFFIXES)
@pytest.mark.parametrize('program', ['claude','codex','pi','opencode','antigravity','grok'])
def test_each_native_record_and_the_door_confirm_the_complete_typed_message(tmp_path,monkeypatch,program,suffix):
    item = message(suffix)
    payload = post.typed([item])
    record = native_record(program,tmp_path,payload,monkeypatch)
    adapter = get(program)
    assert input_receipt.confirmed(adapter,record,payload,AT) == 'delivered'
    pane = SimpleNamespace(id='%ordinary',pid=123,width=2000,height=100)
    seat = SimpleNamespace(harness=adapter,tmux=None,session=SimpleNamespace(human=False),
                           record=lambda:record,pane=lambda:pane)
    door = Door(seat)
    door.since = AT
    assert door.matches(adapter.box(payload),Screen(payload,box=payload,working=False))
    assert door.confirm(payload,seconds=0).result == 'delivered'
    other = payload.replace(item.short,item.short+'00')
    assert input_receipt.confirmed(adapter,record,other,AT) is None
