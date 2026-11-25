"""Offline wire proof. From the worktree, run:

bwrap --unshare-user --unshare-net --ro-bind / / --proc /proc --dev /dev --tmpfs /tmp \
  --setenv QD_GOOGLE_PROOF_PARENT_NET "$(readlink /proc/self/ns/net)" \
  env PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python3 tests/measure_google_endpoints.py

Use the generated launches and replace only the generated base URL's origin
with a loopback stand-in. The installed clients and SDKs build the requests.
"""

import json
import os
import shutil
import socket
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch
from urllib.parse import urlsplit

from quarterdeck import endpoint
from quarterdeck.harness.opencode import OpenCodeHarness
from quarterdeck.harness.pi import PiHarness
from quarterdeck.ship import Pool, SeatSpec

MODEL = 'gemini-3.5-fixture'
KEY = 'synthetic-google-key'
MARKER = 'SYNTHETIC_READ_RESULT'
READY = 'NATIVE_TOOL_TURN_READY'
SIGNATURE = 'c3ludGhldGljLXNpZ25hdHVyZQ=='
MISSING = 'Synthetic fixture: missing or different thought signature'
VERSIONS = {'pi': '0.79.0', 'opencode': '1.19.5'}


class StandIn(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        try:
            assert self.path == f'/v1beta/models/{MODEL}:streamGenerateContent?alt=sse'
            assert self.headers['x-goog-api-key'] == KEY
            assert 'Authorization' not in self.headers
            part = self.reply_to(body)
        except (AssertionError, KeyError, ValueError) as error:
            self.server.failure = (error, body.get('contents', [])[-2:])
            self.send_error(400, 'Unexpected synthetic request')
            return
        if part is None:
            self.send_error(400, MISSING)
            return
        chunk = {
            'candidates': [{'content': {'role': 'model', 'parts': [part]}, 'finishReason': 'STOP'}],
            'usageMetadata': {'promptTokenCount': 12, 'candidatesTokenCount': 3, 'totalTokenCount': 15},
        }
        stream = f'data: {json.dumps(chunk)}\n\n'.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Length', str(len(stream)))
        self.end_headers()
        self.wfile.write(stream)

    def reply_to(self, body):
        tools = [fn['name'] for tool in body.get('tools', []) for fn in tool.get('functionDeclarations', [])]
        if 'read' not in tools:
            # OpenCode can also make a title request without tools.
            assert not tools
            return {'text': 'Synthetic title'}
        self.server.turn_requests.append(body)
        function = self.server.function
        if len(self.server.turn_requests) == 1:
            part = {'functionCall': function}
            if self.server.signed:
                part['thoughtSignature'] = SIGNATURE
            return part
        assert len(self.server.turn_requests) == 2
        assistant, tool = body['contents'][-2:]
        assert assistant['role'] == 'model'
        call, = [part for part in assistant['parts'] if 'functionCall' in part]
        assert call['functionCall']['name'] == 'read' and call['functionCall']['args'] == function['args']
        assert tool['role'] == 'user'
        response = tool['parts'][0]['functionResponse']
        assert response['name'] == 'read' and MARKER in json.dumps(response['response'])
        self.server.rejected = call.get('thoughtSignature') != SIGNATURE
        return None if self.server.rejected else {'text': READY}


def prove(harness, binary, root, pool_name, model, *, signed):
    root.mkdir()
    home = root / 'runtime/seats/fixture/home'
    (root / 'fixture.txt').write_text(MARKER)
    brief = root / 'runtime/brief/fixture.md'
    brief.parent.mkdir(parents=True)
    brief.write_text('Read the synthetic fixture when the provider calls the read function.')
    pool = Pool(pool_name, 'google', endpoint=endpoint.GOOGLE_OPENAI_BASE, api_key='keys:unused')
    effort = 'off' if harness.name == 'pi' else None
    seat = SeatSpec('fixture', 'Fixture', 'worker', harness.name, (pool.name,), model=model, effort=effort)
    with patch.object(endpoint, 'models', return_value=[endpoint.Served(f'models/{MODEL}')]):
        with patch.object(endpoint, '_secret', return_value=KEY):
            harness.build_home(home, seat, pool, None)
    launch = harness.launch(seat, pool, home, brief, None, None)

    if harness.name == 'pi':
        path = home / 'agent/models.json'
        config = json.loads(path.read_text())
        provider = config['providers'][pool.name]
        options, address = provider, 'baseUrl'
        assert (provider['api'], options['apiKey']) == ('google-generative-ai', '$QD_ENDPOINT_KEY')
        argv = [binary, '-p', *launch.argv[1:]]
    else:
        path = home / 'config/opencode/opencode.json'
        config = json.loads(path.read_text())
        provider = config['provider'][pool.name]
        options, address = provider['options'], 'baseURL'
        assert (provider['npm'], options['apiKey']) == ('@ai-sdk/google', '{env:QD_ENDPOINT_KEY}')
        argv = [binary, 'run', *launch.argv[2:]]
    assert options[address] == endpoint.GOOGLE_GENERATIVE_BASE

    with ThreadingHTTPServer(('127.0.0.1', 0), StandIn) as server:
        server.turn_requests, server.failure, server.rejected = [], None, None
        server.signed = signed
        args = {'path': 'fixture.txt'} if harness.name == 'pi' else {'filePath': str(root / 'fixture.txt')}
        server.function = {'name': 'read', 'args': args}
        origin = f'http://127.0.0.1:{server.server_port}'
        options[address] = origin + urlsplit(options[address]).path
        path.write_text(json.dumps(config))
        env = {
            **launch.env, 'PATH': os.environ['PATH'], 'HOME': str(home), 'PI_OFFLINE': '1',
            'HTTP_PROXY': origin, 'HTTPS_PROXY': origin, 'NO_PROXY': '127.0.0.1',
            'OPENCODE_DISABLE_MODELS_FETCH': '1', 'OPENCODE_DISABLE_DEFAULT_PLUGINS': '1',
            'OPENCODE_DISABLE_LSP': '1', 'OPENCODE_PURE': '1', 'npm_config_offline': 'true',
            endpoint.KEY_VARIABLE: KEY,
        }
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            version = subprocess.run([binary, '--version'], cwd=root, env=env, capture_output=True,
                                     text=True, timeout=10, check=True)
            assert (version.stdout + version.stderr).strip() == VERSIONS[harness.name], version
            result = subprocess.run([*argv, 'Read fixture.txt.'], cwd=root, env=env, capture_output=True,
                                    text=True, timeout=30)
            context = (result.stdout, result.stderr, server.failure, len(server.turn_requests))
            assert len(server.turn_requests) == 2 and server.failure is None, context
            assert server.rejected is (not signed), context
            if signed:
                assert result.returncode == 0 and READY in result.stdout, context
            else:
                assert MISSING in result.stderr + result.stdout and READY not in result.stdout, context
        finally:
            server.shutdown()
            thread.join()
    outcome = 'accepted exact replay' if signed else 'rejected missing or different signature'
    print(f'{harness.name} {VERSIONS[harness.name]} {pool_name}/{model}: {outcome}')


if __name__ == '__main__':
    namespace = os.readlink('/proc/self/ns/net')
    parent = os.environ.get('QD_GOOGLE_PROOF_PARENT_NET')
    if not parent or namespace == parent or [name for _, name in socket.if_nameindex()] != ['lo']:
        raise SystemExit('Run this proof in a separate network namespace with only loopback')
    print(f'Proof {namespace}, parent {parent}. Only loopback is present.', flush=True)
    with TemporaryDirectory(prefix='google-endpoint-') as directory:
        root = Path(directory)
        for harness in (PiHarness(), OpenCodeHarness()):
            binary = shutil.which(harness.program)
            if binary is None:
                raise SystemExit(f'{harness.program} {VERSIONS[harness.name]} must be on PATH')
            prove(harness, binary, root / f'{harness.name}-bare', 'google', MODEL, signed=True)
            prove(harness, binary, root / f'{harness.name}-resource', 'custom', f'models/{MODEL}', signed=True)
            prove(harness, binary, root / f'{harness.name}-unsigned', 'custom', MODEL, signed=False)
