"""A direct or relayed jailed key launch starts its declared synthetic executable."""

from hashlib import sha256

import pytest

from quarterdeck import key_launch, keyfile, ship as shipmod
from quarterdeck.harness.claude import ClaudeHarness
from quarterdeck.seat import Seat

from .waiting import wait_for


@pytest.mark.parametrize('relayed', [False, True], ids=['direct-key', 'keyed-endpoint'])
def test_jailed_key_pool_starts_its_pinned_executable(tmp_path, key_file, tmux, monkeypatch, relayed):
    folder = tmp_path / 'ships' / 'pin-test'
    program = folder / 'runtime' / 'programs' / 'test-executable'
    program.parent.mkdir(parents=True, mode=0o700)
    program.write_text(f'''#!/bin/sh
if [ "$1" = --version ]; then
    printf '1.2.3\\n'
    exit 0
fi
[ -n "${keyfile.HELPER_VARIABLE}" ] || exit 1
printf 'pinned program started\\n' > "$CLAUDE_CONFIG_DIR/pin-ready"
read -r line
''')
    program.chmod(0o500)
    digest = sha256(program.read_bytes()).hexdigest()
    endpoint = 'http://127.0.0.1:47173/v1'
    endpoint_line = f'endpoint = "{endpoint}"\n' if relayed else ''
    sandbox_line = '' if relayed else 'sandbox = true\n'
    (folder / 'ship.toml').write_text(f'''[ship]
name = "pin-test"
code = "checkout"
workspace = "../.."
[pools.test]
provider = "anthropic"
api_key = "keys:test"
{endpoint_line}[programs.claude]
path = "{program.relative_to(folder)}"
sha256 = "{digest}"
[sessions.crew]
title = "Pin test"
[seats.example]
session = "crew"
role = "worker"
harness = "claude"
pool = "test"
{sandbox_line}''')
    key_file.write(test='synthetic-key')
    if relayed:
        # Model an endpoint-capable pinned adapter on a base where Claude only declares native pools.
        def accepts_test_endpoint(self, pool):
            return None if pool.endpoint == endpoint else 'expected the synthetic endpoint'

        monkeypatch.setattr(ClaudeHarness, 'accepts', accepts_test_endpoint)
    else:
        # A stand-in with no provider connection can use the offline key-pool admission.
        monkeypatch.setattr(ClaudeHarness, 'synthetic', True)
    ship = shipmod.load(folder)
    seat = Seat(ship, ship.seat('example'), tmux)
    assert seat.pool.kind == ('endpoint' if relayed else 'key')
    if relayed:
        assert seat.spec.sandbox is None

    seat.up(resume=False)

    ready = seat.home / 'pin-ready'
    wait_for(lambda: ready.is_file() and ready.read_text() == 'pinned program started\n')
    launched = seat.launched()
    assert launched.pool == 'test' and launched.version == '1.2.3'
    assert '--jail' in launched.argv[:launched.argv.index('--')]
    jail = key_launch.inner(launched.argv)
    assert ('quarterdeck.endpoint_relay' in jail) is relayed
    bind = jail.index(str(program))
    assert jail[bind - 1:bind + 2] == ['--ro-bind', str(program), str(program)]
    assert str(program.parent) not in jail
