"""A key is sent only where it should go, and no refusal shows it. Synthetic keys only; nothing here opens a real key."""

from __future__ import annotations

import http.server
import re
import threading
import traceback
import urllib.request

import pytest

from quarterdeck import endpoint, forward_post, key_proof, setup
from quarterdeck import ship as shipmod
from quarterdeck.ship import ShipError

KEY = 'sk-synthetic-Q7wM2xC9zT4lP8rV3nH6'
PARTS = (KEY, KEY[3:], KEY[-8:], 'Q7wM2xC9')


def shows_none(text: str) -> bool:
    return not any(part in text for part in PARTS)


def whole(error: BaseException) -> str:
    """The error as a traceback prints it, with everything chained to it."""
    return ''.join(traceback.format_exception(error))


class _Recording(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.seen.append((self.path, dict(self.headers)))
        body = b'{"data": [{"id": "synthetic"}]}'
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def serve():
    running = []

    def start():
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Recording)
        server.seen = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        running.append((server, thread))
        return server

    yield start
    for server, thread in running:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.fixture
def proxied(serve, monkeypatch):
    """A recording proxy named by the environment, and the endpoint it must not stand in front of."""
    proxy, target = serve(), serve()
    for name in ('no_proxy', 'NO_PROXY', 'all_proxy', 'ALL_PROXY', 'HTTP_PROXY', 'HTTPS_PROXY'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('http_proxy', f'http://127.0.0.1:{proxy.server_port}')
    monkeypatch.setenv('https_proxy', f'http://127.0.0.1:{proxy.server_port}')
    return proxy, target


def test_the_doctors_keyed_call_over_http_gives_a_proxy_nothing(proxied):
    proxy, target = proxied
    found = key_proof.Address(f'http://127.0.0.1:{target.server_port}/v1/models', 'Authorization', bearer=True)
    assert key_proof.status(found, KEY) == 200
    assert proxy.seen == []
    assert target.seen[0][1]['Authorization'] == f'Bearer {KEY}'


def test_the_keyed_listing_over_http_gives_a_proxy_nothing(proxied):
    proxy, target = proxied
    assert [row.id for row in endpoint.models(f'http://127.0.0.1:{target.server_port}/v1', api_key=KEY)] == ['synthetic']
    assert proxy.seen == []
    assert target.seen[0][1]['Authorization'] == f'Bearer {KEY}'


def test_a_keyed_call_over_https_keeps_its_proxy_which_sees_only_connect(proxied):
    proxy, _ = proxied
    opener = key_proof.keyed_opener('https://provider.invalid/v1/models')
    kept = [handler.proxies for handler in opener.handlers if isinstance(handler, urllib.request.ProxyHandler)]
    assert kept and kept[0]['https'] == f'http://127.0.0.1:{proxy.server_port}'


def ship_file(folder, line: str, key: bool = True):
    keyed = 'api_key = "keys:paid"\n' if key else ''
    (folder / 'ship.toml').write_text(f'[pools.remote]\nprovider = "openrouter"\nendpoint = "{line}"\n{keyed}')


@pytest.mark.parametrize('url', ['http://remote.invalid/v1', 'http://10.0.0.5:8000/v1', 'http://0.0.0.0:8000/v1',
                                 'http://127.0.0.1.remote.invalid/v1', 'http://localhost.remote.invalid/v1'])
def test_a_key_beside_a_remote_plain_http_endpoint_is_refused(tmp_path, url):
    ship_file(tmp_path, url)
    with pytest.raises(ShipError, match='a key travels only over https'):
        shipmod.load(tmp_path)


@pytest.mark.parametrize('url', ['http://127.0.0.1:8000/v1', 'http://127.5.5.5:8000/v1', 'http://localhost:8000/v1',
                                 'http://[::1]:8000/v1', 'https://remote.invalid/v1'])
def test_a_key_over_loopback_or_https_is_accepted(tmp_path, url):
    ship_file(tmp_path, url)
    assert shipmod.load(tmp_path).pools['remote'].api_key == 'keys:paid'


def test_a_remote_plain_http_endpoint_with_no_key_still_loads(tmp_path):
    ship_file(tmp_path, 'http://remote.invalid/v1', key=False)
    assert shipmod.load(tmp_path).pools['remote'].endpoint == 'http://remote.invalid/v1'


def test_the_ship_files_toml_error_shows_no_part_of_a_key_used_twice_as_a_table_name(tmp_path):
    (tmp_path / 'ship.toml').write_text(f'[ship]\nname = "t"\n["{KEY}"]\na = 1\n["{KEY}"]\nb = 2\n')
    with pytest.raises(ShipError) as caught:
        shipmod.load(tmp_path)
    assert re.search(r'not valid TOML at line 5, column \d+\Z', str(caught.value))
    assert shows_none(whole(caught.value))
    assert caught.value.__cause__ is None and caught.value.__context__ is None


def test_a_ship_file_cut_short_is_named_without_a_line(tmp_path):
    (tmp_path / 'ship.toml').write_text('[pools')
    with pytest.raises(ShipError, match='not valid TOML at the end of the file'):
        shipmod.load(tmp_path)


def test_the_services_own_read_of_the_ship_file_shows_no_part_of_it(tmp_path):
    (tmp_path / 'runtime').mkdir()
    (tmp_path / 'ship.toml').write_text(f'["{KEY}"]\na = 1\n["{KEY}"]\nb = 2\n')
    with pytest.raises(ValueError) as caught:
        forward_post.declared(tmp_path / 'runtime')
    assert shows_none(whole(caught.value))
    assert caught.value.__cause__ is None and caught.value.__context__ is None


CARRYING = [KEY, f'http://{KEY}@remote.invalid/v1', f'http://user:{KEY}@remote.invalid/v1',
            f'http://:{KEY}@remote.invalid/v1', f'https://remote.invalid/v1?key={KEY}',
            f'https://remote.invalid/v1#{KEY}', f'http://remote.invalid:{KEY}/v1', f'http://[{KEY}/v1']


@pytest.mark.parametrize('value', CARRYING)
@pytest.mark.parametrize('keyed', [True, False])
def test_an_endpoint_carrying_a_key_is_refused_and_shows_no_part_of_it(tmp_path, value, keyed):
    ship_file(tmp_path, value, key=keyed)
    with pytest.raises(ShipError, match='endpoint must be an http or https URL') as caught:
        shipmod.load(tmp_path)
    assert shows_none(whole(caught.value))


@pytest.mark.parametrize('value', CARRYING)
def test_setup_refuses_the_same_endpoints_and_shows_no_part_of_them(value):
    with pytest.raises(ShipError) as caught:
        setup.safe_endpoint(value)
    assert shows_none(whole(caught.value))
