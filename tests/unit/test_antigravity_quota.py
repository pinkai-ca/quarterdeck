"""Synthetic quota command contracts; no real binary, sign-in, conversation or log access."""

import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from quarterdeck.harness import antigravity_quota as agq
from quarterdeck.ledger import Ledger, pools_table

AT = datetime(2030, 1, 1, tzinfo=timezone.utc)
FIXTURE = Path(__file__).parents[1] / 'fixtures/antigravity/1.5.2/usage.json'


def document():
    return json.loads(FIXTURE.read_text())


def test_all_groups_visible_only_argon_weekly_blocks_and_missing_is_unknown(tmp_path):
    doc = document()
    usage = agq.parse_usage(doc, AT)
    assert len(usage.windows) == 5 and usage.error is None
    assert [(w.name,w.used,w.bucket) for w in usage.windows if w.blocks] == [('7d',0.75,'synthetic-argon-week')]
    book = Ledger(tmp_path/'ledger.sqlite')
    book.record_usage('google',usage)
    assert book.db.execute("SELECT group_name,bucket,bucket_name FROM pools WHERE window='7d'").fetchone() == (
        'Gemini 4 Argon','synthetic-argon-week','Synthetic weekly allowance')
    assert book.walls(now=AT) == [], 'exhausted Claude/GPT buckets cannot block Argon'
    shown = '\n'.join(pools_table(book))
    assert 'Claude and GPT models / weekly' in shown and '100%' in shown and '75%' in shown and 'Synthetic weekly allowance' in shown
    assert book.usage_known('google')
    del doc['command']['data']['groups'][-1]
    book.record_usage('google', agq.parse_usage(doc, AT))
    assert not book.usage_known('google') and book.walls(now=AT) == []
    assert 'Gemini 4 Argon weekly unavailable' in '\n'.join(pools_table(book))
    assert book.db.execute("SELECT used FROM pools WHERE window='7d'").fetchone() == (0.75,)
    book.close()


@pytest.mark.parametrize('fraction', [None, True, -0.1, 1.1, float('nan'), float('inf'), '0.2', 10**400])
def test_invalid_or_amount_only_fraction_never_becomes_zero(fraction):
    doc = document()
    bucket = doc['command']['data']['groups'][-1]['buckets'][0]
    bucket['remaining_amount'] = 42
    if fraction is None:
        del bucket['remaining_fraction']
    else:
        bucket['remaining_fraction'] = fraction
    usage = agq.parse_usage(doc, AT)
    headline = next(w for w in usage.windows if w.blocks)
    assert headline.used is None and headline.resets_at is not None and usage.error


def test_malformed_missing_and_duplicate_usage_data():
    for doc in (None, {}, {'status':'ERROR'}, {'status':'SUCCESS','command':{'name':'usage'}}):
        assert agq.parse_usage(doc, AT).windows[0].used is None
    doc = document()
    doc['command']['data']['groups'][-1]['buckets'].append(copy.deepcopy(doc['command']['data']['groups'][-1]['buckets'][0]))
    usage = agq.parse_usage(doc, AT)
    assert next(w for w in usage.windows if w.blocks).used is None and 'duplicate' in usage.error
    for reset in (None, 'not-time', '2030-01-01T00:00:00'):
        doc = document()
        doc['command']['data']['groups'][-1]['buckets'][0]['reset_time'] = reset
        assert next(w for w in agq.parse_usage(doc,AT).windows if w.blocks).resets_at is None


def test_the_usage_command_runs_the_program_the_path_gives_with_exact_arguments(monkeypatch):
    calls = []
    monkeypatch.setattr(agq.shutil, 'which', lambda name: None)
    monkeypatch.setattr(agq.subprocess, 'run', lambda *a, **k: calls.append((a, k)))
    result = agq.fetch_usage()
    assert not calls and 'agy is not on the PATH' in result.error, "no program, no call, and the reading says why"
    monkeypatch.setattr(agq.shutil, 'which', lambda name: '/synthetic/bin/agy')

    def run(argv, **options):
        calls.append(argv)
        assert argv == ['/synthetic/bin/agy', '-p', '/usage', '--output-format', 'json']
        assert options['env']['AGY_CLI_DISABLE_AUTO_UPDATE'] == 'true'
        assert options['timeout'] == 20 and options['capture_output']
        return SimpleNamespace(returncode=0, stdout=json.dumps(document()).encode())

    monkeypatch.setattr(agq.subprocess, 'run', run)
    assert agq.fetch_usage().error is None and len(calls) == 1


@pytest.mark.parametrize('case',['exit','timeout','bad-json','conversation','turn','tokens'])
def test_command_failure_and_unexpected_work_produce_visible_unknown(tmp_path, monkeypatch, case):
    monkeypatch.setattr(agq.shutil, 'which', lambda name: str(tmp_path/'synthetic-agy'))
    doc=document()
    if case=='conversation': doc['conversation_id']='synthetic-unexpected-record'
    if case=='turn': doc['num_turns']=1
    if case=='tokens': doc['usage']['input_tokens']=1
    def run(*args,**kwargs):
        if case=='timeout': raise subprocess.TimeoutExpired(args[0],20)
        return SimpleNamespace(returncode=1 if case=='exit' else 0,
                               stdout=b'broken' if case=='bad-json' else json.dumps(doc).encode())
    monkeypatch.setattr(agq.subprocess,'run',run)
    result=agq.fetch_usage()
    assert result.error and result.windows[0].used is None


def test_banner_original_time_missing_reset_and_overflow():
    text='Individual quota reached. Resets in 2h3m4s.\nError ID: synthetic-error'
    event=agq.wall(text,'synthetic-step',AT)
    assert event.resets_at == AT+timedelta(hours=2,minutes=3,seconds=4)
    assert event.error_id == 'synthetic-error'
    assert agq.wall(text,'synthetic-step',None).resets_at is None
    for suffix in ('', ' Resets in nonsense.', ' Resets in 3hgarbage.', ' Resets in '+ '9'*100+'h.'):
        assert agq.wall('Individual quota reached.'+suffix,'step',AT).resets_at is None


def test_banner_scan_is_bounded_and_stops_before_input_or_frame():
    for boundary in ('> typed draft', '─' * 40, ''):
        text = 'Individual quota reached.\n' + boundary + '\nResets in 7h.\nError ID: synthetic-unrelated'
        event = agq.wall(text, 'synthetic-step', AT)
        assert event.resets_at is None and event.error_id is None
    far = 'Individual quota reached.\n' + 'continuation\n' * 6 + 'Resets in 7h.\nError ID: synthetic-distant'
    event = agq.wall(far, 'synthetic-step', AT)
    assert event.resets_at is None and event.error_id is None
