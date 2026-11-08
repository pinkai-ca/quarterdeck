"""Synthetic copies of structured watcher results, independent of delivery receipts."""

from datetime import timedelta
import json

import pytest

from quarterdeck.harness.claude import ClaudeHarness
from quarterdeck.harness.claude_watch import inputs
from quarterdeck.order_attribution import Threads
from quarterdeck.post import Store, typed
from .test_order_attribution import AT, message

OUTPUT = '/tmp/synthetic-watch.output'


def call(key,command,second=1):
    return dict(timestamp=(AT+timedelta(seconds=second)).isoformat(), type='assistant',
                message=dict(role='assistant',content=[dict(type='tool_use',id=key,name='Bash',input={'command':command})]))


def result(key,content,second=2):
    return dict(timestamp=(AT+timedelta(seconds=second)).isoformat(), type='user',
                message=dict(role='user',content=[dict(type='tool_result',tool_use_id=key,content=content)]))


def origin(key='watch',second=2,path=OUTPUT):
    return result(key,f'Command running in background with ID: synthetic. Output is being written to: {path}. You will be notified.',second)


def write(path,rows):
    path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    return path


def test_watch_counts_actual_read_turn_never_watcher_launch_and_never_a_receipt(tmp_path):
    store=Store(tmp_path); order=message(store,'ord-change',to='worker')
    envelope=typed([order])
    path=write(tmp_path/'record.jsonl',[call('watch','qd inbox --wait'),origin(),
             call('read',f'cat {OUTPUT}',11),result('read',envelope+'\n[exited with code 0]',12)])
    found=inputs(path)
    assert len(found)==1 and found[0].at==AT+timedelta(seconds=12) and found[0].kind=='watch'
    graph=Threads(store)
    assert graph.received(found[0].text,found[0].at,'worker')[0]['message']==order.id
    assert ClaudeHarness().inputs(path)==[], 'watch output never becomes transport input evidence'


@pytest.mark.parametrize('failure',['no-origin','not-watcher','wrong-file','reused-output','conflicting-call',
                                    'compound-command','future-call','future-origin','truncated','extra-stdout'])
def test_unproven_watcher_chain_or_incomplete_envelope_supplies_no_attribution(tmp_path,failure):
    store=Store(tmp_path); order=message(store,'ord-change'); envelope=typed([order])
    rows=[call('watch','qd inbox --wait'),origin(),call('read',f'cat {OUTPUT}',11),result('read',envelope,12)]
    if failure=='no-origin':rows=rows[2:]
    if failure=='not-watcher':rows[0]=call('watch','echo qd inbox --wait')
    if failure=='wrong-file':rows[2]=call('read','cat /tmp/synthetic-other.output',11)
    if failure=='reused-output':rows[2:2]=[call('another','qd inbox --wait',3),origin('another',4)]
    if failure=='conflicting-call':rows.insert(1,call('watch','echo arbitrary text'))
    if failure=='compound-command':rows[2]=call('read',f'cat {OUTPUT}; echo extra',11)
    if failure=='future-call':rows[2]=call('read',f'cat {OUTPUT}',13)
    if failure=='future-origin':rows[1]=origin(second=15)
    if failure=='truncated':rows[-1]=result('read',envelope[:-10],12)
    if failure=='extra-stdout':rows[-1]=result('read',envelope+'\nextra stdout',12)
    found=inputs(write(tmp_path/'record.jsonl',rows))
    assert not [m for item in found for m in Threads(store).received(item.text,item.at,'worker')]


def test_repeated_origin_row_is_one_origin_and_queue_metadata_without_output_is_not_consumption(tmp_path):
    store=Store(tmp_path); order=message(store,'ord-change')
    rows=[call('watch','qd inbox --wait'),origin(),origin(second=3)]
    path=write(tmp_path/'record.jsonl',rows)
    assert inputs(path)==[]
    rows += [call('read',f'cat {OUTPUT}',11),result('read',typed([order]),12)]
    assert len(inputs(write(path,rows)))==1


def test_exact_foreground_watcher_output_is_tied_to_its_result_time(tmp_path):
    store=Store(tmp_path); order=message(store,'ord-change')
    path=write(tmp_path/'record.jsonl',[call('watch','qd inbox --wait'),result('watch',typed([order]),12)])
    assert inputs(path)[0].at==AT+timedelta(seconds=12)
