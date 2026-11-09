"""A declared output allowance reaches endpoint clients without changing the default."""

import json

import pytest

from quarterdeck import endpoint, ship
from quarterdeck.harness.opencode import OpenCodeHarness
from quarterdeck.harness.pi import PiHarness
from quarterdeck.ship import Pool, SeatSpec, ShipError


def declared(tmp_path, output, *, endpoint_url=True, context=98304):
    text = '[ship]\nname="synthetic"\n[pools.local]\nprovider="local"\n'
    if endpoint_url:
        text += 'endpoint="http://example.invalid/v1"\n'
    else:
        text = text.replace('provider="local"', 'provider="pi"')
    text += f'context={context}\noutput={output}\n'
    (tmp_path / 'ship.toml').write_text(text)
    return ship.load(tmp_path).pool('local')


def test_declared_output_is_distinct_from_context_and_the_default_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(endpoint, 'models', lambda *a, **k: [endpoint.Served('synthetic', 98304)])
    pool = declared(tmp_path, '16384')
    choice = endpoint.choose(pool, 'synthetic')
    assert (choice.context, choice.output) == (98304, 16384)
    choice.write(tmp_path)
    assert endpoint.Choice.read(tmp_path) == choice
    assert endpoint.choose(Pool('local', 'local', endpoint=pool.endpoint), 'synthetic').output == 8192
    assert 'output 16,384 tokens (declared)' in endpoint.describe(pool)


@pytest.mark.parametrize('value', ['true', '0', '-1', '1.5', '"large"'])
def test_invalid_output_is_refused_at_the_declaration(tmp_path, value):
    with pytest.raises(ShipError, match='output must be a positive count of tokens'):
        declared(tmp_path, value)


def test_output_belongs_to_an_endpoint_and_must_leave_input_room(tmp_path, monkeypatch):
    with pytest.raises(ShipError, match='output is supported only for an endpoint pool'):
        declared(tmp_path, '1024', endpoint_url=False)
    with pytest.raises(ShipError, match='output must be smaller than context'):
        declared(tmp_path, '98304')
    monkeypatch.setattr(endpoint, 'models', lambda *a, **k: [endpoint.Served('synthetic', 8192)])
    with pytest.raises(ShipError, match='output must be smaller than.*context'):
        endpoint.choose(Pool('local', 'local', endpoint='http://example.invalid/v1', output=8192), 'synthetic')


@pytest.mark.parametrize('name', ['opencode', 'pi'])
def test_each_endpoint_adapter_supplies_the_explicit_allowance(tmp_path, monkeypatch, name):
    monkeypatch.setattr(endpoint, 'models', lambda *a, **k: [endpoint.Served('synthetic', 98304)])
    pool = declared(tmp_path, '16384')
    home = tmp_path / 'runtime/seats/synthetic/home'
    harness = {'opencode': OpenCodeHarness, 'pi': PiHarness}[name]()
    harness.build_home(home, SeatSpec('synthetic', 'Synthetic', 'worker', name, ('local',)), pool, None)
    if name == 'opencode':
        config = json.loads((home / 'config/opencode/opencode.json').read_text())
        model = config['provider']['local']['models']['synthetic']
        assert model['limit'] == {'context': 98304, 'output': 16384}
    else:
        config = json.loads((home / 'agent/models.json').read_text())
        model = config['providers']['local']['models'][0]
        assert (model['contextWindow'], model['maxTokens']) == (98304, 16384)


@pytest.mark.parametrize('name', ['opencode', 'pi'])
def test_declared_output_wins_over_a_different_native_model_limit(tmp_path, monkeypatch, name):
    monkeypatch.setattr(endpoint, 'models', lambda *a, **k: [endpoint.Served('synthetic', 98304)])
    pool = declared(tmp_path, '16384')
    template = tmp_path / 'template'
    template.mkdir()
    if name == 'opencode':
        (template / 'opencode.json').write_text(json.dumps({'provider': {'local': {'models': {'synthetic': {'limit': {'output': 1024}}}}}}))
    else:
        (template / 'models.json').write_text(json.dumps({'providers': {'local': {'models': [{'id': 'synthetic', 'maxTokens': 1024}]}}}))
    home = tmp_path / 'runtime/seats/synthetic/home'
    harness = {'opencode': OpenCodeHarness, 'pi': PiHarness}[name]()
    harness.build_home(home, SeatSpec('synthetic', 'Synthetic', 'worker', name, ('local',)), pool, template)
    if name == 'opencode':
        model = json.loads((home / 'config/opencode/opencode.json').read_text())['provider']['local']['models']['synthetic']
        assert model['limit']['output'] == 16384
    else:
        model = json.loads((home / 'agent/models.json').read_text())['providers']['local']['models'][0]
        assert model['maxTokens'] == 16384
