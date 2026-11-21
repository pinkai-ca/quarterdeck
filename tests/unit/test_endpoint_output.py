"""A declared output allowance reaches endpoint clients without changing the default."""

import json
from pathlib import Path

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
        text = text.replace('provider="local"', 'provider="openai"')
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


@pytest.mark.parametrize('name', ['opencode', 'pi'])
@pytest.mark.parametrize('address,native', [
    ('https://api.openai.com/v1', True),
    ('https://api.openai.com/v1/', True),
    ('http://127.0.0.1:8000/v1', False),
    ('https://openrouter.ai/api/v1', False),
    ('https://api.anthropic.com/v1', False),
    ('https://api.openai.com.example.invalid/v1', False),
    ('https://example.invalid/api.openai.com/v1', False),
    ('https://api.openai.com/other/v1', False),
    ('https://api.openai.com:5443/v1', False),
])
def test_endpoint_transport_follows_the_exact_url(tmp_path, monkeypatch, name, address, native):
    monkeypatch.setattr(endpoint, 'models', lambda *a, **k: [endpoint.Served('decoy'), endpoint.Served('selected')])
    monkeypatch.setattr(endpoint, '_secret', lambda pool: 'synthetic-key')
    pool = Pool('custom', 'openai', endpoint=address, api_key='keys:unused')
    home = tmp_path / 'runtime/seats/synthetic/home'
    harness = {'opencode': OpenCodeHarness, 'pi': PiHarness}[name]()
    seat = SeatSpec('synthetic', 'Synthetic', 'worker', name, (pool.name,), model='selected')
    harness.build_home(home, seat, pool, None)
    assert endpoint.Choice.read(home).model == 'selected'
    if name == 'opencode':
        config = json.loads((home / 'config/opencode/opencode.json').read_text())
        provider = config['provider'][pool.name]
        assert provider['npm'] == ('@ai-sdk/openai' if native else '@ai-sdk/openai-compatible')
        assert provider['options'] == {'baseURL': address, 'apiKey': '{env:QD_ENDPOINT_KEY}'}
        assert provider['models']['selected']['limit']['output'] == endpoint.DEFAULT_OUTPUT
        assert config.get('small_model') == ('custom/selected' if native else None)
    else:
        config = json.loads((home / 'agent/models.json').read_text())
        provider = config['providers'][pool.name]
        assert provider['api'] == ('openai-responses' if native else 'openai-completions')
        assert (provider['baseUrl'], provider['apiKey']) == (address, 'QD_ENDPOINT_KEY')
        assert provider['models'][0]['maxTokens'] == endpoint.DEFAULT_OUTPUT
        launch = harness.launch(seat, pool, home, tmp_path / 'brief.md', None, None)
        assert '--no-extensions' in launch.argv
        assert ('--extension' in launch.argv) is native
        if native:
            hook = launch.argv[launch.argv.index('--extension') + 1]
            assert hook.endswith('/pi_responses.ts') and Path(hook).is_file()
            assert launch.env['QD_PI_RESPONSES_PROVIDER'] == pool.name
            assert launch.env['QD_PI_RESPONSES_BASE_URL'] == endpoint.OPENAI_RESPONSES_BASE


@pytest.mark.parametrize('name', ['opencode', 'pi'])
def test_layer_with_different_address_keeps_compatible_transport(tmp_path, monkeypatch, name):
    monkeypatch.setattr(endpoint, 'models', lambda *a, **k: [endpoint.Served('selected')])
    pool = Pool('custom', 'openai', endpoint=endpoint.OPENAI_RESPONSES_BASE)
    home = tmp_path / 'runtime/seats/synthetic/home'
    seat = SeatSpec('synthetic', 'Synthetic', 'worker', name, (pool.name,))
    override = 'https://example.invalid/v1'
    if name == 'opencode':
        filename, group, transport = 'opencode.json', 'provider', 'npm'
        layer = {'options': {'baseURL': override}}
        path = home / 'config/opencode' / filename
        compatible = '@ai-sdk/openai-compatible'
        harness = OpenCodeHarness()
    else:
        filename, group, transport = 'models.json', 'providers', 'api'
        layer = {'baseUrl': override}
        path = home / 'agent' / filename
        compatible = 'openai-completions'
        harness = PiHarness()
    (tmp_path / filename).write_text(json.dumps({group: {pool.name: layer}}))
    harness.build_home(home, seat, pool, tmp_path)
    assert json.loads(path.read_text())[group][pool.name][transport] == compatible
