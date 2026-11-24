"""Publisher transport fixtures exercise the real signature, checksum and staging path."""

from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from quarterdeck import cli
from quarterdeck.harness import claude_release as releases
from quarterdeck.ship import ProgramPin, ShipError, _from_text
from quarterdeck.watch import Facts

VERSION = '9.8.7'
PLATFORM = 'linux-x64'
ARTIFACT = b'synthetic Claude release executable\n'
BASE = releases.RELEASES
LATEST = f'{BASE}/latest'
MANIFEST = f'{BASE}/{VERSION}/manifest.json'
BINARY = f'{BASE}/{VERSION}/{PLATFORM}/claude'


def test_publisher_trust_constants():
    # C11: Changes to any of these constants go to Security before landing.
    assert releases.ORIGIN == 'https://downloads.claude.ai'
    assert releases.KEY_URL == 'https://downloads.claude.ai/keys/claude-code.asc'
    assert releases.FINGERPRINT == '31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE'
    assert releases.PLATFORMS == ('linux-x64', 'linux-arm64')


@pytest.fixture(scope='module')
def signer(tmp_path_factory):
    # Short, run-owned GnuPG paths also work where agent sockets have a short path limit.
    home = tmp_path_factory.getbasetemp() / 'gpg-release'
    home.mkdir(mode=0o700)
    env = releases._env(home)

    def gpg(*args, data=None):
        return subprocess.run([releases._tool('gpg'), '--no-options', '--homedir', str(home), '--batch',
                               '--no-tty', '--pinentry-mode', 'loopback', *args], input=data, env=env,
                              check=True, capture_output=True).stdout

    try:
        identities = {}
        for name in ('release-author', 'separate-author', 'unrelated-author'):
            user_id = name.replace('-', ' ').title()
            gpg('--passphrase', '', '--quick-generate-key', user_id, 'ed25519', 'sign', '0')
            identity = gpg('--with-colons', '--fingerprint', user_id).decode()
            fingerprint = next(line.split(':')[9] for line in identity.splitlines() if line.startswith('fpr:'))
            key = gpg('--armor', '--export', fingerprint)
            identities[name] = (fingerprint, key)

        def sign(who, raw):
            return gpg('--local-user', identities[who][0], '--armor', '--detach-sign', data=raw)

        def export(*names):
            return gpg('--armor', '--export', *(identities[name][0] for name in names))

        yield identities, sign, export
    finally:
        subprocess.run([releases._tool('gpgconf'), '--homedir', str(home), '--kill', 'gpg-agent'],
                       env=env, check=True, capture_output=True)


@contextmanager
def reply_pipe(data):
    reader, writer = os.pipe2(os.O_NONBLOCK)
    with os.fdopen(reader, 'rb') as stream:
        try:
            assert os.write(writer, data) == len(data)
        finally:
            os.close(writer)
        yield stream


class Network:
    def __init__(self, sign):
        self.sign = sign
        self.calls = []
        self.replies = {}
        self.set(LATEST, VERSION.encode(), ETag='"latest"', **{'Last-Modified': 'Tue, 24 Nov 2026 12:00:00 GMT'})
        self.set(BINARY, ARTIFACT, **{'Content-Length': str(len(ARTIFACT))})
        self.manifest()

    def set(self, url, body=b'', *, status=200, exit_code=0, **headers):
        self.replies[url] = (status, body, exit_code, headers)

    def manifest(self, *, version=VERSION, platform=PLATFORM, artifact=ARTIFACT, who='release-author'):
        raw = json.dumps({'version': version, 'platforms': {platform: {
            'binary': 'claude', 'size': len(artifact), 'checksum': sha256(artifact).hexdigest(),
        }}}).encode()
        self.set(MANIFEST, raw)
        self.set(f'{MANIFEST}.sig', self.sign(who, raw))
        return raw

    @contextmanager
    def curl(self, stage, url, request_headers, limit, seconds):
        self.calls.append((url, request_headers, limit))
        status, body, exit_code, headers = self.replies[url]
        wire = [f'HTTP/1.1 {status} Fixture', *(f'{name}: {value}' for name, value in headers.items()), '', '']
        with reply_pipe('\r\n'.join(wire).encode()) as reply, reply_pipe(body) as stdout:
            yield SimpleNamespace(stdout=stdout, wait=lambda *, timeout: exit_code), reply.fileno()


@pytest.fixture
def world(tmp_path, monkeypatch, signer):
    keys, sign, _ = signer
    fingerprint, key = keys['release-author']
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    program_pin = ProgramPin('claude', runtime / 'programs' / 'claude-9.8.6', '1' * 64)
    clock = SimpleNamespace(wall=10_000, monotonic=8_000, boot='boot-a')

    def read_clock():
        return clock.wall, clock.monotonic, clock.boot

    def advance(seconds):
        clock.wall += seconds
        clock.monotonic += seconds

    net = Network(sign)
    net.set(releases.KEY_URL, key)
    monkeypatch.setattr(releases, 'FINGERPRINT', fingerprint)
    monkeypatch.setattr(releases, '_curl', net.curl)
    monkeypatch.setattr(releases, '_platform', lambda: PLATFORM)
    folder = runtime / 'program-releases' / 'claude'
    ship = SimpleNamespace(runtime=runtime, release_sources=('claude',), program_pins={'claude': program_pin})
    return SimpleNamespace(runtime=runtime, pin=program_pin, programs=runtime / 'programs', keys=keys,
                           folder=folder, ship=ship, clock=clock, advance=advance, net=net,
                           poll=lambda: releases.poll(runtime, program_pin, clock=read_clock),
                           state=lambda: releases._read_state(folder))


def test_stages_verified_content_and_records_each_request(world):
    assert world.poll() == releases.HOUR
    state = world.state()
    candidate = state.candidate()
    assert state.result == 'staged' and state.failures == 0, state
    assert candidate.sha256 == sha256(ARTIFACT).hexdigest()
    assert candidate.manifest_sha256 == sha256(world.net.replies[MANIFEST][1]).hexdigest()
    assert state.pending == candidate.receipt()
    assert state.pending['origin'] == BINARY
    assert state.pending['signing_fingerprint'] == world.keys['release-author'][0]
    executable = world.runtime.parent / state.pending['path']
    assert executable.read_bytes() == ARTIFACT
    assert stat.S_IMODE(executable.stat().st_mode) == 0o500
    assert stat.S_IMODE(executable.parent.stat().st_mode) == 0o500
    assert stat.S_IMODE(world.programs.stat().st_mode) == 0o700
    assert set(world.programs.iterdir()) == {executable.parent}
    assert not world.pin.path.exists()
    receipts = [json.loads(line) for line in (world.folder / 'requests.jsonl').read_text().splitlines()]
    assert [row['origin'] for row in receipts] == [LATEST, MANIFEST, f'{MANIFEST}.sig', releases.KEY_URL, BINARY]
    for row in receipts:
        status, body, _, _ = world.net.replies[row['origin']]
        assert (row['status'], row['bytes'], row['sha256']) == (status, len(body), sha256(body).hexdigest())


@pytest.mark.parametrize('overflow', [False, True])
def test_cumulative_header_limit_stops_curl_during_receipt(tmp_path, monkeypatch, overflow):
    curl = tmp_path / 'curl'
    curl.write_text(f'''#!{sys.executable}
import signal
import sys

args = sys.argv[1:]
interim = b'HTTP/1.1 103 Early Hints\\r\\nX-Interim: ' + b'i' * 32768 + b'\\r\\n\\r\\n'
final = b'HTTP/1.1 200 OK\\r\\nETag: "final"\\r\\nX-Final: '
end = b'\\r\\n\\r\\n'
pad = {releases.METADATA_BYTES} + {int(overflow)} - len(interim) - len(final) - len(end)
with open(args[args.index('--dump-header') + 1], 'wb') as headers:
    headers.write(interim)
    headers.flush()
    headers.write(final + b'f' * pad + end)
    headers.flush()
    if {overflow!r}:
        signal.pause()
sys.stdout.buffer.write(b'body')
''')
    curl.chmod(0o700)
    monkeypatch.setattr(releases, '_tool', lambda name: str(curl))
    stage = tmp_path / 'stage'
    stage.mkdir()
    popen = subprocess.Popen
    children = []

    def capture(*args, **kwargs):
        process = popen(*args, **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(releases.subprocess, 'Popen', capture)
    target = BytesIO()
    with releases._directory(stage) as scratch:
        fetch = releases.Fetch(tmp_path, scratch)
        if overflow:
            with pytest.raises(releases.Held, match=releases.Reason.BOUNDS):
                fetch.get(LATEST, target, 64)
        else:
            response = fetch.get(LATEST, target, 64)
            assert response.status == 200 and response.headers['ETag'] == '"final"'
    child, = children
    assert child.returncode == (-signal.SIGKILL if overflow else 0)
    assert not list(stage.iterdir())
    receipt, = (tmp_path / 'requests.jsonl').read_text().splitlines()
    body = b'' if overflow else b'body'
    assert target.getvalue() == body
    row = json.loads(receipt)
    assert (row['origin'], row['bytes'], row['sha256']) == (LATEST, len(body), sha256(body).hexdigest())
    assert (row['status'], row['transport_exit']) == ((None, None) if overflow else (200, 0))


def test_restarted_poll_respects_interval_and_304_uses_validators(world):
    world.poll()
    world.net.calls.clear()
    world.clock.wall += 2 * releases.HOUR  # same-boot restart must still respect elapsed time
    assert world.poll() == releases.HOUR
    assert not world.net.calls
    world.advance(releases.HOUR)
    world.net.set(LATEST, status=304)
    assert world.poll() == releases.HOUR
    assert world.net.calls == [(LATEST, {'If-None-Match': '"latest"',
                                         'If-Modified-Since': 'Tue, 24 Nov 2026 12:00:00 GMT'}, 256)]
    assert world.state().result == 'unchanged'
    assert world.state().pending['sha256'] == sha256(ARTIFACT).hexdigest()


def test_duplicate_manifest_reuses_inode_and_changed_digest_has_its_own_path(world):
    world.poll()
    first = world.runtime.parent / world.state().pending['path']
    inode = first.stat().st_ino
    world.advance(releases.HOUR)
    world.net.calls.clear()
    world.poll()
    assert world.state().result == 'duplicate'
    assert not any(url == BINARY for url, _, _ in world.net.calls)
    assert first.stat().st_ino == inode

    changed = b'different publisher-signed bytes at the same version\n'
    world.net.manifest(artifact=changed)
    world.net.set(BINARY, changed)
    world.advance(releases.HOUR)
    world.poll()
    second = world.runtime.parent / world.state().pending['path']
    assert world.state().result == 'staged'
    assert first != second and first.stat().st_ino == inode
    assert second.read_bytes() == changed


@pytest.mark.parametrize(('latest', 'result', 'note'), [
    ('9.8.5', 'publisher-rollback', 'claude publisher rollback: latest 9.8.5, active 9.8.6'),
    ('9.8.6', 'at-active', None),
])
def test_older_or_equal_release_in_status(world, latest, result, note, monkeypatch, capsys):
    ship_folder = world.runtime.parent
    (ship_folder / 'ship.toml').write_text(f"""[ship]
name = "synthetic"
release_sources = ["claude"]
[programs.claude]
path = "runtime/programs/{world.pin.path.name}"
sha256 = "{world.pin.sha256}"
""")
    monkeypatch.delenv('QD_TMUX_SOCKET', raising=False)
    status = ['--ship', str(ship_folder), 'status']
    state_path = world.folder / 'state.json'

    def check_status(expected):
        before = state_path.read_bytes(), state_path.stat().st_ino, state_path.stat().st_mtime_ns
        for options in ([], ['--line']):
            assert cli.main([*status, *options]) == 0
            output = capsys.readouterr().out
            assert output.count('publisher rollback') == (1 if expected else 0)
            if expected:
                assert expected in output
            assert 'release discovery hold' not in output
        assert cli.main(['--json', *status, '--line']) == 0
        facts = json.loads(capsys.readouterr().out)
        assert facts['release_failures'] == []
        assert facts['release_notices'] == ([expected] if expected else [])
        assert (state_path.read_bytes(), state_path.stat().st_ino, state_path.stat().st_mtime_ns) == before

    world.net.set(LATEST, latest.encode() + b'\n', ETag='"current"')
    assert world.poll() == releases.HOUR
    state = world.state()
    assert (state.result, state.error, state.failures, state.pending) == (result, None, 0, None)
    check_status(note)
    assert world.poll() == releases.HOUR
    check_status(note)

    # Previous releases stored this outcome under one result name for both cases.
    old_state = json.loads(state_path.read_bytes()) | {'result': 'at-or-below-active'}
    state_path.write_text(json.dumps(old_state))
    check_status(note)
    world.advance(releases.HOUR)
    world.net.set(LATEST, status=304)
    assert world.poll() == releases.HOUR
    assert world.net.calls == [(LATEST, {}, 256), (LATEST, {'If-None-Match': '"current"'}, 256)]
    state = world.state()
    assert (state.result, state.error, state.failures, state.pending) == (result, None, 0, None)
    check_status(note)
    assert not list(world.programs.iterdir())

    if note:
        world.advance(releases.HOUR)
        world.net.set(LATEST, b'9.8.6\n')
        world.poll()
        check_status(None)


def test_gpg_preserves_spaces_in_colon_rows(world):
    home = world.runtime / 'key-reading'
    home.mkdir(mode=0o700)
    key = home / 'key.asc'
    key.write_bytes(world.keys['release-author'][1])
    with releases._directory(home) as directory:
        rows = releases._gpg(directory.path, directory.fd, '--with-colons', '--show-keys',
                             str(directory.path / key.name))
    uid, = (row.split(':') for row in rows if row.startswith('uid:'))
    assert uid[9] == 'Release Author'


@pytest.mark.parametrize('first', ['separate-author', 'release-author'])
def test_publisher_keyring_requires_one_primary(world, signer, first):
    _, _, export = signer
    world.net.set(releases.KEY_URL, export(first, 'unrelated-author'))
    world.poll()
    state = world.state()
    assert (state.result, state.error, state.pending) == ('held', releases.Reason.SIGNER, None)
    assert [url for url, _, _ in world.net.calls] == [LATEST, MANIFEST, f'{MANIFEST}.sig', releases.KEY_URL]
    assert not list(world.programs.iterdir())


@pytest.mark.parametrize(('defect', 'reason'), [
    ('different-key', releases.Reason.SIGNER),
    ('different-signer', releases.Reason.SIGNER),
    ('bad-signature', releases.Reason.SIGNER),
    ('absent-signature', releases.Reason.SIGNER),
    ('absent-manifest', releases.Reason.MANIFEST),
    ('bad-manifest', releases.Reason.MANIFEST),
    ('wrong-version', releases.Reason.MANIFEST),
    ('wrong-platform', releases.Reason.MANIFEST),
    ('bad-version-pointer', releases.Reason.MANIFEST),
    ('bad-hash', releases.Reason.DIGEST),
    ('short-binary', releases.Reason.DIGEST),
    ('long-binary', releases.Reason.BOUNDS),
])
def test_invalid_release_never_publishes(world, signer, defect, reason):
    net = world.net
    if defect == 'different-key':
        net.set(releases.KEY_URL, world.keys['separate-author'][1])
        net.manifest(who='separate-author')
    elif defect == 'different-signer':
        net.manifest(who='separate-author')
    elif defect == 'bad-signature':
        net.set(f'{MANIFEST}.sig', b'bad signature')
    elif defect == 'absent-signature':
        net.set(f'{MANIFEST}.sig', status=404)
    elif defect == 'absent-manifest':
        net.set(MANIFEST, status=404)
    elif defect == 'bad-manifest':
        net.set(MANIFEST, b'{"version":')
        net.set(f'{MANIFEST}.sig', signer[1]('release-author', b'{"version":'))
    elif defect == 'wrong-version':
        net.manifest(version='9.8.8')
    elif defect == 'wrong-platform':
        net.manifest(platform='linux-arm64')
    elif defect == 'bad-version-pointer':
        net.set(LATEST, b'9.8.7/9.8.6')
    elif defect == 'bad-hash':
        net.set(BINARY, b'X' * len(ARTIFACT))
    elif defect == 'short-binary':
        net.set(BINARY, ARTIFACT[:-1])
    elif defect == 'long-binary':
        net.set(BINARY, ARTIFACT + b'x')
    world.poll()
    assert world.state().result == 'held'
    assert world.state().error == reason
    assert not list(world.programs.iterdir())
    if defect not in ('bad-hash', 'short-binary', 'long-binary'):
        assert BINARY not in [url for url, _, _ in net.calls]


def test_interrupted_transfer_leaves_no_public_file_and_recovers(world, monkeypatch):
    original = releases.Fetch._get

    def interrupted(fetch, url, target, *args):
        if url == BINARY:
            class Cut:
                def write(self, chunk):
                    target.write(chunk[:len(chunk) // 2])
                    target.flush()
                    os._exit(77)

            return original(fetch, url, Cut(), *args)
        return original(fetch, url, target, *args)

    monkeypatch.setattr(releases.Fetch, '_get', interrupted)
    pid = os.fork()
    if pid == 0:
        try:
            world.poll()
        finally:
            os._exit(78)
    assert os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]) == 77
    assert world.state().result == 'attempting' and world.state().error is None
    scratch = world.programs / releases.STAGE
    assert list(world.programs.iterdir()) == [scratch]
    assert stat.S_IMODE((scratch / 'claude').stat().st_mode) == 0o600
    assert (scratch / 'claude').read_bytes() == ARTIFACT[:len(ARTIFACT) // 2]
    assert world.state().pending is None

    monkeypatch.setattr(releases.Fetch, '_get', original)
    assert world.poll() == releases.HOUR
    assert not scratch.exists()
    assert world.state().result == 'held' and world.state().error == releases.Reason.INTERRUPTED
    assert not world.net.calls
    world.advance(releases.HOUR)
    world.poll()
    assert world.state().result == 'staged'


def test_interruption_after_rename_reuses_verified_candidate(world, monkeypatch):
    publish = releases._publish

    def after_rename(*args):
        publish(*args)
        os._exit(77)

    monkeypatch.setattr(releases, '_publish', after_rename)
    pid = os.fork()
    if pid == 0:
        try:
            world.poll()
        finally:
            os._exit(78)
    assert os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]) == 77
    assert world.state().pending is None
    executable, = world.programs.glob('claude-*/claude')
    inode = executable.stat().st_ino
    assert executable.read_bytes() == ARTIFACT

    monkeypatch.setattr(releases, '_publish', publish)
    world.advance(releases.HOUR)
    world.poll()
    assert world.state().result == 'duplicate'
    assert executable.stat().st_ino == inode
    assert all(url != BINARY for url, _, _ in world.net.calls)


def test_missing_candidate_recovers_on_304(world):
    world.poll()
    file = world.runtime.parent / world.state().pending['path']
    file.parent.chmod(0o700)
    file.unlink()
    file.parent.rmdir()

    world.advance(releases.HOUR)
    world.net.set(LATEST, status=304)
    world.poll()
    assert world.state().result == 'staged'
    assert file.read_bytes() == ARTIFACT


def test_repeated_failure_is_visible_and_backs_off_across_restarts(world):
    original = world.net.replies[LATEST]
    world.net.set(LATEST, status=503)
    assert world.poll() == releases.HOUR
    assert releases.status(world.ship) == ([], [])
    world.advance(releases.HOUR)
    assert world.poll() == 2 * releases.HOUR
    alerts, notices = releases.status(world.ship)
    alert, = alerts
    assert not notices
    assert releases.Reason.HTTP in alert
    assert alert in Facts('ok', 0, 0, 0, release_failures=[alert]).line()
    assert len(world.net.calls) == 2

    world.clock.boot = 'boot-b'
    world.clock.monotonic = 3
    world.advance(releases.HOUR)
    assert world.poll() == releases.HOUR
    assert len(world.net.calls) == 2
    world.advance(releases.HOUR)
    world.net.replies[LATEST] = original
    assert world.poll() == releases.HOUR
    assert releases.status(world.ship) == ([], [])


def test_redirects_stay_on_allowed_https_origin(world):
    mirrored = f'{BASE}/same-origin-latest'
    world.net.set(LATEST, status=302, Location=mirrored)
    world.net.set(mirrored, status=302, Location='https://unrelated.invalid/version')
    world.poll()
    assert [url for url, _, _ in world.net.calls] == [LATEST, mirrored]
    assert world.state().error == releases.Reason.ORIGIN


def test_disk_floor_stops_before_requesting_binary(world, monkeypatch):
    monkeypatch.setattr(releases.write_bound, 'disk_space', lambda path: (100, 100_000))
    world.poll()
    assert world.state().error == releases.Reason.DISK
    assert all(url != BINARY for url, _, _ in world.net.calls)
    assert not list(world.programs.iterdir())


def test_publication_cannot_replace_an_existing_directory(tmp_path):
    prepared, destination = tmp_path / 'prepared', tmp_path / 'existing'
    prepared.mkdir()
    destination.mkdir()
    before = destination.stat().st_ino
    with pytest.raises(releases.Held, match=releases.Reason.EXISTING):
        releases._publish_once(prepared, destination)
    assert destination.stat().st_ino == before
    assert prepared.is_dir()


def test_service_staging_is_opt_in_and_a_pending_fetch_does_not_wait_on_tick(tmp_path, monkeypatch):
    from quarterdeck.service import Service

    assert _from_text('', tmp_path).release_sources == ()
    assert _from_text('[ship]\nrelease_sources = ["claude"]', tmp_path).release_sources == ('claude',)
    with pytest.raises(ShipError, match='release_sources'):
        _from_text('[ship]\nrelease_sources = ["other"]', tmp_path)
    started, release = threading.Event(), threading.Event()

    def poll(runtime, pin):
        started.set()
        release.wait()
        return releases.HOUR

    monkeypatch.setattr(releases, 'poll', poll)
    service = SimpleNamespace(ship=SimpleNamespace(runtime=tmp_path, release_sources=(), program_pins={}))
    Service.stage_program_releases(service)
    assert service.release_schedule.worker is None
    service.ship.release_sources = ('claude',)
    try:
        Service.stage_program_releases(service)
        assert started.wait(10)
        worker = service.release_schedule.worker
        Service.stage_program_releases(service)
        assert service.release_schedule.worker is worker
    finally:
        release.set()
        service.release_schedule.worker.join()
