"""The declared Grok executable is shared by all consumers. Every executable here is synthetic."""

from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from quarterdeck import check, credential, doctor, harness, quota, seat
from quarterdeck.drill import Check
from quarterdeck.harness import grok
from quarterdeck.ship import Pool, ProgramPinError, Session, ShipError, load
from quarterdeck.tmux import Pane


@pytest.fixture
def pinned(tmp_path, monkeypatch):
    program = tmp_path / 'runtime/programs/grok-8.6.4-linux-x86_64'
    program.parent.mkdir(parents=True, mode=0o700)
    (program.parent.parent / '.git').mkdir()
    program.write_text(f'#!{sys.executable} -IBS\n' + '''
import json
import os
from pathlib import Path
import sys
import tomllib

assert os.environ['GROK_DISABLE_AUTOUPDATER'] == '1'
config = tomllib.loads(Path(os.environ['GROK_CONFIG_PATH']).read_text())
assert config['cli'] == {'auto_update': False, 'use_leader': False}
args = sys.argv[1:]
if args == ['--version']:
    print('Grok Build 8.6.4')
elif args == ['agent', '--no-leader', 'stdio']:
    def answer(method, value):
        request = json.loads(input())
        assert request['method'] == method
        print(json.dumps({'id': request['id'], 'result': value}), flush=True)
    answer('initialize', {})
    answer('_x.ai/billing', {'config': {'creditUsagePercent': 31, 'currentPeriod': {
        'type': 'USAGE_PERIOD_TYPE_WEEKLY', 'start': '2030-01-01T00:00:00Z',
        'end': '2030-01-08T00:00:00Z'}}})
else:
    assert args[0] == '--fullscreen'
    print('started the pinned executable')
''')
    program.chmod(0o500)
    digest = sha256(program.read_bytes()).hexdigest()
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    sign_in = tmp_path / 'empty-fixture'
    sign_in.mkdir()
    (sign_in / 'auth.json').write_text('{}')
    (tmp_path / 'ship.toml').write_text(f'''
[ship]
name = "synthetic"
code = "checkout"
workspace = "workspace"
[programs.grok]
path = "{program.relative_to(tmp_path)}"
sha256 = "{digest}"
[pools.example]
provider = "xai"
config_dir = "{sign_in}"
[sessions.crew]
title = "Synthetic"
[seats.example]
session = "crew"
role = "worker"
harness = "grok"
pool = "example"
''')
    competing = tmp_path / 'bin/grok'
    competing.parent.mkdir()
    competing.write_text('#!/bin/sh\nprintf "wrong program ran\\n" >> "$0.ran"\nprintf "9.9.9\\n"\n')
    competing.chmod(0o700)
    monkeypatch.setenv('PATH', str(competing.parent) + os.pathsep + os.environ['PATH'])
    monkeypatch.setattr(seat, 'program_path', lambda: str(competing.parent))
    monkeypatch.setattr(doctor, 'program_path', lambda: str(competing.parent))
    monkeypatch.setattr(grok, 'SYSTEM_CONFIG', tmp_path / 'empty-admin-layer')
    for name in grok.INHERITED_REDIRECTS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(grok, 'credential', lambda *args, **kwargs: None)
    monkeypatch.setattr(credential, 'reseed', lambda *args, **kwargs: ())
    ship = load(tmp_path)
    yield ship, program, digest
    assert not competing.with_suffix('.ran').exists()


def _billing(ship):
    service = SimpleNamespace(ship=ship, ledger=SimpleNamespace(prepare_usage=lambda: None), resolved_seats=list)
    pool = ship.pools['example']
    return quota.read_usage(service, pool, harness.native_for(pool.provider))


def _launch(launched):
    spec = launched.adapter_spec()
    launched.harness.build_home(launched.home, spec, launched.pool, None)
    brief = launched.home / 'brief'
    brief.write_text('Synthetic launch instructions')
    return launched.harness.launch(spec, launched.pool, launched.home, brief, None, None)


def _program_row(ship):
    return next(row for row in doctor.checks(ship) if row.name == 'program grok')


def test_launch_doctor_check_and_billing_use_the_ship_pin(pinned, monkeypatch):
    ship, program, digest = pinned
    declared = seat.Seat(ship, ship.seats['example'], None)
    version, _ = declared._observe_selection()
    assert version == '8.6.4'
    launch = _launch(declared)
    assert launch.argv[0] == str(program)
    started = subprocess.run(launch.argv, env=launch.env, capture_output=True, text=True, timeout=5, check=True)
    assert started.stdout.strip() == 'started the pinned executable'

    legacy_mark = ship.runtime / 'proven/grok-8.6.4'
    legacy_mark.parent.mkdir()
    legacy_mark.write_text('earlier PATH check\n')
    row = _program_row(ship)
    assert row.passed and 'unproven' in row.detail and str(program) in row.detail

    @contextmanager
    def temporary(ship, tmux, spec, orders, purpose, *, installed_version=None, human=False, **kwargs):
        treated = Session(spec.session, spec.session, human=human)
        candidate = seat.Seat(replace(ship, sessions={**ship.sessions, spec.session: treated}), spec, tmux)
        if not human:
            assert installed_version == version
        launched = SimpleNamespace(argv=list(_launch(candidate).argv))
        candidate.launched = lambda: launched
        yield candidate

    monkeypatch.setattr(check, 'temporary_seat', temporary)
    monkeypatch.setattr(check, 'probe', lambda *args: [Check('synthetic probe', True)])
    monkeypatch.setattr(check, 'probe_wake', lambda *args: [Check('synthetic wake', True)])
    rows = check.check(ship, 'grok', None)
    assert str(program) in rows[0].detail and version in rows[0].detail
    assert next(row for row in rows if row.name == 'the seat ran the program doctor shows').passed
    mark = ship.runtime / 'proven' / f'grok-8.6.4-{digest}'
    assert mark.read_text().splitlines()[1] == str(program)
    assert 'proven by qd check' in _program_row(ship).detail

    reading = _billing(ship)
    assert reading.error is None and reading.windows[0].used == 0.31


@pytest.mark.parametrize('failure,reason', [
    ('missing', 'is missing'), ('modified', 'SHA-256 does not match'),
    ('wrong-mode', 'mode 0500'),
])
def test_bad_pin_refuses_all_consumers(pinned, failure, reason):
    ship, program, _ = pinned
    if failure == 'missing':
        program.unlink()
    elif failure == 'modified':
        program.chmod(0o700)
        with program.open('a') as changed:
            changed.write('# different bytes\n')
        program.chmod(0o500)
    else:
        program.chmod(0o700)
    declared = seat.Seat(ship, ship.seats['example'], None)
    with pytest.raises(ProgramPinError, match=reason):
        declared._observe_selection()
    with pytest.raises(ProgramPinError, match=reason):
        declared.harness.launch(declared.adapter_spec(), declared.pool, declared.home, Path('brief'), None, None)
    row = _program_row(ship)
    assert not row.passed and reason in row.detail
    rows = check.check(ship, 'grok', None)
    assert len(rows) == 1 and not rows[0].passed and reason in rows[0].detail
    reading = _billing(ship)
    assert not reading.called and reason in reading.error


@pytest.mark.parametrize('action,replace_after_version', [
    ('failover', False), ('up', False), ('off-wall', False), ('recovery', False), ('failover', True),
])
def test_cross_program_move_verifies_the_pin_at_first_launch(pinned, monkeypatch, action, replace_after_version):
    ship, program, _ = pinned
    spec = replace(ship.seats['example'], harness='codex', pools=('source', 'example'),
                   pool_choices={'source': {'harness': 'codex'}, 'example': {'harness': 'grok'}})
    ship = replace(ship, seats={spec.name: spec}, pools={**ship.pools, 'source': Pool('source', 'openai')})
    candidate = seat.Seat(ship, spec, SimpleNamespace(pane=lambda ident: None))
    assert candidate.harness.name == 'codex' and candidate.pool.name == 'source'
    launched = []

    def open_window(argv, env, path):
        output = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=5, check=True)
        launched.append((argv[0], output.stdout.strip()))
        return Pane('%1', 'Synthetic', 'example', os.getpid(), 100, 25, False, False, None)

    monkeypatch.setattr(candidate, '_open_window', open_window)
    monkeypatch.setattr(candidate, '_survives', lambda *args: None)
    monkeypatch.setattr(candidate, '_relaunch_down', lambda: 'earlier synthetic program stopped')
    monkeypatch.setattr(candidate, '_off_wall_destination', lambda: ('example', 'synthetic destination'))
    monkeypatch.setattr(candidate, '_path', lambda: os.environ['PATH'])
    actions = {
        'failover': lambda: candidate.failover_to('example'),
        'up': lambda: candidate.up(resume=False, pool='example'),
        'off-wall': lambda: candidate._relaunch_off_wall(resume=False),
        'recovery': lambda: candidate._up_on_recorded_pool('example', 'grok', resume=False),
    }
    if replace_after_version:
        observe = candidate._observe_selection

        def observe_then_replace(*args):
            observation = observe(*args)
            replacement = program.with_name('changed')
            replacement.write_text('#!/bin/sh\nprintf "wrong program ran\\n"\n')
            replacement.chmod(0o500)
            replacement.replace(program)
            return observation

        monkeypatch.setattr(candidate, '_observe_selection', observe_then_replace)
        with pytest.raises(ProgramPinError, match='SHA-256 does not match'):
            actions[action]()
        assert launched == [] and candidate.launched() is None
    else:
        actions[action]()
        assert launched == [(str(program), 'started the pinned executable')]
        recorded = candidate.launched()
        assert recorded.harness == 'grok' and recorded.version == '8.6.4'
        assert recorded.argv[0] == str(program)


@pytest.mark.parametrize('setting', [
    'path = "/tmp/grok"\nsha256 = "' + 'a' * 64 + '"',
    'path = "runtime/programs/../grok"\nsha256 = "' + 'a' * 64 + '"',
    'path = "runtime/programs/grok"\nsha256 = "bad digest"',
    'path = "runtime/programs/grok"',
])
def test_invalid_pin_declaration_fails_at_load(tmp_path, setting):
    (tmp_path / 'ship.toml').write_text('[programs.grok]\n' + setting)
    with pytest.raises(ShipError, match='programs.grok'):
        load(tmp_path)
