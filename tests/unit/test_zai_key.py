"""Z.ai key routes with scratch declarations and synthetic HTTP responses."""

from contextlib import closing
from datetime import timedelta
from hashlib import sha256
import io
import json
import os
import urllib.error
import urllib.request
from dataclasses import replace
from types import SimpleNamespace

import pytest

from quarterdeck import doctor, endpoint, key_launch, key_proof, keyfile, quota, service
from quarterdeck.harness import get, resume_target
from quarterdeck.harness.key_routes import ZAI_CHAT, ZAI_MESSAGES, prepare_zai
from quarterdeck.harness.synthetic_record import ClaudeShape
from quarterdeck.ledger import Ledger
from quarterdeck.post import now
from quarterdeck.seat import Launched, Seat, process_start
from quarterdeck.ship import Pool, ShipError, load
from quarterdeck.tmux import Pane


from .test_opencode_record import AT, MS, database, message, part, session


KEY = 'synthetic-standard-key-for-fixture'
MODEL = 'synthetic-glm'


def declare(folder, *, program='claude', base=ZAI_MESSAGES, model=MODEL):
    declaration = folder / 'ship.toml'
    declaration.write_text(f'''[ship]
name = "scratch"
code = "checkout"
workspace = "."
[pools.standard]
provider = "Z.ai"
endpoint = "{base}"
api_key = "keys:glm"
[pools.subscription]
provider = "anthropic"
[sessions.crew]
title = "Scratch"
[seats.worker]
session = "crew"
role = "worker"
harness = "{program}"
pool = "standard"
sandbox = false
'''+ (f'model = "{model}"\n' if model else ''))
    return declaration


def test_claude_launch_uses_the_pin_model_and_existing_key_launcher(tmp_path, key_file, monkeypatch):
    declaration = declare(tmp_path)
    pinned = tmp_path / 'runtime/programs/claude-proof'
    pinned.parent.mkdir(parents=True, mode=0o700)
    pinned.write_text('#!/bin/sh\nprintf "1.2.3\\n"\n')
    pinned.chmod(0o500)
    digest = sha256(pinned.read_bytes()).hexdigest()
    with declaration.open('a') as file:
        file.write(f'\n[programs.claude]\npath = "{pinned.relative_to(tmp_path)}"\nsha256 = "{digest}"\n')
    ship = load(tmp_path)
    key_file.write(glm=KEY)
    panes = {}
    tmux = SimpleNamespace(pane=panes.get)

    def opened(self, argv, env, path):
        inner = key_launch.inner(argv)
        assert inner[0] == str(pinned)
        assert inner[inner.index('--model') + 1] == MODEL
        assert argv[argv.index('--name') + 1] == 'glm'
        assert argv[argv.index('--variable') + 1] == keyfile.HELPER_VARIABLE
        assert KEY not in repr((argv, env))
        assert keyfile.HELPER_VARIABLE not in env
        assert env['CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL'] == '1'
        assert env['CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC'] == '1'
        text = (self.home / 'settings.json').read_text()
        assert KEY not in text
        settings = json.loads(text)
        assert settings['env']['ANTHROPIC_BASE_URL'] == ZAI_MESSAGES
        assert settings['apiKeyHelper'] == keyfile.HELPER_COMMAND
        pane = Pane('%1', 'Scratch', 'worker', os.getpid(), 100, 25, False, False, None)
        panes[pane.id] = pane
        source = ClaudeShape(self.home, tmp_path, 'synthetic-messages', MODEL)
        source.begin('Synthetic prompt', MODEL, 'none')
        source.end('Synthetic reply')
        return pane

    monkeypatch.setattr(Seat, '_open_window', opened)
    monkeypatch.setattr(Seat, '_survives', lambda *args: None)
    seat = Seat(ship, ship.seats['worker'], tmux)
    assert 'provider Z.ai' in seat.up(resume=False)
    assert seat.launched().version == '1.2.3'
    assert seat.launched().pool == 'standard'
    assert seat._require_resume(pool='standard').id == 'synthetic-messages'
    with pytest.raises(resume_target.Unproved, match='provider route'):
        seat._require_resume(pool='subscription')


@pytest.mark.parametrize('program,base,model,reason', [
    ('claude', ZAI_MESSAGES, None, 'explicit model ID'),
    ('opencode', ZAI_CHAT, None, 'explicit model ID'),
    ('pi', ZAI_CHAT, None, 'explicit model ID'),
    ('pi', ZAI_CHAT, '-invalid', 'explicit model ID'),
    ('claude', ZAI_CHAT, MODEL, 'no supported Z.ai key route'),
    ('codex', ZAI_CHAT, MODEL, 'no supported Z.ai key route'),
    ('opencode', ZAI_MESSAGES, MODEL, 'no supported Z.ai key route'),
    ('claude', ZAI_MESSAGES + '.invalid', MODEL, 'Z.ai keys require'),
])
def test_wrong_route_or_implicit_model_fails_at_load(tmp_path, program, base, model, reason):
    declare(tmp_path, program=program, base=base, model=model)
    with pytest.raises(ShipError, match=reason):
        load(tmp_path)


def test_messages_doctor_uses_the_standard_listing_probe(tmp_path, key_file, monkeypatch):
    declare(tmp_path)
    pool = load(tmp_path).pools['standard']
    key_file.write(glm=KEY)
    seen = []

    class Answer(io.BytesIO):
        status = 200

    def open_request(request, *, timeout):
        seen.append(request.full_url)
        assert request.get_header('Authorization') == f'Bearer {KEY}'
        return Answer(b'{"data":[]}')  # the doctor reads the status alone

    monkeypatch.setattr(key_proof, 'keyed_opener', lambda url: SimpleNamespace(open=open_request))
    check = doctor.pool_check(pool)
    assert check.passed
    assert 'account balance unknown' in check.detail
    assert 'model listing answered; inference unverified' in check.detail
    assert KEY not in check.detail
    assert seen == [f'{ZAI_CHAT}/models']


@pytest.mark.parametrize('program,human', [('opencode', False), ('opencode', True), ('pi', False)])
def test_chat_builds_the_existing_keyed_endpoint_home(tmp_path, key_file, monkeypatch, program, human):
    path = declare(tmp_path, program=program, base=ZAI_CHAT)
    if human:
        path.write_text(path.read_text().replace('title = "Scratch"', 'title = "Scratch"\nhuman = true'))
    ship = load(tmp_path)
    key_file.write(glm=KEY)
    home = tmp_path / 'runtime/seats/worker/home'
    spec = Seat(ship, ship.seats['worker'], None).adapter_spec()
    pool = ship.pools['standard']
    adapter = get(program)

    def open_request(request, *, timeout):
        assert request.full_url == f'{ZAI_CHAT}/models'
        assert request.get_header('Authorization') == f'Bearer {KEY}'
        return io.BytesIO(json.dumps({'object': 'list', 'data': [
            {'id': 'another-model', 'object': 'model', 'created': 3, 'owned_by': 'synthetic'},
            {'id': MODEL, 'object': 'model', 'created': 2, 'owned_by': 'synthetic'},
            {'id': '-invalid-later-model', 'created': 10},
            {'id': KEY, 'created': 11},
        ]}).encode())

    monkeypatch.setattr(key_proof, 'keyed_opener', lambda url: SimpleNamespace(open=open_request))
    adapter.build_home(home, spec, pool, None)
    prepare_zai(program, pool, home)
    launch = adapter.launch(spec, pool, home, tmp_path / 'brief', None, None)
    choice = endpoint.Choice.read(home)
    assert choice.model == MODEL
    assert 'newest by listed creation time: another-model' in choice.note
    assert launch.env[keyfile.ENDPOINT_VARIABLE] == 'keys:glm'
    if program == 'opencode':
        assert launch.env['OPENCODE_DISABLE_PROJECT_CONFIG'] == '1'
        assert launch.env['OPENCODE_DISABLE_MODELS_FETCH'] == '1'
        assert launch.env['OPENCODE_PURE'] == '1'
        assert launch.env['npm_config_offline'] == 'true'
        assert ('OPENCODE_PERMISSION' in launch.env) is not human
    assert not any(KEY in str(item) for item in (*launch.argv, *launch.env.values()))
    for path in home.rglob('*.json'):
        assert KEY not in path.read_text()


@pytest.mark.parametrize('first,second', [(None, 2), (2, 2)])
def test_unproved_catalogue_order_stays_unknown(first, second):
    assert endpoint.newest_by_created([endpoint.Served('a', created=first),
                                       endpoint.Served('b', created=second)]) is None


@pytest.mark.parametrize('status,code,reason', [
    (429, '1113', 'insufficient balance'),
    (429, 1113, 'insufficient balance'),
    (429, '1302', 'answered 429'),
    (429, 1113.0, 'answered 429'),
    (401, '1113', 'key refused'),
    (403, 1000, 'key refused'),
])
def test_chat_probe_classifies_only_matching_status_and_code(tmp_path, key_file, monkeypatch, status, code, reason):
    key_file.write(glm=KEY)
    pool = Pool('standard', 'zai', endpoint=ZAI_CHAT, api_key='keys:glm', ship_folder=tmp_path)
    seen = []

    def open_request(request, *, timeout):
        seen.append(request.full_url)
        assert request.get_header('Authorization') == f'Bearer {KEY}'
        body = json.dumps({'error': {'code': code, 'message': KEY}}).encode()
        raise urllib.error.HTTPError(request.full_url, status, 'synthetic', {}, io.BytesIO(body))

    monkeypatch.setattr(key_proof, 'keyed_opener', lambda url: SimpleNamespace(open=open_request))
    passed, line = key_proof.prove(pool)
    assert not passed and reason in line and KEY not in line
    with pytest.raises(ShipError, match='could not discover models') as failure:
        endpoint.choose(pool, MODEL)
    assert ('insufficient balance' in str(failure.value)) == (reason == 'insufficient balance')
    assert KEY not in str(failure.value)
    assert seen == [f'{ZAI_CHAT}/models'] * 2


def test_claude_routing_overrides_are_refused_before_the_key_reaches_the_program(tmp_path):
    pool = Pool('standard', 'zai', endpoint=ZAI_MESSAGES, api_key='keys:glm')
    path = tmp_path / 'settings.json'
    path.write_text('{"env":{"ANTHROPIC_BASE_URL":"https://other.invalid"}}')
    with pytest.raises(keyfile.KeyProblem, match='remove authentication and routing overrides'):
        prepare_zai('claude', pool, tmp_path)


@pytest.mark.parametrize('reverse', [False, True])
def test_key_reference_can_only_name_one_canonical_provider(tmp_path, reverse):
    path = declare(tmp_path)
    lines = path.read_text()
    start, end = lines.index('[pools.standard]'), lines.index('[pools.subscription]')
    other = '[pools.other]\nprovider = "ZAI"\n'
    other += f'endpoint = "{ZAI_CHAT}"\napi_key = "keys:glm"\n'
    def write(pool):
        path.write_text(lines[:start] + (pool + lines[start:end] if reverse else lines[start:end] + pool)
                        + lines[end:])
    write(other)
    assert load(tmp_path).pools['other'].provider == 'zai'
    write(other.replace('"ZAI"', '"google"').replace(ZAI_CHAT, 'https://example.invalid/v1'))
    with pytest.raises(ShipError, match='an API key may be declared for only one provider'):
        load(tmp_path)


def test_zai_listing_bounds_and_filters_before_using_model_names(tmp_path, key_file, monkeypatch):
    key_file.write(glm=KEY)
    pool = Pool('standard', 'zai', endpoint=ZAI_CHAT, api_key='keys:glm', ship_folder=tmp_path)
    admitted = ['routes/model:one_2.3-test', 'a' * 128]
    ignored = ['', '-dash', 'has space', 'ctl\nrow', 'ñative', 'x' * 129, KEY]
    payload = json.dumps({'data': [
        {'id': name, 'created': ix + 1} for ix, name in enumerate(admitted + ignored)
    ]}).encode()
    reads = []

    class FixtureResponse(io.BytesIO):
        def read(self, size):
            reads.append(size)
            return super().read(size)

    def open_request(*args, **kwargs):
        return FixtureResponse(payload)

    monkeypatch.setattr(key_proof, 'keyed_opener', lambda url: SimpleNamespace(open=open_request))
    assert [model.id for model in endpoint.models(ZAI_CHAT, api_key=KEY)] == admitted
    assert f'newest by listed creation time: {admitted[-1]}' in endpoint.choose(pool, admitted[0]).note
    with pytest.raises(ShipError, match='Z.ai does not serve the declared model') as failure:
        endpoint.choose(pool, 'missing')
    assert all(name not in str(failure.value) for name in admitted)
    assert reads == [endpoint.ZAI_LISTING_BYTES + 1] * 3
    payload = b'x' * (endpoint.ZAI_LISTING_BYTES + 1)
    with pytest.raises(ShipError, match='exceeds the byte limit'):
        endpoint.models(ZAI_CHAT, api_key=KEY)
    payload = json.dumps({'data': [0] * (endpoint.ZAI_LISTING_ROWS + 1)}).encode()
    with pytest.raises(ShipError, match='exceeds the row limit'):
        endpoint.models(ZAI_CHAT, api_key=KEY)


@pytest.mark.parametrize('override', [
    {'provider': {'api': 'https://example.invalid'}},
    {'headers': {'X-Example': 'synthetic'}},
    {'baseURL': 'https://example.invalid'},
    {'options': {'baseURL': 'https://example.invalid'}},
    {'id': 'other-model'},
])
def test_opencode_checks_routing_overrides_in_each_model_entry(tmp_path, override):
    path = tmp_path / 'config/opencode/opencode.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'provider': {'standard': {
        'npm': '@ai-sdk/openai-compatible',
        'options': {'baseURL': ZAI_CHAT, 'apiKey': '{env:QD_ENDPOINT_KEY}'},
        'models': {MODEL: {'name': MODEL}, 'another-model': override},
    }}}))
    pool = Pool('standard', 'zai', endpoint=ZAI_CHAT, api_key='keys:glm')
    with pytest.raises(keyfile.KeyProblem, match='remove authentication and routing overrides'):
        prepare_zai('opencode', pool, tmp_path)


def opencode_request(con, tick, *, base=MS, status=429, code='1113', provider='standard', agent='build', done=True,
                     sid='root', success=False, body=None):
    parent = f'{sid}-user-{tick}'
    answer = f'{sid}-assistant-{tick}'
    created = base + tick
    message(con, parent, sid, tick, {'role': 'user', 'agent': 'build', 'time': {'created': created},
                                    'model': {'providerID': provider, 'modelID': MODEL}})
    data = {'role': 'assistant', 'parentID': parent, 'agent': agent, 'providerID': provider, 'modelID': MODEL,
            'time': {'created': created + 1}}
    if done:
        data['time']['completed'] = created + 2
    if success:
        data['finish'] = 'stop'
        part(con, f'{answer}-finish', answer, sid, tick + 2, {'type': 'step-finish', 'reason': 'stop'})
    else:
        body = json.dumps({'error': {'code': code, 'message': 'arbitrary provider text'}}) if body is None else body
        data['error'] = {'name': 'APIError', 'data': {'statusCode': status, 'responseBody': body,
                                                    'message': 'HTTP 429, code 1113'}}
    message(con, answer, sid, tick + 1, data)
    con.commit()


def opencode_seat(record, provider='zai', launched_at=AT, *, proven=True):
    pool = Pool('standard', provider, api_key='keys:glm',
                endpoint=ZAI_CHAT if provider == 'zai' else 'https://example.invalid/v1')
    boundary = SimpleNamespace(pool=pool.name, at=launched_at.isoformat(), provider_route=(
        {'provider': pool.provider, 'address': sha256(pool.endpoint.encode()).hexdigest()} if proven else None))
    return SimpleNamespace(name='worker', harness=get('opencode'), ship=SimpleNamespace(pools={pool.name: pool}),
                           launched=lambda: boundary, record=lambda: record)


def test_only_post_hold_acceptance_clears_the_local_zai_wall(tmp_path):
    record = tmp_path / 'data-001/opencode/opencode.db'
    seat = opencode_seat(record)
    with closing(database(record)) as con, Ledger(tmp_path / 'ledger.sqlite') as book:
        session(con)
        opencode_request(con, 10)
        refused_at = AT + timedelta(milliseconds=12)
        retry = refused_at + timedelta(hours=1)
        quota.observe_key_refusal(book, seat, refused_at)
        quota.observe_key_refusal(book, seat, refused_at)
        wall, = book.quota.walls(refused_at)
        assert 'code 1113' in wall and 'reset unknown' in wall and 'next eligible try' in wall
        assert book.quota.walls(retry - timedelta(seconds=1)) == [wall]
        assert book.quota.walls(retry) == []  # the local window, never a reported provider reset

        # A restart or fresh launch on this route alone cannot clear the refusal.
        relaunched = opencode_seat(record, launched_at=refused_at + timedelta(seconds=1))
        quota.observe_key_refusal(book, relaunched, refused_at + timedelta(seconds=2))
        assert book.quota.walls(refused_at + timedelta(seconds=2)) == [wall]
        opencode_request(con, 4000, success=True)
        quota.observe_key_refusal(book, relaunched, AT + timedelta(seconds=5))
        assert book.quota.walls(AT + timedelta(seconds=5)) == [wall]

        opencode_request(con, 3601000, success=True)
        quota.observe_key_refusal(book, relaunched, AT + timedelta(seconds=3602))
        assert book.db.execute('SELECT superseded FROM wall_events').fetchall() == [(1,)]
        opencode_request(con, 3603000)
        quota.observe_key_refusal(book, relaunched, AT + timedelta(seconds=3604))
        assert len(book.quota.walls(AT + timedelta(seconds=3604))) == 1


@pytest.mark.parametrize('case', [
    'other-code', 'wrong-status', 'text-only', 'oversized', 'unfinished', 'title', 'child',
    'other-pool', 'wrong-provider', 'unproved-launch', 'before-launch',
])
def test_unproved_open_code_refusals_do_not_wall(tmp_path, case):
    record = tmp_path / 'data-001/opencode/opencode.db'
    seat = opencode_seat(record, 'google' if case == 'wrong-provider' else 'zai',
                        AT + timedelta(milliseconds=15) if case == 'before-launch' else AT,
                        proven=case != 'unproved-launch')
    request = {'code': '1302'} if case == 'other-code' else {}
    request.update({
        'wrong-status': {'status': 401}, 'text-only': {'body': 'HTTP 429, code 1113'},
        'oversized': {'body': json.dumps({'error': {'code': '1113', 'message': 'a' * key_proof.ZAI_ERROR_BYTES}})},
        'unfinished': {'done': False}, 'title': {'agent': 'title'}, 'child': {'sid': 'child'},
        'other-pool': {'provider': 'different'},
    }.get(case, {}))
    with closing(database(record)) as con, Ledger(tmp_path / 'ledger.sqlite') as book:
        session(con)
        if case == 'child':
            session(con, 'child', parent='root')
        opencode_request(con, 10, **request)
        quota.observe_key_refusal(book, seat, AT + timedelta(seconds=1))
        assert book.quota.walls(AT + timedelta(seconds=1)) == []


def test_claude_zai_rate_limit_text_changes_neither_walls_nor_pool(tmp_path, monkeypatch):
    path = declare(tmp_path)
    path.write_text(path.read_text().replace('pool = "standard"', 'pool = ["standard", "subscription"]'))
    ship = load(tmp_path)
    live = Pane('%1', 'Scratch', 'worker', os.getpid(), 100, 25, False, False, None)
    tmux = SimpleNamespace(pane=lambda ident: live if ident == live.id else None, capture=lambda *a, **kw: '')
    seat = Seat(ship, ship.seats['worker'], tmux)
    launched = Launched(live.id, live.pid, (now() - timedelta(seconds=1)).isoformat(),
                        'standard', None, ['claude'], '2.1.331', 'claude', start=process_start(live.pid),
                        provider_route={'provider': 'zai', 'address': sha256(ZAI_MESSAGES.encode()).hexdigest()})
    launched.write(seat.trusted / 'launch.json')
    source = ClaudeShape(seat.home, tmp_path, 'synthetic-joined', MODEL)
    source.begin('Reply OK.', MODEL, 'none')
    source.entry('assistant', {'role': 'assistant', 'model': '<synthetic>',
        'content': [{'type': 'text', 'text': 'API Error: Request rejected (429) · Insufficient balance or no resource package. Please recharge.'}]},
        isApiErrorMessage=True, error='rate_limit', apiErrorStatus=429, apiErrorIsTransient=False)
    assert seat.record() == source.path
    assert get('claude').state(source.path).ended_at_limit is None
    monkeypatch.setattr(service, 'seats', lambda *a: [seat])
    monkeypatch.setattr(seat, 'failover_to', lambda *args: pytest.fail('unexpected pool move'))
    svc = service.Service(ship, tmux)
    try:
        svc.failover()
        assert svc.ledger.quota.walls() == []
        assert seat.launched() == launched and seat.pool.name == 'standard'
    finally:
        svc.ledger.close()


def test_zai_opencode_normal_launch_records_the_route_for_wall_admission(tmp_path, key_file, monkeypatch):
    from quarterdeck.harness.opencode import OpenCodeHarness
    from quarterdeck import seat as seatmod

    declare(tmp_path, program='opencode', base=ZAI_CHAT)
    ship = load(tmp_path)
    key_file.write(glm=KEY)
    panes = {}
    launch_env = {}
    response = json.dumps({'data': [{'id': MODEL}]}).encode()
    monkeypatch.setattr(key_proof, 'keyed_opener',
                        lambda url: SimpleNamespace(open=lambda *a, **kw: io.BytesIO(response)))
    monkeypatch.setattr(OpenCodeHarness, 'version', lambda self: '1.19.5')
    monkeypatch.setattr(Seat, '_survives', lambda *a: None)
    monkeypatch.setattr(seatmod.shutil, 'which', lambda *a, **kw: '/usr/bin/sh')

    def new_window(title, display, cwd, argv, env):
        assert env['npm_config_offline'] == 'true'
        assert env['OPENCODE_DISABLE_PROJECT_CONFIG'] == '1'
        assert env['OPENCODE_DISABLE_LSP_DOWNLOAD'] == env['OPENCODE_DISABLE_DEFAULT_PLUGINS'] == '1'
        assert KEY not in repr((argv, env))
        launch_env.update(env)
        pane = Pane('%1', title, display, os.getpid(), 100, 25, False, False, None)
        panes[pane.id] = pane
        return pane

    tmux = SimpleNamespace(pane=panes.get, has_session=lambda title: True, new_window=new_window,
                           session_environment=lambda *a: str(tmp_path))
    seat = Seat(ship, ship.seats['worker'], tmux)
    assert 'provider Z.ai' in seat.up(resume=False)
    launched = seat.launched()
    assert launched.provider_route == {'provider': 'zai', 'address': sha256(ZAI_CHAT.encode()).hexdigest()}
    record = seat.home / 'data-001/opencode/opencode.db'
    assert launch_env['XDG_DATA_HOME'] == str(record.parents[1])
    with closing(database(record)) as con, Ledger(tmp_path / 'ledger.sqlite') as book:
        base = int(launched.when.timestamp() * 1000)
        session(con)
        con.execute('UPDATE session SET time_created=?, time_updated=?', (base + 1, base + 12))
        opencode_request(con, 10, base=base)
        assert seat.record() == record
        at = launched.when + timedelta(seconds=1)
        quota.observe_key_refusal(book, seat, at)
        assert 'code 1113' in book.quota.walls(at, pool='standard')[0]


SENTINEL = 'synthetic-sentinel'


def zai_home(tmp_path, monkeypatch, key_file, program, *, template=None, settings=None, base=ZAI_CHAT):
    """A built Z.ai seat home, with the ship template and seat settings layered as the normal launch layers them."""
    declare(tmp_path, program=program, base=base)
    ship = load(tmp_path)
    key_file.write(glm=KEY)
    listing = json.dumps({'data': [{'id': MODEL}]}).encode()
    monkeypatch.setattr(key_proof, 'keyed_opener',
                        lambda url: SimpleNamespace(open=lambda *a, **kw: io.BytesIO(listing)))
    spec = Seat(ship, ship.seats['worker'], None).adapter_spec()
    if settings is not None:
        spec = replace(spec, settings=settings)
    ship_home = None
    if template is not None:
        ship_home = tmp_path / 'templates'
        ship_home.mkdir()
        name = 'opencode.json' if program == 'opencode' else 'settings.json'
        (ship_home / name).write_text(json.dumps(template))
    home = tmp_path / 'runtime/seats/worker/home'
    adapter = get(program)
    adapter.build_home(home, spec, ship.pools['standard'], ship_home)
    return adapter, spec, ship.pools['standard'], home


QUALIFIED = f'standard/{MODEL}'


@pytest.mark.parametrize('layer', ['template', 'seat'])
@pytest.mark.parametrize('extra', [
    {},
    {'share': 'disabled', 'autoupdate': False, 'mcp': {}, 'plugin': [], 'enterprise': {}},
    {'model': QUALIFIED, 'small_model': QUALIFIED, 'enabled_providers': ['standard']},
    {'agent': {'build': {'model': QUALIFIED, 'permission': {'bash': 'allow'}}, 'review': {'prompt': 'Review.'}},
     'mode': {'plan': {'small_model': QUALIFIED}}},
    {'provider': {'standard': {'models': {MODEL: {'limit': {'context': 1000, 'output': 100}}}}}},
])
def test_zai_opencode_publishes_and_launches_an_allowed_final_configuration(tmp_path, key_file, monkeypatch,
                                                                            layer, extra):
    adapter, spec, pool, home = zai_home(tmp_path, monkeypatch, key_file, 'opencode',
                                         **{'template' if layer == 'template' else 'settings': extra})
    config = json.loads((home / 'config/opencode/opencode.json').read_text())
    assert config['model'] == config['small_model'] == QUALIFIED
    assert config['enabled_providers'] == ['standard']
    assert config['share'] == 'disabled' and config['autoupdate'] is False
    prepare_zai('opencode', pool, home)
    launch = adapter.launch(spec, pool, home, tmp_path / 'brief', None, None)
    assert launch.env['OPENCODE_DISABLE_LSP_DOWNLOAD'] == '1'
    assert launch.env['OPENCODE_DISABLE_DEFAULT_PLUGINS'] == '1'


@pytest.mark.parametrize('layer', ['template', 'seat'])
@pytest.mark.parametrize('extra,reason', [
    ({'model': f'standard/{SENTINEL}'}, 'selected model for both'),
    ({'small_model': f'{SENTINEL}/{MODEL}'}, 'selected model for both'),
    ({'agent': {'build': {'model': f'{SENTINEL}/{MODEL}'}}}, 'agents and modes'),
    ({'agent': {'title': {'small_model': f'standard/{SENTINEL}'}}}, 'agents and modes'),
    ({'mode': {'plan': {'model': f'{SENTINEL}/{MODEL}'}}}, 'agents and modes'),
    ({'agent': {'build': SENTINEL}}, 'agents and modes'),
    ({'provider': {SENTINEL: {'npm': '@ai-sdk/openai-compatible'}}}, 'only the selected pool provider'),
    ({'enabled_providers': [SENTINEL]}, 'only the selected pool provider'),
    ({'provider': {'standard': {'models': {SENTINEL: {'name': SENTINEL}}}}}, 'only the selected model'),
    ({'provider': {'standard': {'options': {'apiKey': f'{{env:{SENTINEL}}}'}}}}, 'routing overrides'),
    ({'provider': {'standard': {'options': {'headers': {'X-Sentinel': SENTINEL}}}}}, 'routing overrides'),
    ({'share': 'manual'}, 'disabled sharing and updates'),
    ({'autoshare': True}, 'disabled sharing and updates'),
    ({'autoupdate': True}, 'disabled sharing and updates'),
    ({'mcp': {SENTINEL: {'type': 'local', 'command': ['true']}}}, 'empty MCP, plugin and enterprise'),
    ({'plugin': [SENTINEL]}, 'empty MCP, plugin and enterprise'),
    ({'enterprise': {'url': f'https://{SENTINEL}.invalid'}}, 'empty MCP, plugin and enterprise'),
    ({'instructions': [f'{{env:{SENTINEL}}}']}, 'substitution marker'),
    ({'agent': {'build': {'prompt': f'{{file:/{SENTINEL}}}'}}}, 'substitution marker'),
    ({'agent': {f'{{env:{SENTINEL}}}': {}}}, 'substitution marker'),
    ({'provider': {'standard': {'models': {MODEL: {'name': f'{{ENV:{SENTINEL}}}'}}}}}, 'substitution marker'),
])
def test_zai_opencode_refuses_a_final_configuration_off_its_route(tmp_path, key_file, monkeypatch,
                                                                  layer, extra, reason):
    with pytest.raises(ShipError, match=reason) as failure:
        zai_home(tmp_path, monkeypatch, key_file, 'opencode',
                 **{'template' if layer == 'template' else 'settings': extra})
    assert SENTINEL not in str(failure.value)
    assert not (tmp_path / 'runtime/seats/worker/home/config/opencode/opencode.json').exists()


@pytest.mark.parametrize('change', [
    lambda config: config.update(mcp={SENTINEL: {'type': 'remote', 'url': f'https://{SENTINEL}.invalid'}}),
    lambda config: config.update(small_model=f'{SENTINEL}/{MODEL}'),
    lambda config: config['agent'].update(build={'prompt': f'{{env:{SENTINEL}}}'}),
])
def test_zai_opencode_launch_rechecks_the_published_configuration(tmp_path, key_file, monkeypatch, change):
    adapter, spec, pool, home = zai_home(tmp_path, monkeypatch, key_file, 'opencode')
    path = home / 'config/opencode/opencode.json'
    config = json.loads(path.read_text())
    config.setdefault('agent', {})
    change(config)
    path.write_text(json.dumps(config))
    with pytest.raises(ShipError, match='Z.ai OpenCode') as failure:
        adapter.launch(spec, pool, home, tmp_path / 'brief', None, None)
    assert SENTINEL not in str(failure.value)


def test_zai_opencode_launch_refuses_a_model_choice_other_than_the_declaration(tmp_path, key_file, monkeypatch):
    adapter, spec, pool, home = zai_home(tmp_path, monkeypatch, key_file, 'opencode')
    with pytest.raises(ShipError, match='differs from the declaration'):
        adapter.launch(replace(spec, model=SENTINEL), pool, home, tmp_path / 'brief', None, None)


def test_pi_zai_launch_is_offline(tmp_path, key_file, monkeypatch):
    adapter, spec, pool, home = zai_home(tmp_path, monkeypatch, key_file, 'pi')
    prepare_zai('pi', pool, home)
    launch = adapter.launch(spec, pool, home, tmp_path / 'brief', None, None)
    assert launch.env['PI_OFFLINE'] == '1'


def test_zai_switches_pass_the_launch_environment_filter():
    from quarterdeck.jail_environment import without_refused

    switches = {name: '1' for name in ('OPENCODE_DISABLE_LSP_DOWNLOAD', 'OPENCODE_DISABLE_DEFAULT_PLUGINS',
                                       'OPENCODE_DISABLE_MODELS_FETCH', 'OPENCODE_PURE', 'PI_OFFLINE')}
    switches['npm_config_offline'] = 'true'
    assert without_refused(switches) == switches


def test_claude_zai_settings_skip_the_fetch_preflight(tmp_path, key_file, monkeypatch):
    _, _, pool, home = zai_home(tmp_path, monkeypatch, key_file, 'claude', base=ZAI_MESSAGES,
                                template={'skipWebFetchPreflight': True, 'autoUploadSessions': False})
    prepare_zai('claude', pool, home)
    settings = json.loads((home / 'settings.json').read_text())
    assert settings['skipWebFetchPreflight'] is True
    assert settings['autoUploadSessions'] is False  # the template's own false is kept


@pytest.mark.parametrize('layer', ['template', 'seat'])
@pytest.mark.parametrize('extra,reason', [
    ({'skipWebFetchPreflight': False}, 'skipWebFetchPreflight to be true'),
    ({'skipWebFetchPreflight': 'true'}, 'skipWebFetchPreflight to be true'),
    ({'autoUploadSessions': True}, 'refuses autoUploadSessions'),
])
def test_claude_zai_refuses_fetch_preflight_or_session_upload_overrides(tmp_path, key_file, monkeypatch,
                                                                       layer, extra, reason):
    with pytest.raises(ShipError, match=reason):
        zai_home(tmp_path, monkeypatch, key_file, 'claude', base=ZAI_MESSAGES,
                 **{'template' if layer == 'template' else 'settings': extra})


@pytest.mark.parametrize('settings', [
    {'env': {}},
    {'env': {}, 'skipWebFetchPreflight': False},
    {'env': {}, 'skipWebFetchPreflight': True, 'autoUploadSessions': True},
])
def test_claude_zai_prepare_rechecks_the_written_settings(tmp_path, settings):
    pool = Pool('standard', 'zai', endpoint=ZAI_MESSAGES, api_key='keys:glm')
    (tmp_path / 'settings.json').write_text(json.dumps(settings))
    with pytest.raises(keyfile.KeyProblem, match='remove authentication and routing overrides'):
        prepare_zai('claude', pool, tmp_path)


def test_other_pools_keep_their_settings_and_launch_environment(tmp_path, monkeypatch):
    from quarterdeck.harness.claude import ClaudeHarness

    ship_home = tmp_path / 'templates'
    ship_home.mkdir()
    (ship_home / 'settings.json').write_text(json.dumps({'autoUploadSessions': True, 'skipWebFetchPreflight': False}))
    declare(tmp_path)
    ship = load(tmp_path)
    spec = Seat(ship, ship.seats['worker'], None).adapter_spec()
    home = tmp_path / 'claude-home'
    monkeypatch.setattr('quarterdeck.harness.claude.credential', lambda *a, **kw: None)
    ClaudeHarness().build_home(home, spec, ship.pools['subscription'], ship_home)
    settings = json.loads((home / 'settings.json').read_text())
    assert settings['autoUploadSessions'] is True and settings['skipWebFetchPreflight'] is False

    pool = Pool('local', 'local', endpoint='http://127.0.0.1:9/v1')
    monkeypatch.setattr(endpoint, 'models', lambda *a, **kw: [endpoint.Served(MODEL)])
    for program in ('opencode', 'pi'):
        adapter = get(program)
        other = tmp_path / f'{program}-home'
        layered = {'mcp': {'local': {'type': 'local', 'command': ['true']}}, 'small_model': 'local/other'}
        adapter.build_home(other, replace(spec, settings=layered if program == 'opencode' else {}), pool, None)
        launch = adapter.launch(spec, pool, other, tmp_path / 'brief', None, None)
        for name in ('OPENCODE_DISABLE_LSP_DOWNLOAD', 'OPENCODE_DISABLE_DEFAULT_PLUGINS', 'PI_OFFLINE',
                     'npm_config_offline'):
            assert name not in launch.env
        if program == 'opencode':
            config = json.loads((other / 'config/opencode/opencode.json').read_text())
            assert config['mcp'] == layered['mcp'] and config['small_model'] == 'local/other'
            assert 'enabled_providers' not in config
