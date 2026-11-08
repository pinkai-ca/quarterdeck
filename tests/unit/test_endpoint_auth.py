"""Discovery, doctor and both homes against a synthetic keyed loopback server."""

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from threading import Thread

import pytest

from quarterdeck import doctor, endpoint
from quarterdeck.harness.opencode import OpenCodeHarness
from quarterdeck.harness.pi import PiHarness
from quarterdeck.ship import Pool, SeatSpec, ShipError


@pytest.fixture
def server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            supplied = self.headers.get('Authorization')
            self.server.seen.append((self.path, supplied))
            if supplied != self.server.expected:
                self.send_error(401, 'synthetic authorization refusal')
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'data': [{'id': 'synthetic-model', 'max_model_len': 4096}]}).encode())

        def log_message(self, *args):
            pass

    http = HTTPServer(('127.0.0.1', 0), Handler)
    http.expected, http.seen = 'Bearer synthetic-key', []
    thread = Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield http, f'http://127.0.0.1:{http.server_port}/v1'
    finally:
        http.shutdown()
        thread.join()
        http.server_close()


@pytest.mark.parametrize('source', ['env', 'file'])
def test_keyed_discovery_doctor_and_adapter_homes_never_store_the_key(server, tmp_path, monkeypatch, source):
    http, url = server
    if source == 'env':
        monkeypatch.setenv('SYNTHETIC_ENDPOINT_KEY', 'synthetic-key')
        key = 'env:SYNTHETIC_ENDPOINT_KEY'
    else:
        secret = tmp_path/'synthetic-key-source'
        secret.write_text('synthetic-key\n')
        key = f'file:{secret}'
    pool = Pool('synthetic', 'local', endpoint=url, api_key=key)
    assert endpoint.choose(pool, None).model == 'synthetic-model'
    assert endpoint.describe(pool) == f'{url} answers; serves synthetic-model (4,096 tokens)'
    assert doctor.pool_check(pool).passed
    for harness in (OpenCodeHarness(), PiHarness()):
        spec = SeatSpec('worker', 'crew', 'worker', harness.name, ('synthetic',))
        home = tmp_path/harness.name/'runtime'/'seats'/'worker'/'home'
        assert harness.build_home(home, spec, pool, None).variable == endpoint.KEY_VARIABLE
        launch = harness.launch(spec, pool, home, tmp_path/'brief', None, None)
        assert launch.env[endpoint.KEY_VARIABLE] == 'synthetic-key'
        assert all('synthetic-key' not in file.read_text() for file in home.rglob('*') if file.is_file())
    assert http.seen == [('/v1/models', 'Bearer synthetic-key')]*5
    monkeypatch.setenv('SYNTHETIC_BAD_KEY', 'synthetic-rejected-key')
    with pytest.raises(ShipError, match='answered 401') as refused:
        endpoint.choose(Pool('bad', 'local', endpoint=url, api_key='env:SYNTHETIC_BAD_KEY'), None)
    assert 'synthetic-rejected-key' not in str(refused.value)


def test_keyless_local_discovery_sends_no_authorization(server):
    http, url = server
    http.expected = None
    pool = Pool('local', 'local', endpoint=url)
    assert endpoint.choose(pool, None).model == 'synthetic-model'
    assert doctor.pool_check(pool).passed
    assert http.seen == [('/v1/models', None)]*2


def test_authenticated_request_error_cannot_echo_its_header(server, monkeypatch):
    _, url = server
    monkeypatch.setenv('SYNTHETIC_BROKEN_KEY', 'synthetic-private-value\ninvalid-header')
    pool = Pool('synthetic', 'local', endpoint=url, api_key='env:SYNTHETIC_BROKEN_KEY')
    with pytest.raises(ShipError, match='ValueError') as error:
        endpoint.choose(pool, None)
    assert 'synthetic-private-value' not in str(error.value) and 'invalid-header' not in str(error.value)
    check = doctor.pool_check(pool)
    assert not check.passed and 'synthetic-private-value' not in check.detail
