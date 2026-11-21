"""qd doctor's call: the provider's fixed address, the key in a header, no redirect followed, no body kept."""

from __future__ import annotations

import http.server
import threading

import pytest

from quarterdeck import doctor, key_proof, keyfile
from quarterdeck.ship import Pool

KEY = 'sk-synthetic-Q7wM2xC9zT4lP8rV3nH6'
TAIL = KEY[-8:]


@pytest.fixture
def server():
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, dict(self.headers)))
            code = self.server.answer
            self.send_response(code)
            if code == 302:
                self.send_header('Location', f'http://127.0.0.1:{self.server.server_port}/elsewhere')
            body = f'{{"error": "the key {KEY} {TAIL} was refused"}}'.encode()
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    http_server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    http_server.answer, http_server.seen = 200, seen
    thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    thread.start()
    try:
        yield http_server
    finally:
        http_server.shutdown()
        thread.join()
        http_server.server_close()


def url(server, path='/v1'):
    return f'http://127.0.0.1:{server.server_port}{path}'


@pytest.mark.parametrize('provider,header,sent', [('anthropic', 'x-api-key', KEY), ('openai', 'Authorization', f'Bearer {KEY}'),
                                                  ('xai', 'Authorization', f'Bearer {KEY}'), ('google', 'x-goog-api-key', KEY)])
def test_each_provider_is_asked_at_its_fixed_address_with_the_key_in_a_header(server, key_file, tmp_path, monkeypatch,
                                                                              provider, header, sent):
    key_file.write(paid=KEY)
    fixed = key_proof.ADDRESSES[provider]
    assert fixed.url.startswith('https://') and KEY not in fixed.url and fixed.header == header
    monkeypatch.setitem(key_proof.ADDRESSES, provider, key_proof.Address(url(server, '/models'), fixed.header, fixed.bearer, fixed.extra))
    assert key_proof.prove(Pool('p', provider, api_key='keys:paid', ship_folder=tmp_path)) == (True, 'key works')
    path, headers = server.seen[0]
    assert path == '/models' and KEY not in path
    assert {k.lower(): v for k, v in headers.items()}[header.lower()] == sent


@pytest.mark.parametrize('answer,passed,words', [
    (200, True, 'answers with this key; it may not need one'), (401, False, 'key refused by the provider'),
    (403, False, 'key refused by the provider'), (302, False, 'redirect'), (500, False, 'answered 500'),
])
def test_an_endpoints_answer_is_read_by_its_status_alone(server, key_file, tmp_path, answer, passed, words):
    key_file.write(paid=KEY)
    server.answer = answer
    found, line = key_proof.prove(Pool('p', 'local', endpoint=url(server), api_key='keys:paid', ship_folder=tmp_path))
    assert found is passed and words in line and KEY not in line and TAIL not in line
    assert [path for path, _ in server.seen] == ['/v1/models'], 'one call, and a redirect is not followed'


def test_openrouter_is_proven_by_its_key_route_since_its_list_is_public():
    pool = Pool('p', 'openrouter', endpoint='https://openrouter.ai/api/v1', api_key='keys:paid')
    found, conclusive = key_proof.address(pool)
    assert found.url == 'https://openrouter.ai/api/v1/key' and conclusive


def test_a_provider_with_no_such_call_says_so(key_file, tmp_path):
    key_file.write(paid=KEY)
    assert key_proof.prove(Pool('p', 'meta', api_key='keys:paid', ship_folder=tmp_path)) == (
        True, 'this provider has no call that proves a key without spending')


def test_an_address_that_does_not_answer_is_said_without_the_key(key_file, tmp_path, server):
    key_file.write(paid=KEY)
    gone = url(server)
    server.shutdown()
    server.server_close()
    server.shutdown = lambda: None
    server.server_close = lambda: None
    found, line = key_proof.prove(Pool('p', 'local', endpoint=gone, api_key='keys:paid', ship_folder=tmp_path))
    assert not found and line == 'could not reach 127.0.0.1' and KEY not in line


def test_a_key_a_header_cannot_hold_is_never_echoed():
    assert key_proof.status(key_proof.Address('http://127.0.0.1:9/x', 'x-api-key'), 'bad\nvalue', timeout=1) is None


def test_the_doctor_says_present_works_missing_and_refused_and_names_the_file(server, key_file, tmp_path):
    pool = Pool('p', 'local', endpoint=url(server), api_key='keys:paid', ship_folder=tmp_path)
    absent = doctor.pool_check(pool)
    assert not absent.passed and absent.detail == f'key missing: no key file at {key_file.path}' and server.seen == []
    key_file.write(other=KEY)
    named = doctor.pool_check(pool)
    assert not named.passed and named.detail == f'key missing: key paid is not in {key_file.path}'
    key_file.write(paid=KEY)
    good = doctor.pool_check(pool)
    assert good.passed and good.detail == f'key present in {key_file.path} · answers with this key; it may not need one'
    server.answer = 401
    bad = doctor.pool_check(pool)
    assert not bad.passed and bad.detail == f'key present in {key_file.path} · key refused by the provider'
    server.answer = 200
    key_file.path.chmod(0o644)  # noted, and refused for nothing
    widened = doctor.pool_check(pool)
    assert widened.passed and widened.detail.endswith('other accounts can read the key file; 0600 is advised')
    key_file.path.write_text(f'paid = "{KEY}\n')
    broken = doctor.pool_check(pool)
    assert not broken.passed and broken.detail.startswith('key refused: ') and KEY not in broken.detail


@pytest.mark.parametrize('address,sent', [
    ('https://api.anthropic.com/v1', {'anthropic-version': '2023-06-01'}),
    ('https://api.anthropic.com.example.invalid/v1', {}),
    ('https://example.invalid/api.anthropic.com/v1', {}),
    ('https://openrouter.ai/api/v1', {}),
])
def test_only_anthropics_own_host_is_sent_its_version_header(address, sent):
    assert key_proof.listing_headers(address) == sent


def test_the_launch_check_and_the_doctor_both_send_it_beside_the_bearer_key(server, key_file, tmp_path, monkeypatch):
    from quarterdeck import endpoint
    key_file.write(paid=KEY)
    monkeypatch.setattr(key_proof, 'ANTHROPIC_HOST', '127.0.0.1')  # the local server stands in for its host
    pool = Pool('p', 'anthropic', endpoint=url(server), api_key='keys:paid', ship_folder=tmp_path)
    with pytest.raises(Exception):
        endpoint.choose(pool, None)  # this stand-in's body is no model listing; its request is what is read here
    assert key_proof.prove(pool)[0] is True
    assert len(server.seen) == 2
    for path, headers in server.seen:
        lowered = {name.lower(): value for name, value in headers.items()}
        assert path == '/v1/models' and lowered['authorization'] == f'Bearer {KEY}'
        assert lowered['anthropic-version'] == '2023-06-01'
