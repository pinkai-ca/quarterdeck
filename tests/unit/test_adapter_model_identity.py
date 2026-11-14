"""Ordinary observed model names: native records, display case and resolved alarms."""
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from quarterdeck import model_mismatch, post
from quarterdeck.harness.antigravity import AntigravityHarness
from quarterdeck.harness.codex import CodexHarness
from quarterdeck.harness.grok import GrokHarness
from quarterdeck.harness.opencode import OpenCodeHarness
from quarterdeck.harness.pi import PiHarness


FIXTURES = Path(__file__).parents[1] / 'fixtures'


def codex_seat(tmp_path, monkeypatch, *, declared='gpt-6.1-sol', recorded=None):
    harness = CodexHarness()
    record = tmp_path / 'rollout.jsonl'
    rows = [dict(type='session_meta', payload=dict(id='synthetic-session'))]
    if recorded is not None:
        rows.append(dict(type='turn_context', payload=dict(model=recorded)))
    record.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    launched = SimpleNamespace(harness='codex', version='0.166.0')
    pane = SimpleNamespace(id='%1', pid=123, dead=False)
    seat = SimpleNamespace(name='sample', harness=harness, ship=SimpleNamespace(runtime=tmp_path / 'runtime'),
                           spec=SimpleNamespace(model=declared, model_mode='named'),
                           launched=lambda: launched, pane=lambda: pane, record=lambda: record)
    raw = (FIXTURES / 'codex/0.166.0/idle.txt').read_text().replace('GPT-6-Astra', 'GPT-6.1-Sol')
    monkeypatch.setattr(model_mismatch, 'Door', lambda seat: SimpleNamespace(screen=lambda: harness.screen(raw)))
    return seat


def test_codex_display_case_footer_matches_declared_id(tmp_path, monkeypatch):
    seat = codex_seat(tmp_path, monkeypatch)
    reading = model_mismatch.observe(seat)
    assert reading.status == 'match'
    assert reading.declared == reading.running == 'gpt-6.1-sol'
    assert reading.source == 'screen'


@pytest.mark.parametrize('declared', ['gpt-6-astra', 'gpt-6.1', 'gpt61sol', 'gpt-6.1-sol-extra'])
def test_codex_other_models_and_punctuation_remain_different(tmp_path, monkeypatch, declared):
    seat = codex_seat(tmp_path, monkeypatch, declared=declared)
    assert model_mismatch.observe(seat).status == 'mismatch'


@pytest.mark.parametrize('recorded,status', [('gpt-6.1-sol', 'match'), ('gpt-6-astra', 'mismatch')])
def test_codex_native_context_model_is_read_before_screen(tmp_path, monkeypatch, recorded, status):
    seat = codex_seat(tmp_path, monkeypatch, recorded=recorded)
    def no_screen(seat):
        pytest.fail('an available native model must not read the screen')
    monkeypatch.setattr(model_mismatch, 'Door', no_screen)
    reading = model_mismatch.observe(seat)
    assert reading.status == status
    assert reading.source == 'record' and reading.running == recorded


def test_existing_display_case_alarm_clears_on_next_reading(tmp_path, monkeypatch):
    seat = codex_seat(tmp_path, monkeypatch)
    runtime = seat.ship.runtime
    prior = dict(version=1, key=['codex', 'gpt-6.1-sol', 'GPT-6.1-Sol'], active=True, reports=[], source='screen')
    model_mismatch.save(runtime, seat.name, prior)
    service = SimpleNamespace(store=post.Store(runtime), systems_recipients=lambda seat: [])
    model_mismatch.step(service, seat, lambda seat, expected: False)
    saved = model_mismatch.load(runtime, seat.name)
    assert saved['active'] is False
    assert saved['key'] == prior['key']  # the old observation remains as history
    assert service.store.all() == []


def test_pi_measured_native_id_needs_no_alias():
    harness = PiHarness()
    model = harness.record_model(FIXTURES / 'pi/0.74.2/session.jsonl')
    assert model == 'qwen3.8-27b'
    assert model_mismatch.compare(harness, 'qwen3.8-27b', model).status == 'match'
    raw = (FIXTURES / 'pi/0.79.0/idle-256.txt').read_text()
    assert model_mismatch.compare(harness, 'sample-model', harness.screen(raw).shows['model']).status == 'match'


def test_opencode_measured_native_id_needs_no_display_alias(tmp_path):
    rows = json.loads((FIXTURES / 'opencode/1.19.5/record.json').read_text())
    record = tmp_path / 'opencode.db'
    with sqlite3.connect(record) as con:
        con.execute('CREATE TABLE session (id TEXT, time_created INTEGER, time_updated INTEGER, parent_id TEXT)')
        con.execute('CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT)')
        con.execute('CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT)')
        con.execute('INSERT INTO session VALUES (:id, :time_created, :time_updated, :parent_id)', rows['session'])
        con.executemany('INSERT INTO message VALUES (:id, :session_id, :time_created, :time_updated, :data)', rows['messages'])
        con.executemany('INSERT INTO part VALUES (:id, :message_id, :session_id, :time_created, :time_updated, :data)', rows['parts'])
    harness = OpenCodeHarness()
    model = harness.record_model(record)
    assert model == 'qwen3.8-27b'
    assert model_mismatch.compare(harness, 'qwen3.8-27b', model).status == 'match'
    # Its footer combines a human model label and endpoint; the adapter does not claim a model id there.
    assert 'model' not in harness.screen((FIXTURES / 'opencode/1.19.5/idle.txt').read_text()).shows


def test_grok_billing_key_does_not_establish_a_running_native_id():
    harness = GrokHarness()
    model = harness.record_model(FIXTURES / 'grok/1.0.77/lifecycle.jsonl')
    assert model is None
    assert model_mismatch.compare(harness, 'grok-4.7-build', model).status == 'unknown'


def test_grok_display_name_is_no_identity_reading_and_creates_no_alarm(tmp_path, monkeypatch):
    seat = codex_seat(tmp_path, monkeypatch)
    seat.harness = GrokHarness()
    launched = SimpleNamespace(harness='grok', version='1.0.77')
    seat.launched = lambda: launched
    seat.spec.model = 'grok-4.7'
    seat.record = lambda: None
    capture = json.loads((FIXTURES / 'grok/1.0.77/idle-mono.json').read_text())
    screen = seat.harness.screen('\n'.join(capture['lines']))
    assert screen.shows['model'] == 'Grok 4.7'
    monkeypatch.setattr(model_mismatch, 'Door', lambda seat: SimpleNamespace(screen=lambda: screen))
    assert model_mismatch.observe(seat).status == 'unknown'
    service = SimpleNamespace(store=post.Store(seat.ship.runtime), systems_recipients=lambda seat: ['systems'])
    model_mismatch.step(service, seat, lambda seat, expected: False)
    assert not model_mismatch.path(seat.ship.runtime, seat.name).exists()
    assert service.store.all() == []


@pytest.mark.parametrize('native,shown', [
    ('gemini-3.8-flash-low', 'Gemini 3.8 Flash (Low)'),
    ('gemini-4-argon-low', 'Gemini 4 Argon (Low)'),
    ('claude-sonnet-5-5-low', 'Claude Sonnet 5.5 (Low)'),
])
def test_antigravity_reuses_its_measured_identity_table(native, shown):
    harness = AntigravityHarness()
    assert model_mismatch.compare(harness, native, shown).status == 'match'
    assert model_mismatch.compare(harness, native, shown + ' other').status == 'mismatch'
