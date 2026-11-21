"""Offline wire proof for the installed OpenCode 1.19.5 and pi 0.79.0.

Run with PYTHONPATH=src python3 tests/measure_openai_endpoints.py from the worktree.
Uses each adapter's generated home and launch options, pointing the generated
base URL at a loopback stand-in. No client settings outside the proof are used.
"""

import json
import os
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch

from quarterdeck import endpoint
from quarterdeck.harness.opencode import OpenCodeHarness
from quarterdeck.harness.pi import PiHarness
from quarterdeck.ship import Pool, SeatSpec

MODEL = 'gpt-6-luna'
KEY = 'synthetic-offline-endpoint-key'
MARKER = 'SYNTHETIC_READ_RESULT'
TEXT = 'OFFLINE_READY'
CAP = 1024
VERSIONS = {'opencode': '1.19.5', 'pi': '0.79.0'}


def response_events(turn, arguments):
    response = dict(id=f'resp_{turn}', object='response', status='in_progress', model=MODEL, output=[])
    yield 'response.created', dict(response=response)
    if turn == 1:
        item = dict(type='function_call', id='fc_read', call_id='call_read', name='read')
        yield 'response.output_item.added', dict(output_index=0, item=dict(item, arguments='', status='in_progress'))
        yield 'response.function_call_arguments.delta', dict(output_index=0, item_id=item['id'], delta=arguments)
        yield 'response.function_call_arguments.done', dict(output_index=0, item_id=item['id'],
                                                             name='read', arguments=arguments)
        item.update(arguments=arguments, status='completed')
    else:
        item = dict(type='message', id='msg_ready', role='assistant')
        part = dict(type='output_text', text=TEXT, annotations=[], logprobs=[])
        yield 'response.output_item.added', dict(output_index=0, item=dict(item, content=[], status='in_progress'))
        yield 'response.content_part.added', dict(output_index=0, content_index=0, item_id=item['id'],
                                                  part=dict(part, text=''))
        yield 'response.output_text.delta', dict(output_index=0, content_index=0, item_id=item['id'],
                                                 delta=TEXT, logprobs=[])
        yield 'response.output_text.done', dict(output_index=0, content_index=0, item_id=item['id'],
                                                text=TEXT, logprobs=[])
        yield 'response.content_part.done', dict(output_index=0, content_index=0, item_id=item['id'], part=part)
        item.update(content=[part], status='completed')
    yield 'response.output_item.done', dict(output_index=0, item=item)
    usage = dict(input_tokens=30, output_tokens=8, total_tokens=38,
                 input_tokens_details={'cached_tokens': 0}, output_tokens_details={'reasoning_tokens': 0})
    yield 'response.completed', dict(response=dict(response, status='completed', output=[item], usage=usage))


class StandIn(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, status, content_type, body):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.send(404, 'application/json', b'{}')

    do_CONNECT = do_GET

    def do_POST(self):
        path = self.path
        if path not in ('/v1/chat/completions', '/v1/responses'):
            self.do_GET()
            return
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.calls.append((path, body, self.headers.get('Authorization') == f'Bearer {KEY}'))
        if path == '/v1/chat/completions':
            if 'max_tokens' in body:
                complaint = "Unsupported parameter: 'max_tokens'; use 'max_completion_tokens'"
            elif body.get('tools') and body.get('reasoning_effort') not in ('none',):
                complaint = 'Function tools with reasoning_effort require /v1/responses'
            else:
                complaint = 'Unexpected Chat Completions request'
            if body.get('tools'):
                self.server.rejections.append(complaint)
            error = {'error': {'type': 'invalid_request_error', 'message': complaint}}
            self.send(400, 'application/json', json.dumps(error).encode())
            return
        tools = body.get('tools', [])
        if tools:
            fields = next(tool['parameters']['properties'] for tool in tools if tool.get('name') == 'read')
            path_field = 'filePath' if 'filePath' in fields else 'path'
            args = json.dumps({path_field: str(self.server.fixture)})
            turn = sum(bool(request.get('tools')) for _, request, _ in self.server.calls)
            events = response_events(turn, args)
        else:
            # OpenCode also generates a title in parallel, without tools.
            events = response_events('title', None)
        stream = ''.join(f'event: {kind}\ndata: {json.dumps(dict(data, type=kind, sequence_number=index))}\n\n'
                         for index, (kind, data) in enumerate(events))
        self.send(200, 'text/event-stream', stream.encode())


def generated_launch(root, harness, *, old, output, pool_name, effort):
    home = root / 'runtime/seats/demo/home'
    brief = root / 'runtime/brief/demo.md'
    brief.parent.mkdir(parents=True)
    brief.write_text('Offline proof. Read the file only when asked through the supplied function.')
    pool = Pool(pool_name, 'openai', endpoint=endpoint.OPENAI_RESPONSES_BASE, api_key='keys:unused',
                output=output)
    seat = SeatSpec('demo', 'Demo', 'worker', harness.name, (pool.name,), model=MODEL, effort=effort)
    with patch.object(endpoint, 'models', return_value=[endpoint.Served('decoy'), endpoint.Served(MODEL)]):
        with patch.object(endpoint, '_secret', return_value=KEY):
            harness.build_home(home, seat, pool, None)
    launch = harness.launch(seat, pool, home, brief, None, None)
    if harness.name == 'opencode':
        config = home / 'config/opencode/opencode.json'
        data = json.loads(config.read_text())
        provider = data['provider'][pool.name]
        options, address = provider['options'], 'baseURL'
        transport, native, former = 'npm', '@ai-sdk/openai', '@ai-sdk/openai-compatible'
    else:
        config = home / 'agent/models.json'
        data = json.loads(config.read_text())
        provider = data['providers'][pool.name]
        options, address = provider, 'baseUrl'
        transport, native, former = 'api', 'openai-responses', 'openai-completions'
    assert options[address] == endpoint.OPENAI_RESPONSES_BASE
    assert provider[transport] == native
    if old:
        provider[transport] = former
        if harness.name == 'opencode':
            data.pop('small_model')
    return config, data, options, address, launch


def proof(root, harness, binary, *, old, output, pool_name, effort, cap_hook=True):
    fixture = root / 'fixture.txt'
    with ThreadingHTTPServer(('127.0.0.1', 0), StandIn) as server:
        config, data, options, address, launch = generated_launch(root, harness, old=old, output=output,
                                                                 pool_name=pool_name, effort=effort)
        proxy = f'http://127.0.0.1:{server.server_port}'
        options[address] = f'{proxy}/v1'
        config.write_text(json.dumps(data))
        fixture.write_text(MARKER)
        server.fixture, server.calls, server.rejections = fixture, [], []
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            env = {
                'PATH': os.environ['PATH'], 'HOME': str(root / 'home'), 'XDG_CONFIG_HOME': str(root / 'home/config'),
                'XDG_CACHE_HOME': str(root / 'home/cache'), 'XDG_DATA_HOME': str(root / 'home/data'),
                'XDG_STATE_HOME': str(root / 'home/state'), 'NO_PROXY': '127.0.0.1',
                'HTTP_PROXY': proxy, 'HTTPS_PROXY': proxy, 'ALL_PROXY': proxy, **launch.env,
                endpoint.KEY_VARIABLE: KEY, 'OPENCODE_DISABLE_MODELS_FETCH': '1',
                'OPENCODE_DISABLE_DEFAULT_PLUGINS': '1', 'OPENCODE_DISABLE_LSP': '1',
            }
            # OpenCode's run subcommand is noninteractive. The rest is the adapter's
            # launch, including its model selector and generated configuration.
            if harness.name == 'opencode':
                argv = [binary, 'run', *launch.argv[2:]]
            else:
                argv = [binary, '-p', *launch.argv[1:]]
                assert env['QD_PI_RESPONSES_BASE_URL'] == endpoint.OPENAI_RESPONSES_BASE
                env['QD_PI_RESPONSES_BASE_URL'] = options[address]
                if old or not cap_hook:
                    at = argv.index('--extension')
                    del argv[at:at + 2]
            prompt = 'Use the read tool on fixture.txt and reply with its contents.'
            done = subprocess.run([*argv, prompt], cwd=root, env=env, capture_output=True, text=True, timeout=35)
        finally:
            server.shutdown()
            thread.join()
        calls, rejections = server.calls, server.rejections

    tool_calls = [call for call in calls if call[1].get('tools')]
    assert len(tool_calls) == (1 if old else 2), (argv, len(tool_calls), done.stderr, done.stdout)
    path, body, _ = tool_calls[0]
    cap = output or endpoint.DEFAULT_OUTPUT
    for route, call, keyed in tool_calls if old else calls:
        assert keyed and call['model'] == MODEL and call['stream'] is True, (route, call.get('model'), keyed)
    if old and harness.name == 'opencode':
        assert path == '/v1/chat/completions' and body['max_tokens'] == cap
        assert set(rejections) == {"Unsupported parameter: 'max_tokens'; use 'max_completion_tokens'"}
    elif old:
        assert path == '/v1/chat/completions' and {'max_tokens', 'max_completion_tokens'}.isdisjoint(body)
        assert body.get('reasoning_effort') == (None if effort == 'off' else effort)
        assert set(rejections) == {'Function tools with reasoning_effort require /v1/responses'}
    else:
        assert done.returncode == 0 and TEXT in done.stdout, (done.stdout, done.stderr)
        assert not rejections
        assert all(path == '/v1/responses' and call.get('store') is False for path, call, _ in calls)
        if harness.name == 'pi' and effort:
            assert tool_calls[0][1]['reasoning']['effort'] == effort
        for path, body, _ in tool_calls:
            assert path == '/v1/responses' and body.get('max_output_tokens') == (cap if cap_hook else None), body.keys()
            assert {'max_tokens', 'max_completion_tokens', 'reasoning_effort'}.isdisjoint(body)
            assert any(tool.get('type') == 'function' and tool.get('name') == 'read' for tool in body['tools'])
        result, = [item for item in tool_calls[1][1]['input'] if item.get('type') == 'function_call_output']
        assert result['call_id'] == 'call_read' and MARKER in result['output'], result
    wire_cap = body.get('max_tokens', body.get('max_output_tokens'))
    print(f'{harness.name}: {"rejected Chat Completions" if old else "Responses read and result"}, '
          f'pool={pool_name}, effort={effort}, wire cap={wire_cap}, requests={len(calls)}')


def version(binary, root):
    env = {'PATH': os.environ['PATH'], 'HOME': str(root), 'XDG_CACHE_HOME': str(root / 'cache'),
           'OPENCODE_DISABLE_AUTOUPDATE': '1', 'PI_SKIP_VERSION_CHECK': '1'}
    return subprocess.check_output([binary, '--version'], env=env, cwd=root, stderr=subprocess.STDOUT,
                                   text=True, timeout=15).strip()


def main():
    with TemporaryDirectory(prefix='.openai-endpoint-proof-', dir=Path(__file__).resolve().parents[1]) as folder:
        root = Path(folder)
        for harness in (OpenCodeHarness(), PiHarness()):
            binary = shutil.which(harness.program)
            assert binary, harness.program
            before = Path(binary).resolve(), version(binary, root)
            assert before[1] == VERSIONS[harness.name], before
            try:
                effort = 'medium' if harness.name == 'pi' else None
                proof(root / f'{harness.name}-former', harness, binary, old=True, output=CAP,
                      pool_name='openai', effort=effort)
                proof(root / f'{harness.name}-explicit', harness, binary, old=False, output=CAP,
                      pool_name='openai', effort=effort)
                proof(root / f'{harness.name}-default', harness, binary, old=False, output=None,
                      pool_name='offline-proof', effort=None)
                if harness.name == 'pi':
                    proof(root / 'pi-former-off', harness, binary, old=True, output=None,
                          pool_name='openai', effort='off')
                    proof(root / 'pi-native-without-hook', harness, binary, old=False, output=CAP,
                          pool_name='openai', effort='medium', cap_hook=False)
            finally:
                assert (Path(binary).resolve(), version(binary, root)) == before


if __name__ == '__main__':
    main()
