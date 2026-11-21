"""Discovery, doctor and both homes against a synthetic keyed loopback server."""

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from threading import Thread

import pytest

from quarterdeck import doctor, endpoint, keyfile
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


def test_keyed_discovery_doctor_and_adapter_homes_never_store_the_key(server, tmp_path, key_file):
    http, url = server
    key_file.write(synthetic_endpoint_key='synthetic-key', synthetic_bad_key='synthetic-rejected-key')
    pool = Pool('synthetic', 'local', endpoint=url, api_key='keys:synthetic-endpoint-key', ship_folder=tmp_path)
    assert endpoint.choose(pool, None).model == 'synthetic-model'
    assert endpoint.describe(pool) == f'{url} answers; serves synthetic-model (4,096 tokens)'
    proven = doctor.pool_check(pool)  # one call of its own, its answer's body unread
    assert proven.passed and 'synthetic-key' not in proven.detail
    for harness in (OpenCodeHarness(), PiHarness()):
        spec = SeatSpec('worker', 'crew', 'worker', harness.name, ('synthetic',))
        home = tmp_path/harness.name/'runtime'/'seats'/'worker'/'home'
        assert harness.build_home(home, spec, pool, None).variable == endpoint.KEY_VARIABLE
        launch = harness.launch(spec, pool, home, tmp_path/'brief', None, None)
        assert launch.env[endpoint.KEY_VARIABLE] == pool.api_key, 'its reference: the launcher fills the variable'
        assert all('synthetic-key' not in file.read_text() for file in home.rglob('*') if file.is_file())
    assert http.seen == [('/v1/models', 'Bearer synthetic-key')]*5  # choose, describe, doctor and the two homes
    with pytest.raises(ShipError, match='answered 401') as refused:
        endpoint.choose(Pool('bad', 'local', endpoint=url, api_key='keys:synthetic-bad-key', ship_folder=tmp_path), None)
    assert 'synthetic-rejected-key' not in str(refused.value)


def test_keyless_local_discovery_sends_no_authorization(server):
    http, url = server
    http.expected = None
    pool = Pool('local', 'local', endpoint=url)
    assert endpoint.choose(pool, None).model == 'synthetic-model'
    assert doctor.pool_check(pool).passed
    assert http.seen == [('/v1/models', None)]*2


def test_authenticated_request_error_cannot_echo_its_header(server, monkeypatch, tmp_path):
    """The key file refuses such a value; a header's own error is still never quoted, whatever reaches it."""
    _, url = server
    monkeypatch.setattr(keyfile, 'value', lambda pool: 'synthetic-private-value\ninvalid-header')
    monkeypatch.setattr(keyfile, 'problem', lambda pool: None)
    pool = Pool('synthetic', 'local', endpoint=url, api_key='keys:synthetic-broken-key', ship_folder=tmp_path)
    with pytest.raises(ShipError, match='ValueError') as error:
        endpoint.choose(pool, None)
    assert 'synthetic-private-value' not in str(error.value) and 'invalid-header' not in str(error.value)
    monkeypatch.setattr(keyfile, 'status_line', lambda pool: f'pool {pool.name}: key present')
    check = doctor.pool_check(pool)
    assert not check.passed and 'synthetic-private-value' not in check.detail


def test_a_listing_with_a_key_follows_no_redirect():
    seen = []

    class Redirecting(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get('Authorization')))
            self.send_response(302)
            self.send_header('Location', f'http://127.0.0.1:{self.server.server_port}/elsewhere')
            self.end_headers()

        def log_message(self, *args):
            pass

    http = HTTPServer(('127.0.0.1', 0), Redirecting)
    thread = Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(ShipError, match='answered 302') as refused:
            endpoint.models(f'http://127.0.0.1:{http.server_port}/v1', api_key='synthetic-key')
    finally:
        http.shutdown()
        thread.join()
        http.server_close()
    assert seen == [('/v1/models', 'Bearer synthetic-key')] and 'synthetic-key' not in str(refused.value)
