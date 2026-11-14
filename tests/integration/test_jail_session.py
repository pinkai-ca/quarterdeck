"""A jail does not reach the operator's desktop session, and still reaches the model endpoint on loopback.

Real bubblewrap, stand-in programs only: a Python probe inside a jail built by sandbox.plan, and a stand-in model
endpoint (a plain HTTP server) on loopback outside. From inside, every unix socket that ``ss -xl`` lists outside
fails to connect when it has a path. An abstract socket follows the network namespace the jail still shares; those
are named, not refused, until the jail gets a network of its own.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from quarterdeck import sandbox, ship as shipmod

from .test_sandbox import namespaces_allowed

pytestmark = pytest.mark.skipif(not namespaces_allowed(), reason="bubblewrap is not installed, or cannot make a namespace here (inside a jail)")

KINDS = {'u_str': 'stream', 'u_seq': 'seqpacket', 'u_dgr': 'datagram'}


def listening() -> list[dict]:
    """What ``ss -xl`` lists outside: each listening unix socket's kind and address, path or abstract."""
    if shutil.which('ss') is None:
        pytest.skip('ss is not installed')
    out = subprocess.run(['ss', '-xlH'], capture_output=True, text=True, timeout=30).stdout
    found = []
    for line in out.splitlines():
        fields = line.split()
        if len(fields) < 5 or fields[0] not in KINDS or fields[4] in ('*', ''):
            continue
        found.append(dict(kind=KINDS[fields[0]], address=fields[4]))
    return found


PROBE = r"""
import json, os, socket, sys, urllib.request
sockets, endpoint, uid = json.loads(sys.argv[1]), sys.argv[2], sys.argv[3]
kinds = dict(stream=socket.SOCK_STREAM, seqpacket=socket.SOCK_SEQPACKET, datagram=socket.SOCK_DGRAM)
reached = []
for item in sockets:
    address = item['address']
    target = '\0' + address[1:] if address.startswith('@') else address
    probe = socket.socket(socket.AF_UNIX, kinds[item['kind']])
    probe.settimeout(1)
    try:
        probe.connect(target)
        reached.append(address)
    except OSError:
        pass
    finally:
        probe.close()
runtime = os.environ.get('XDG_RUNTIME_DIR')
try:
    answer = urllib.request.urlopen(endpoint, timeout=5).status
except OSError as error:
    answer = repr(error)
scratch = sorted(os.listdir('/mnt/scratch')) if os.path.isdir('/mnt/scratch') else None
print(json.dumps(dict(
    reached=reached,
    session={name: os.environ.get(name) for name in json.loads(sys.argv[4])},
    runtime=runtime, runtime_entries=sorted(os.listdir(runtime)) if runtime and os.path.isdir(runtime) else None,
    runtime_writable=bool(runtime) and os.access(runtime, os.W_OK),
    run=sorted(os.listdir('/run')), scratch=scratch, endpoint=answer,
)))
"""


class Answer(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # the stand-in model endpoint
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"object": "list", "data": []}')

    def log_message(self, *args):
        pass


@pytest.fixture
def endpoint():
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Answer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_address[1]}/v1/models'
    server.shutdown()


def test_no_path_socket_outside_is_reachable_and_the_endpoint_still_answers(drydock: Path, tmp_path: Path, endpoint):
    workspace = tmp_path / 'work'
    workspace.mkdir()
    ship = shipmod.load(drydock)
    shipmod.fence(ship)
    own = ship.runtime / 'seats' / 'worker-1'
    (own / 'home').mkdir(parents=True, exist_ok=True)
    import pwd
    operator_home = Path(pwd.getpwuid(os.getuid()).pw_dir)  # the real home, as a launch hides it, not the run's own HOME
    jail = sandbox.plan(ship, 'worker-1', own, own / 'home', workspace, sys.executable, os.environ['PATH'],
                        home=operator_home)
    outside = listening()
    paths = [item['address'] for item in outside if not item['address'].startswith('@')]
    env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS='unix:path=/run/user/%d/bus' % os.getuid(), DISPLAY=':0',
               WAYLAND_DISPLAY='wayland-0', SSH_AUTH_SOCK='/run/user/%d/openssh_agent' % os.getuid(),
               GPG_AGENT_INFO='/run/user/%d/gnupg/S.gpg-agent' % os.getuid(), XAUTHORITY='/run/user/%d/gdm/Xauthority' % os.getuid())
    result = subprocess.run(sandbox.command(jail, [sys.executable, '-I', '-c', PROBE, json.dumps(outside), endpoint,
                                                   str(os.getuid()), json.dumps(sandbox.SESSION_VARIABLES)]),
                            capture_output=True, text=True, env=env, timeout=120)
    assert result.returncode == 0, result.stderr
    seen = json.loads(result.stdout.strip().splitlines()[-1])
    reached_paths = [address for address in seen['reached'] if not address.startswith('@')]
    assert reached_paths == [], f'path sockets reachable from inside the jail: {reached_paths}'
    abstract = [address for address in seen['reached'] if address.startswith('@')]
    print('abstract sockets still reachable until the jail has its own network:', abstract)
    assert seen['session'] == {name: None for name in sandbox.SESSION_VARIABLES}, seen['session']
    assert seen['runtime'] == f'/run/user/{os.getuid()}' and seen['runtime_entries'] == [] and seen['runtime_writable']
    assert set(seen['run']) <= {'user', 'systemd'}, seen['run']
    assert seen['scratch'] in (None, []), 'the shared scratch is hidden'
    assert seen['endpoint'] == 200, seen['endpoint']
    if Path(f'/run/user/{os.getuid()}/bus').exists():
        assert f'/run/user/{os.getuid()}/bus' in paths, 'the session bus was among what was tried'
