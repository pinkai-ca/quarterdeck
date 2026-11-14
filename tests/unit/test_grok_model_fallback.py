"""Synthetic examples of the measured 1.0.77 selection/cache shapes; no model turn."""

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import doctor, ledger, watch
from quarterdeck.door import Reading
from quarterdeck.harness import get
from quarterdeck.harness.grok import GrokHarness
from quarterdeck.harness.grok_models import EFFORT_REFUSAL, MODEL_REFUSAL
from quarterdeck.ship import Pool, SeatSpec, Session, Ship


FIXTURES = Path(__file__).parents[1] / 'fixtures/grok/1.0.77'


def cache(home):
    models = {name: {'info': {'id': name, 'model': name, 'name': label}}
              for name, label in [('grok-4.7', 'Grok 4.7'),
                                  ('grok-4.7-build-fast', 'Grok 4.7 Fast'),
                                  ('grok-4.6', 'Grok 4.6'), ('grok-4.5', 'Grok 4.5')]}
    data = {'grok_version': '1.0.77', 'models': models}
    (home / 'models_cache.json').write_text(json.dumps(data))
    return data


def screen(warnings=True):
    rows = json.loads((FIXTURES / 'idle-mono.json').read_text())['lines']
    # Rebuild a fictional screen using only the measured native warning/footer words.
    rows = [row.replace(' Grok 4.7 (high)', '─ Grok 4.7 (low)') for row in rows]
    if warnings:
        rows[:0] = ['     ' + EFFORT_REFUSAL, '', '     ' + MODEL_REFUSAL, '']
    return GrokHarness().screen('\n'.join(rows))


def record(tmp_path, model='grok-4.7'):
    def row(event, time):
        return {'timestamp': time, 'method': '_x.ai/session/update',
                'params': {'sessionId': 'fictional-model-example', 'update': event}}
    rows = [row({'sessionUpdate': 'user_message_chunk',
                 'content': {'type': 'text', 'text': 'Fictional input.'},
                 '_meta': {'modelId': model}}, 2208988800),
            row({'sessionUpdate': 'turn_completed', 'prompt_id': 'fictional-prompt',
                 'usage': {'modelUsage': {'grok-4.7-build': {}},
                           'inputTokens': 8, 'cachedReadTokens': 2,
                           'outputTokens': 3, 'cacheCreationTokens': 0}}, 2208988801)]
    path = tmp_path / 'model-example.jsonl'
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    return path


def test_input_metadata_names_running_model_and_usage_keeps_its_billing_key(tmp_path):
    adapter = GrokHarness()
    path = record(tmp_path)
    state, turns = adapter.read(path)
    assert state.model == adapter.record_model(path) == 'grok-4.7'
    assert turns[0].model == 'grok-4.7-build'
    assert turns[0].tokens.read_uncached == 6
    assert turns[0].tokens.written == 3


@pytest.mark.parametrize('model', [None, '', {}, 7])
def test_missing_or_unreadable_model_metadata_does_not_adopt_the_billing_key(tmp_path, model):
    state, turns = GrokHarness().read(record(tmp_path, model))
    assert state.model is None
    assert turns[0].model == 'grok-4.7-build'


def test_screen_keeps_warning_words_and_observed_model_effort():
    shown = screen()
    assert shown.shows == {'model': 'Grok 4.7', 'effort': 'low',
                           'selection_warning': MODEL_REFUSAL + '; ' + EFFORT_REFUSAL}
    plain = screen(False)
    assert shown.box == plain.box and shown.working is plain.working is False
    assert shown.unknown is plain.unknown is None
    assert 'selection_warning' not in plain.shows
    unfamiliar = GrokHarness().screen('Unknown model error\nGrok 9 (medium)')
    assert unfamiliar.shows == {}


@pytest.mark.parametrize('with_record', [False, True])
def test_status_and_watch_show_observed_fallback_before_and_after_a_turn(tmp_path, with_record):
    adapter = GrokHarness()
    seat = SimpleNamespace(record=lambda: record(tmp_path) if with_record else None, harness=adapter)
    shown = screen()
    observed = watch.running_settings(seat, screen=shown)
    expected = 'grok-4.7' if with_record else 'Grok 4.7'
    assert observed['model'] == expected and observed['effort'] == 'low'
    reading = watch.SeatReading('worker', watch.Activity('idle'), Reading(True, 'empty box'),
                                None, ledger.Count(0, 0), 'subscription', observed['model'], 'grok',
                                declared_model='grok-4.7-build', declared_effort='high',
                                effort=observed['effort'], version='1.0.77',
                                selection_warning=observed['selection_warning'])
    table = '\n'.join(watch.render_table(dict(service='ok', seats=[reading], open_orders=[], usage=[])))
    full = '\n'.join(watch.render_full(dict(seat=reading, record=None, state=None, record_absent=None,
                                          record_limits=None, screen=shown, usage=[])))
    for text in (table, full):
        assert f'fallback: declared grok-4.7-build / high, running {expected} / low' in text
        assert MODEL_REFUSAL in text and EFFORT_REFUSAL in text
    unknown = replace(reading, model=None, effort=None)
    assert 'running unknown / unknown' in unknown.selection_text


def test_the_programs_cached_list_uses_ids_not_labels_or_billing_keys(tmp_path):
    cache(tmp_path)
    adapter = GrokHarness()
    models = adapter.listed_models(tmp_path, version='1.0.77')
    assert models == {'grok-4.7': 'Grok 4.7', 'grok-4.7-build-fast': 'Grok 4.7 Fast',
                      'grok-4.6': 'Grok 4.6', 'grok-4.5': 'Grok 4.5'}
    assert 'grok-4.7-build' not in models
    assert 'Grok 4.7' not in models


@pytest.mark.parametrize('problem', ['missing', 'malformed', 'empty', 'wrong-version',
                                    'inconsistent-id', 'incomplete-entry', 'unmeasured-version'])
def test_unknown_cache_cannot_prove_a_model_missing(tmp_path, problem):
    data = cache(tmp_path)
    path = tmp_path / 'models_cache.json'
    if problem == 'missing':
        path.unlink()
    elif problem == 'malformed':
        path.write_text('{')
    else:
        if problem == 'empty':
            data['models'] = {}
        elif problem == 'wrong-version':
            data['grok_version'] = 'unmeasured'
        elif problem == 'inconsistent-id':
            data['models']['grok-4.7']['info']['id'] = 'other'
        elif problem == 'incomplete-entry':
            data['models']['grok-4.5'] = {}
        path.write_text(json.dumps(data))
    version = 'unmeasured' if problem == 'unmeasured-version' else '1.0.77'
    assert GrokHarness().listed_models(tmp_path, version=version) is None


@pytest.mark.parametrize('model,known,passed', [('grok-4.7', True, True),
                                             ('grok-4.7-build', True, False),
                                             ('grok-4.7-build', False, True)])
def test_doctor_distinguishes_missing_ids_from_unknown_lists(tmp_path, monkeypatch, model, known, passed):
    pool = Pool('subscription', 'xai')
    spec = SeatSpec('worker', 'crew', 'worker', 'grok', ('subscription',), model=model, cwd=tmp_path)
    ship = Ship(tmp_path, 'fictional', 'operator', tmp_path, {'subscription': pool},
                {'crew': Session('crew', 'qd')}, {'worker': spec})
    home = ship.runtime / 'seats/worker/home'
    home.mkdir(parents=True)
    if known:
        cache(home)
    monkeypatch.setattr(GrokHarness, 'preflight', lambda *args: None)
    monkeypatch.setattr(GrokHarness, 'workspace_notice', lambda *args: None)
    check = next(c for c in doctor.seat_readiness(ship, versions={'grok': '1.0.77'})
                 if c.name == 'seat worker pool subscription model')
    assert check.passed is passed
    assert ('unknown' in check.detail) is (not known)
    if known:
        assert ('not listed by the program' in check.detail) is (not passed)
    assert not (home / 'config.toml').exists(), 'the check writes nothing and launches nothing'


def test_an_unmeasured_program_list_is_explicitly_unknown(tmp_path):
    assert get('claude').listed_models(tmp_path) is None
