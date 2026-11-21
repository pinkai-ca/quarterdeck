"""A jail has a network of its own: only its model endpoint, relayed in, is reachable.

Real bubblewrap, stand-ins only: a Python probe is the jailed program, a plain HTTP server on loopback outside is the
model endpoint (one path answers at once, one streams), and another loopback listener outside stands for anything
else on the machine. No model runs.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from quarterdeck import endpoint_relay, sandbox, ship as shipmod

from .test_sandbox import namespaces_allowed

pytestmark = pytest.mark.skipif(not namespaces_allowed(), reason="bubblewrap is not installed, or cannot make a namespace here (inside a jail)")


class Endpoint(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def do_GET(self):
        if self.path == '/v1/stream':  # a streamed answer: three pieces, a pause between each
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            for piece in (b'data: one\n\n', b'data: two\n\n', b'data: [DONE]\n\n'):
                self.wfile.write(b'%x\r\n%s\r\n' % (len(piece), piece))
                self.wfile.flush()
                time.sleep(0.6)
            self.wfile.write(b'0\r\n\r\n')
            return
        body = b'{"object": "list", "data": []}'
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def endpoint():
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Endpoint)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


@pytest.fixture
def elsewhere():
    """Another loopback listener outside: something else on the machine that no jail may reach."""
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(8)
    yield listener.getsockname()[1]
    listener.close()


def abstract_sockets() -> list[dict]:
    if shutil.which('ss') is None:
        return []
    kinds = {'u_str': 'stream', 'u_seq': 'seqpacket', 'u_dgr': 'datagram'}
    found = []
    for line in subprocess.run(['ss', '-xlH'], capture_output=True, text=True, timeout=30).stdout.splitlines():
        fields = line.split()
        if len(fields) >= 5 and fields[0] in kinds and fields[4].startswith('@'):
            found.append(dict(kind=kinds[fields[0]], address=fields[4]))
    return found


def jail_for(drydock: Path, tmp_path: Path, endpoint: str | None = None) -> sandbox.Jail:
    import pwd

    workspace = tmp_path / 'work'
    workspace.mkdir(exist_ok=True)
    ship = shipmod.load(drydock)
    shipmod.fence(ship)
    own = ship.runtime / 'seats' / 'worker-1'
    (own / 'home').mkdir(parents=True, exist_ok=True)
    path = os.pathsep.join([str(Path(sys.executable).parent), '/usr/bin', '/bin'])
    extra = {} if endpoint is None else dict(endpoint=endpoint)
    return sandbox.plan(ship, 'worker-1', own, own / 'home', workspace, sys.executable, path,
                        home=Path(pwd.getpwuid(os.getuid()).pw_dir), **extra)


PROBE = r"""
import json, socket, sys, time, urllib.request
abstract, elsewhere, port = json.loads(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
kinds = dict(stream=socket.SOCK_STREAM, seqpacket=socket.SOCK_SEQPACKET, datagram=socket.SOCK_DGRAM)
reached = []
for item in abstract:
    probe = socket.socket(socket.AF_UNIX, kinds[item['kind']])
    probe.settimeout(1)
    try:
        probe.connect('\0' + item['address'][1:])
        reached.append(item['address'])
    except OSError:
        pass
    finally:
        probe.close()
def tcp(port):
    try:
        socket.create_connection(('127.0.0.1', port), timeout=2).close()
        return True
    except OSError:
        return False
try:
    socket.getaddrinfo('example.com', 80)
    resolved = True
except OSError:
    resolved = False
seen = dict(abstract=reached, elsewhere=tcp(elsewhere), resolved=resolved)
if port != '-':
    try:
        seen['models'] = urllib.request.urlopen(f'http://127.0.0.1:{port}/v1/models', timeout=10).status
    except OSError as error:
        seen['models'] = repr(error)
    try:
        started, arrivals = time.monotonic(), []
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/v1/stream', timeout=10) as answer:
            for line in answer:
                if line.startswith(b'data:'):
                    arrivals.append(round(time.monotonic() - started, 2))
        seen['stream'] = arrivals
    except OSError as error:
        seen['stream'] = repr(error)
print(json.dumps(seen))
"""


def probe(argv: list[str], timeout=60) -> dict:
    env = {key: value for key, value in os.environ.items() if not key.startswith(('QD_', 'TMUX'))}
    result = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=timeout)
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_a_jail_reaches_no_abstract_socket_no_outside_listener_and_no_name(drydock: Path, tmp_path: Path, elsewhere):
    outside = abstract_sockets()
    jail = jail_for(drydock, tmp_path)
    seen = probe(sandbox.command(jail, [sys.executable, '-I', '-c', PROBE, json.dumps(outside), str(elsewhere), '-']))
    assert seen['abstract'] == [], f'abstract sockets reachable from inside: {seen["abstract"]}'
    assert seen['elsewhere'] is False, 'an outside loopback listener is reachable from inside'
    assert seen['resolved'] is False, 'a name resolved inside the jail'


def test_the_endpoint_answers_through_the_relay_and_streams_and_nothing_else_opens(drydock: Path, tmp_path: Path,
                                                                                      endpoint, elsewhere):
    jail = jail_for(drydock, tmp_path, endpoint=f'http://127.0.0.1:{endpoint}/v1')
    argv = sandbox.command(jail, [sys.executable, '-I', '-c', PROBE, json.dumps(abstract_sockets()), str(elsewhere),
                                  str(endpoint)])
    launcher = argv.index('--') + 1  # the launcher that starts the jail from an empty environment, then the relay
    assert argv[1:4] == ['-I', '-m', 'quarterdeck.jail_environment'] and sandbox.seats_hidden(argv) is True
    assert argv[launcher + 1:launcher + 4] == ['-I', '-m', 'quarterdeck.endpoint_relay']
    seen = probe(argv)
    assert seen['models'] == 200, seen['models']
    stream = seen['stream']
    assert isinstance(stream, list) and len(stream) == 3, stream
    assert stream[0] < stream[-1] - 0.8, f'the answer streamed piece by piece through the relay: {stream}'
    assert seen['abstract'] == [] and seen['elsewhere'] is False and seen['resolved'] is False
    assert not sandbox.relay_folder(jail).exists(), 'the relay removes its folder when the jail ends'


def test_without_its_relay_the_jail_reaches_nothing_and_nothing_falls_open(drydock: Path, tmp_path: Path, endpoint):
    """The relay's folder is there but nothing listens on its socket, as when the relay has died: the forwarder
    inside refuses, and no other way to the endpoint exists."""
    jail = jail_for(drydock, tmp_path, endpoint=f'http://127.0.0.1:{endpoint}/v1')
    argv = sandbox.command(jail, [sys.executable, '-I', '-c', PROBE, '[]', '1', str(endpoint)])
    relay = argv.index('quarterdeck.endpoint_relay')
    jailed = argv[argv.index('--', relay) + 1:]  # the jail itself, started without the relay that would serve it
    folder = sandbox.relay_folder(jail)
    endpoint_relay._prepare(folder)
    endpoint_relay._hosts(folder, None)
    try:
        seen = probe(jailed)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    assert seen['models'] != 200 and seen['stream'] != [] and not isinstance(seen['stream'], list), seen


def test_a_relay_that_dies_takes_the_jail_with_it(drydock: Path, tmp_path: Path, endpoint):
    jail = jail_for(drydock, tmp_path, endpoint=f'http://127.0.0.1:{endpoint}/v1')
    marker = tmp_path / 'work' / 'alive'
    sleeper = f"import pathlib, time; pathlib.Path({str(marker)!r}).write_text('up'); time.sleep(120)"
    argv = sandbox.command(jail, [sys.executable, '-I', '-c', sleeper])
    env = {key: value for key, value in os.environ.items() if not key.startswith(('QD_', 'TMUX'))}
    relay = subprocess.Popen(argv, env=env, start_new_session=True)
    try:
        deadline = time.monotonic() + 30
        while not marker.exists():
            assert time.monotonic() < deadline and relay.poll() is None, 'the jail started'
            time.sleep(0.1)
        children = subprocess.run(['pgrep', '-s', str(relay.pid)], capture_output=True, text=True).stdout.split()
        assert len(children) >= 2, 'the relay and its jail run'
        relay.send_signal(signal.SIGKILL)
        relay.wait(timeout=10)
        deadline = time.monotonic() + 10
        while subprocess.run(['pgrep', '-s', str(relay.pid)], capture_output=True).returncode == 0:
            assert time.monotonic() < deadline, 'the jail outlived its relay'
            time.sleep(0.1)
    finally:
        if relay.poll() is None:
            os.killpg(relay.pid, signal.SIGKILL)
        subprocess.run(['pkill', '-KILL', '-s', str(relay.pid)], capture_output=True)
        shutil.rmtree(sandbox.relay_folder(jail), ignore_errors=True)


HTTPS_PROBE = r"""
import json, os, socket
from urllib.parse import urlsplit
proxy = urlsplit(os.environ['HTTPS_PROXY'])
def through(target):
    client = socket.create_connection((proxy.hostname, proxy.port), timeout=10)
    client.sendall(f'CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n'.encode())
    answer = b''
    while b'\r\n\r\n' not in answer:
        part = client.recv(4096)
        if not part:
            return dict(status=None)
        answer += part
    status = int(answer.split()[1])
    echoed = None
    if status == 200:
        client.sendall(b'client hello, passed through unchanged')
        echoed = client.recv(4096).decode()
    client.close()
    return dict(status=status, echoed=echoed)
print(json.dumps(dict(own=through('api.example.test:443'), other_name=through('elsewhere.example.test:443'),
                      other_port=through('api.example.test:444'), https_proxy=os.environ['HTTPS_PROXY'] == os.environ['https_proxy'])))
"""


def test_a_jailed_seat_reaches_a_remote_https_endpoint_on_443_and_nothing_else(drydock: Path, tmp_path: Path):
    """A jail cannot bind 443, so its forwarder is the program's proxy, for the endpoint's own name and port alone."""
    outside = socket.socket()
    outside.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    outside.bind(('127.0.0.1', 0))
    outside.listen(8)

    def echo():
        while True:
            try:
                connection, _ = outside.accept()
            except OSError:
                return
            with connection:
                if data := connection.recv(4096):
                    connection.sendall(data)
    threading.Thread(target=echo, daemon=True).start()
    jail = jail_for(drydock, tmp_path, endpoint='https://api.example.test/v1')
    argv = sandbox.command(jail, [sys.executable, '-I', '-c', HTTPS_PROBE])
    relay = argv.index('quarterdeck.endpoint_relay')
    assert argv[relay + 2:relay + 5] == ['api.example.test', '443', 'api.example.test']
    assert '443:api.example.test' in argv
    argv[relay + 2:relay + 4] = ['127.0.0.1', str(outside.getsockname()[1])]  # the stand-in for the remote host
    try:
        seen = probe(argv)
    finally:
        outside.close()
    assert seen['own'] == {'status': 200, 'echoed': 'client hello, passed through unchanged'}
    assert seen['other_name']['status'] == 403 and seen['other_port']['status'] == 403 and seen['https_proxy'] is True


@pytest.mark.parametrize('address,forwarded', [('https://api.example.test/v1', '443:api.example.test'),
                                               ('https://api.example.test:8443/v1', '8443'),
                                               ('http://127.0.0.1:8000/v1', '8000')])
def test_the_forwarder_is_a_proxy_only_where_a_jail_cannot_bind_the_port(drydock: Path, tmp_path: Path, address, forwarded):
    argv = sandbox.command(jail_for(drydock, tmp_path, endpoint=address), ['program'])
    from quarterdeck import endpoint_relay
    assert argv[argv.index(endpoint_relay.INSIDE) + 2] == forwarded
