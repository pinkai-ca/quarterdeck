"""Discovery and staging of the native Claude Code executable, without running it."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
import fcntl
import hashlib
from email.message import Message
from http.client import HTTPException, parse_headers
from io import BytesIO
import json
import math
import os
from pathlib import Path
import platform
import re
from selectors import DefaultSelector, EVENT_READ
import shutil
import stat
import subprocess
import threading
import time
from urllib.parse import urljoin, urlsplit

from .. import post_dir, write_bound

SOURCE = 'claude'
ORIGIN = 'https://downloads.claude.ai'
RELEASES = f'{ORIGIN}/claude-code-releases'
KEY_URL = f'{ORIGIN}/keys/claude-code.asc'
FINGERPRINT = '31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE'

HOUR = 3600
MAX_BACKOFF = 24 * HOUR
ATTEMPT_SECONDS = 300
METADATA_BYTES = 64 * 1024
PROGRAM_BYTES = 1024 ** 3
BLOCK = 1024 ** 2
VERSION = re.compile(r'(?:0|[1-9][0-9]{0,5})(?:\.(?:0|[1-9][0-9]{0,5})){2}')
DIGEST = re.compile(r'[0-9a-f]{64}')
PLATFORMS = ('linux-x64', 'linux-arm64')
RELEASE_DIRECTORY = re.compile(r'claude-(' + VERSION.pattern + r')-linux-(?:x64|arm64)-[0-9a-f]{64}')
FILE_READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
STAGE = '.claude-stage'


class Reason(StrEnum):
    ACTIVE = 'active pin version is unknown'
    PLATFORM = 'native host platform is unsupported'
    ORIGIN = 'publisher redirect left the allowed origin'
    HTTP = 'publisher request failed'
    BOUNDS = 'publisher response exceeded a bound'
    MANIFEST = 'signed manifest has the wrong version, platform or shape'
    SIGNER = 'publisher key or manifest signature did not verify'
    DIGEST = 'executable length or SHA-256 did not match'
    DISK = 'insufficient disk headroom'
    STORAGE = 'release storage or a required system tool was unavailable'
    EXISTING = 'existing release directory did not verify'
    INTERRUPTED = 'release attempt interrupted'


class Held(Exception):
    def __init__(self, reason: Reason):
        self.reason = reason
        super().__init__(reason.value)


def _version(value: object) -> bool:
    return isinstance(value, str) and VERSION.fullmatch(value) is not None


def _version_parts(version: str) -> tuple[int, ...]:
    return tuple(map(int, version.split('.')))


def _digest(value: object) -> bool:
    return isinstance(value, str) and DIGEST.fullmatch(value) is not None


def _size(value: object) -> bool:
    return type(value) is int and 0 < value <= PROGRAM_BYTES


def _unique(pairs):
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError('duplicate fields')
    return fields


@dataclass(frozen=True)
class Candidate:
    version: str
    platform: str
    sha256: str
    size: int
    manifest_sha256: str

    def __post_init__(self):
        if (not _version(self.version) or self.platform not in PLATFORMS or not _digest(self.sha256)
                or not _size(self.size) or not _digest(self.manifest_sha256)):
            raise ValueError('invalid release candidate')

    @property
    def directory(self) -> str:
        return f'claude-{self.version}-{self.platform}-{self.sha256}'

    @property
    def path(self) -> str:
        return f'runtime/programs/{self.directory}/claude'

    @property
    def url(self) -> str:
        return f'{RELEASES}/{self.version}/{self.platform}/claude'

    def receipt(self) -> dict:
        return {**asdict(self), 'path': self.path, 'origin': self.url, 'signing_fingerprint': FINGERPRINT}


@dataclass
class State:
    due_wall: float = 0
    due_monotonic: float = 0
    boot: str = ''
    failures: int = 0
    result: str = ''
    error: str | None = None
    version: str | None = None
    etag: str | None = None
    modified: str | None = None
    pending: dict | None = None

    def __post_init__(self):
        if (any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
                for value in (self.due_wall, self.due_monotonic))
                or not isinstance(self.boot, str) or type(self.failures) is not int or not 0 <= self.failures <= 6
                or self.result not in ('', 'attempting', 'held', 'at-or-below-active', 'at-active',
                                       'publisher-rollback', 'unchanged', 'duplicate', 'staged')
                or self.error not in (None, *Reason) or self.version is not None and not _version(self.version)
                or any(value is not None and not _header(value) for value in (self.etag, self.modified))):
            raise ValueError('invalid release state')

    def wait(self, now: tuple[float, float, str]) -> float:
        wall, monotonic, boot = now
        if boot and self.boot == boot:
            return max(0, self.due_monotonic - monotonic)
        return max(0, self.due_wall - wall)

    def reserve(self, now: tuple[float, float, str]) -> State:
        wall, monotonic, boot = now
        delay = min(HOUR * 2 ** self.failures, MAX_BACKOFF)
        return replace(self, due_wall=wall + delay, due_monotonic=monotonic + delay, boot=boot,
                       failures=min(self.failures + 1, 6), result='attempting')

    def candidate(self) -> Candidate | None:
        if self.pending is None:
            return None
        row = self.pending
        candidate = Candidate(row['version'], row['platform'], row['sha256'], row['size'], row['manifest_sha256'])
        if row != candidate.receipt():
            raise ValueError('invalid candidate provenance')
        return candidate


def _clock() -> tuple[float, float, str]:
    try:
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:
        boot = ''
    return time.time(), time.monotonic(), boot


@dataclass(frozen=True)
class Directory:
    fd: int

    @property
    def path(self) -> Path:
        return Path('/proc/self/fd') / str(self.fd)


@contextmanager
def _directory(path: Path, mode: int | None = None, *, create_in: Directory | None = None):
    if create_in is not None:
        try:
            path.mkdir(mode=mode)
        except FileExistsError:
            pass
        else:
            os.fsync(create_in.fd)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or mode is not None and stat.S_IMODE(info.st_mode) != mode:
            raise Held(Reason.STORAGE)
        yield Directory(fd)
    finally:
        os.close(fd)


def _owned_file(path: Path, flags: int, mode: int) -> int:
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != mode:
        os.close(fd)
        raise Held(Reason.STORAGE)
    return fd


@contextmanager
def _state_folder(root: Directory, *, create: bool = False):
    with _directory(root.path / 'program-releases', 0o700, create_in=root if create else None) as parent:
        with _directory(parent.path / SOURCE, 0o700, create_in=parent if create else None) as folder:
            yield folder


def _read_state(folder: Path) -> State:
    try:
        fd = _owned_file(folder / 'state.json', FILE_READ, 0o600)
    except FileNotFoundError:
        return State()
    with os.fdopen(fd, 'rb') as stream:
        raw = stream.read(METADATA_BYTES + 1)
    if len(raw) > METADATA_BYTES:
        raise Held(Reason.STORAGE)
    try:
        return State(**json.loads(raw, object_pairs_hook=_unique))
    except (TypeError, ValueError, RecursionError):
        raise Held(Reason.STORAGE) from None


def _write_state(folder: Directory, state: State) -> None:
    temporary = folder.path / '.state.tmp'
    temporary.unlink(missing_ok=True)
    fd = _owned_file(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='ascii') as stream:
        json.dump(asdict(state), stream, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(folder.path / 'state.json')
    os.fsync(folder.fd)


def _audit(folder: Path, **receipt) -> None:
    fd = _owned_file(folder / 'requests.jsonl', os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, 'a', encoding='ascii') as stream:
        stream.write(json.dumps(receipt) + '\n')


def _header(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 256 and all(' ' <= char <= '~' for char in value)


def _allowed(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return (f'{parts.scheme}://{parts.netloc}' == ORIGIN and not parts.query and not parts.fragment
            and re.fullmatch(r'/[A-Za-z0-9._/-]{1,255}', parts.path) is not None)


def _tool(name: str) -> str:
    command = shutil.which(name, path=os.defpath)
    if command is None:
        raise Held(Reason.STORAGE)
    return command


def _env(home: Path) -> dict:
    return {'HOME': str(home), 'LC_ALL': 'C', 'PATH': os.defpath}


def _response(raw: bytes) -> tuple[int | None, Message]:
    # An interim HTTP response may precede the final header block.
    for block in reversed(raw.split(b'\r\n\r\n')):
        start, _, headers = block.partition(b'\r\n')
        if match := re.fullmatch(rb'HTTP/1\.[01] ([1-5][0-9]{2})(?: .*)?', start):
            try:
                return int(match[1]), parse_headers(BytesIO(headers + b'\r\n\r\n'))
            except HTTPException:
                raise Held(Reason.BOUNDS) from None
    return None, parse_headers(BytesIO())


@contextmanager
def _curl(stage: Directory, url: str, request_headers: dict, limit: int, seconds: float):
    command = [_tool('curl'), '--disable', '--noproxy', '*', '--proto', '=https',
               '--http1.1', '--silent', '--connect-timeout', '15', '--max-time', f'{seconds:.3f}',
               '--max-filesize', str(limit), '--output', '-']
    for name, value in request_headers.items():
        command += ['--header', f'{name}: {value}']
    reader, writer = os.pipe()
    try:
        try:
            process = subprocess.Popen([*command, '--dump-header', f'/proc/self/fd/{writer}', '--', url],
                                       env=_env(stage.path), pass_fds=(stage.fd, writer),
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        finally:
            os.close(writer)
        with process:
            try:
                yield process, reader
            except BaseException:
                process.kill()
                raise
    finally:
        os.close(reader)


@dataclass(frozen=True)
class Response:
    status: int
    headers: Message
    received: int
    sha256: str


class Fetch:
    def __init__(self, folder: Path, stage: Directory):
        self.folder, self.stage = folder, stage
        self.deadline = time.monotonic() + ATTEMPT_SECONDS

    def get(self, url: str, target, limit: int, *, headers=None, expected: int | None = None,
            missing: Reason = Reason.HTTP) -> Response:
        for _ in range(4):
            if not _allowed(url):
                raise Held(Reason.ORIGIN)
            response = self._get(url, target, limit, headers or {}, expected)
            if response.status == 404:
                raise Held(missing)
            if response.status not in (301, 302, 303, 307, 308):
                if response.status not in (200, 304) or response.status == 304 and not headers:
                    raise Held(Reason.HTTP)
                return response
            location = response.headers.get('Location', '')
            if not _header(location):
                raise Held(Reason.ORIGIN)
            try:
                url = urljoin(url, location)
            except ValueError:
                raise Held(Reason.ORIGIN) from None
            target.seek(0)
            target.truncate()
        raise Held(Reason.HTTP)

    def _get(self, url: str, target, limit: int, headers: dict, expected: int | None) -> Response:
        seconds = self.deadline - time.monotonic()
        if seconds < 0.01:
            raise Held(Reason.BOUNDS)
        raw = bytearray()
        count, digest = 0, hashlib.sha256()
        status = code = None
        try:
            try:
                with _curl(self.stage, url, headers, limit, seconds) as (process, header_fd):
                    # Drain both pipes so curl can keep writing either stream. One extra
                    # header byte distinguishes a full budget from an oversized response.
                    with DefaultSelector() as streams:
                        streams.register(header_fd, EVENT_READ)
                        streams.register(process.stdout, EVENT_READ)
                        while streams.get_map():
                            remaining = self.deadline - time.monotonic()
                            if remaining <= 0 or not (ready := streams.select(remaining)):
                                raise Held(Reason.BOUNDS)
                            for stream, _ in ready:
                                if stream.fd == header_fd:
                                    room = METADATA_BYTES - len(raw)
                                    chunk = os.read(header_fd, room + 1)
                                    if len(chunk) > room:
                                        raw.clear()  # An incomplete response has no final status to audit.
                                        raise Held(Reason.BOUNDS)
                                    raw.extend(chunk)
                                else:
                                    chunk = os.read(stream.fd, BLOCK)
                                    count += len(chunk)
                                    digest.update(chunk)
                                    if count > limit or time.monotonic() > self.deadline:
                                        raise Held(Reason.BOUNDS)
                                    if chunk:
                                        target.write(chunk)
                                if not chunk:
                                    streams.unregister(stream.fd)
                    code = process.wait(timeout=max(0, self.deadline - time.monotonic()))
            finally:
                status, reply = _response(bytes(raw))
            if code in (28, 63):
                raise Held(Reason.BOUNDS)
            if code or status is None:
                raise Held(Reason.HTTP)
            if status == 200 and expected is not None:
                lengths = reply.get_all('Content-Length', ())
                if count != expected or lengths and lengths != [str(expected)]:
                    raise Held(Reason.DIGEST)
            return Response(status, reply, count, digest.hexdigest())
        except subprocess.TimeoutExpired:
            raise Held(Reason.BOUNDS) from None
        finally:
            _audit(self.folder, at=time.time(), origin=url, status=status, transport_exit=code,
                   bytes=count, sha256=digest.hexdigest())

    def metadata(self, url: str, *, limit: int = METADATA_BYTES, headers=None,
                 missing: Reason = Reason.HTTP) -> tuple[Response, bytes]:
        target = BytesIO()
        response = self.get(url, target, limit, headers=headers, missing=missing)
        return response, target.getvalue()


def _gpg(home: Path, stage_fd: int, *args: str) -> list[str]:
    command = [_tool('gpg'), '--no-options', '--homedir', str(home), '--batch', '--no-tty',
               '--disable-dirmngr', '--no-autostart', '--no-auto-key-retrieve', '--no-auto-key-locate',
               '--no-auto-key-import', '--no-random-seed-file', *args]
    try:
        result = subprocess.run(command, env=_env(home), pass_fds=(stage_fd,), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=15, check=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise Held(Reason.SIGNER) from None
    return result.stdout.decode('ascii', 'replace').split('\n')


def _verify(stage: Directory, key: bytes, manifest: bytes, signature: bytes) -> None:
    home = stage.path / 'openpgp'
    home.mkdir(mode=0o700)
    key_path, manifest_path, signature_path = (home / name for name in ('key.asc', 'manifest.json', 'manifest.sig'))
    for path, data in ((key_path, key), (manifest_path, manifest), (signature_path, signature)):
        path.write_bytes(data)
    # Fingerprints are emitted immediately after the corresponding public key record. The
    # isolated keyring accepts exactly one public primary key, independently of its user ID.
    rows = [line.split(':') for line in _gpg(home, stage.fd, '--with-colons', '--show-keys', str(key_path)) if line]
    primary = [index for index, row in enumerate(rows) if row[0] == 'pub']
    if len(primary) != 1 or any(row[0] in ('sec', 'ssb') for row in rows):
        raise Held(Reason.SIGNER)
    fingerprint = rows[primary[0] + 1] if primary[0] + 1 < len(rows) else []
    if fingerprint[:1] != ['fpr'] or fingerprint[9:10] != [FINGERPRINT]:
        raise Held(Reason.SIGNER)
    _gpg(home, stage.fd, '--import', str(key_path))
    lines = _gpg(home, stage.fd, '--status-fd', '1', '--verify', str(signature_path), str(manifest_path))
    statuses = [row[1:] for line in lines if (row := line.split())[:1] == ['[GNUPG:]']]
    valid = [row for row in statuses if row[:1] == ['VALIDSIG']]
    if (len(valid) != 1 or len(valid[0]) != 11 or valid[0][1] != FINGERPRINT or valid[0][-1] != FINGERPRINT
            or not any(row[:1] == ['GOODSIG'] for row in statuses)
            or any(row and row[0] in ('EXPSIG', 'EXPKEYSIG', 'REVKEYSIG', 'BADSIG', 'ERRSIG') for row in statuses)):
        raise Held(Reason.SIGNER)


def _manifest(raw: bytes, version: str, native: str, manifest_sha256: str) -> Candidate:
    try:
        data = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique)
        entry = data['platforms'][native]
        if data['version'] != version or entry['binary'] != 'claude':
            raise ValueError('manifest does not name the requested artifact')
        return Candidate(version, native, entry['checksum'], entry['size'], manifest_sha256)
    except (KeyError, TypeError, ValueError, RecursionError):
        raise Held(Reason.MANIFEST) from None


def _active_version(pin) -> str:
    if pin is not None:
        # Baseline managed pins use claude-VERSION. A later promotion may name the
        # verified executable under its release directory.
        name = pin.path.name
        if name == 'claude' and (release := RELEASE_DIRECTORY.fullmatch(pin.path.parent.name)):
            return release[1]
        version = name.removeprefix('claude-')
        if name.startswith('claude-') and _version(version):
            return version
    raise Held(Reason.ACTIVE)


def _platform() -> str:
    arch = {'x86_64': 'x64', 'aarch64': 'arm64'}.get(platform.machine())
    try:
        libc = os.confstr('CS_GNU_LIBC_VERSION')
    except (OSError, ValueError):
        libc = None
    if platform.system() == 'Linux' and arch is not None and libc:
        return f'linux-{arch}'
    raise Held(Reason.PLATFORM)


def _installed(programs: Directory, candidate: Candidate) -> bool:
    path = programs.path / candidate.directory
    try:
        with _directory(path, 0o500) as release:
            try:
                fd = _owned_file(release.path / 'claude', FILE_READ, 0o500)
                with os.fdopen(fd, 'rb') as stream:
                    info = os.fstat(fd)
                    if (info.st_size == candidate.size
                            and hashlib.file_digest(stream, 'sha256').hexdigest() == candidate.sha256):
                        return True
            except (OSError, Held):
                pass
            raise Held(Reason.EXISTING)
    except FileNotFoundError:
        return False
    except (OSError, Held):
        raise Held(Reason.EXISTING) from None


def _clean_stage(stage: Path) -> None:
    try:
        with _directory(stage) as pocket:
            if stat.S_IMODE(os.fstat(pocket.fd).st_mode) not in (0o500, 0o700):
                raise Held(Reason.STORAGE)
            # A stop between chmod and rename can leave the staging directory read-only.
            os.fchmod(pocket.fd, 0o700)
    except FileNotFoundError:
        return
    shutil.rmtree(stage)


@contextmanager
def _staging(programs: Directory):
    path = programs.path / STAGE
    _clean_stage(path)  # only this source's staging name, while holding its lock
    path.mkdir(mode=0o700)
    try:
        with _directory(path, 0o700) as stage:
            yield stage
    finally:
        _clean_stage(path)


def _publish_once(prepared: Path, destination: Path) -> None:
    try:
        at_cwd = -100  # Linux AT_FDCWD. Both paths are absolute.
        post_dir._rename_noreplace(prepared, destination, at_cwd, at_cwd)
    except FileExistsError:
        raise Held(Reason.EXISTING) from None


def _publish(programs: Directory, stage: Directory, fetch: Fetch, candidate: Candidate) -> None:
    free, total = write_bound.disk_space(str(programs.path))
    if free - candidate.size < write_bound.floor(total):
        raise Held(Reason.DISK)
    fd = _owned_file(stage.path / 'claude', os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w+b') as binary:
        response = fetch.get(candidate.url, binary, candidate.size, expected=candidate.size)
        if response.sha256 != candidate.sha256:
            raise Held(Reason.DIGEST)
        binary.flush()
        os.fchmod(fd, 0o500)
        os.fsync(fd)
        binary.seek(0)
        if hashlib.file_digest(binary, 'sha256').hexdigest() != candidate.sha256:
            raise Held(Reason.DIGEST)
    free, total = write_bound.disk_space(str(programs.path))
    if free < write_bound.floor(total):
        raise Held(Reason.DISK)
    shutil.rmtree(stage.path / 'fetch')  # publish only the executable
    os.fchmod(stage.fd, 0o500)
    os.fsync(stage.fd)
    _publish_once(programs.path / STAGE, programs.path / candidate.directory)
    os.fsync(programs.fd)


def _discover(folder: Path, programs: Directory, pin, state: State) -> State:
    active, native = _active_version(pin), _platform()
    previous = state.candidate()
    validators = {name: value for name, value in (('If-None-Match', state.etag),
                                                  ('If-Modified-Since', state.modified)) if value}
    with _staging(programs) as stage, _directory(stage.path / 'fetch', 0o700, create_in=stage) as scratch:
        fetch = Fetch(folder, scratch)
        response, raw = fetch.metadata(f'{RELEASES}/latest', headers=validators, limit=256)
        unchanged = response.status == 304
        version = state.version if unchanged else raw.decode('ascii', 'replace').strip()
        if not _version(version):
            raise Held(Reason.MANIFEST)
        candidate = None
        if version == active:
            result = 'at-active'
        elif _version_parts(version) < _version_parts(active):
            result = 'publisher-rollback'
        elif (unchanged and previous is not None and previous.version == version and previous.platform == native
              and _installed(programs, previous)):
            candidate, result = previous, 'unchanged'
        else:
            url = f'{RELEASES}/{version}/manifest.json'
            manifest_response, manifest = fetch.metadata(url, missing=Reason.MANIFEST)
            _, signature = fetch.metadata(f'{url}.sig', missing=Reason.SIGNER)
            _, key = fetch.metadata(KEY_URL, missing=Reason.SIGNER)
            _verify(scratch, key, manifest, signature)
            candidate = _manifest(manifest, version, native, manifest_response.sha256)
            result = 'duplicate' if _installed(programs, candidate) else 'staged'
            if result == 'staged':
                _publish(programs, stage, fetch, candidate)
        etag, modified = response.headers.get('ETag'), response.headers.get('Last-Modified')
        return replace(state, version=version, failures=0, result=result, error=None,
                       etag=etag if _header(etag) else state.etag if unchanged else None,
                       modified=modified if _header(modified) else state.modified if unchanged else None,
                       pending=candidate.receipt() if candidate is not None else None)


def poll(runtime: Path, pin, *, clock=_clock) -> float:
    """Run one due attempt under the source lock. Return seconds until another tick may schedule it."""
    with _directory(runtime) as root, _state_folder(root, create=True) as folder:
        lock = _owned_file(folder.path / 'poll.lock', os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return HOUR
            return _poll(root, folder, pin, clock)
        finally:
            os.close(lock)


def _poll(root: Directory, folder: Directory, pin, clock) -> float:
    state, now = _read_state(folder.path), clock()
    if delay := state.wait(now):
        if state.result == 'attempting':
            try:
                with _directory(root.path / 'programs', 0o700) as programs:
                    _clean_stage(programs.path / STAGE)
            except FileNotFoundError:
                pass
            _write_state(folder, replace(state, result='held', error=Reason.INTERRUPTED))
        return delay
    reserved = state.reserve(now)
    _write_state(folder, reserved)  # the reservation survives death before the first request
    try:
        with _directory(root.path / 'programs', 0o700, create_in=root) as programs:
            success = _discover(folder.path, programs, pin, state)
    except Held as error:
        _write_state(folder, replace(reserved, result='held', error=error.reason))
    except (OSError, ValueError, KeyError, TypeError):
        _write_state(folder, replace(reserved, result='held', error=Reason.STORAGE))
    else:
        # A success clears backoff, even if the last attempt took longer than an hour to retry.
        _write_state(folder, replace(success, due_wall=now[0] + HOUR, due_monotonic=now[1] + HOUR, boot=now[2]))
        return HOUR
    return reserved.wait(clock())


def status(ship) -> tuple[list[str], list[str]]:
    """Return discovery holds and informational notices from one read of the last observation."""
    if SOURCE not in getattr(ship, 'release_sources', ()):
        return [], []
    try:
        with _directory(ship.runtime) as root, _state_folder(root) as folder:
            state = _read_state(folder.path)
    except FileNotFoundError:
        return [], []
    except (Held, OSError):
        return [f'{SOURCE} release discovery held ({Reason.STORAGE})'], []
    finished_failures = state.failures - (state.result == 'attempting')
    if state.error and (finished_failures >= 2 or state.error in (
            Reason.ACTIVE, Reason.PLATFORM, Reason.ORIGIN, Reason.SIGNER, Reason.MANIFEST, Reason.DIGEST, Reason.EXISTING)):
        return [f'{SOURCE} release discovery held ({state.error})'], []
    if state.version is not None:
        try:
            active = _active_version(ship.program_pins.get(SOURCE))
        except Held:
            return [], []
        if _version_parts(state.version) < _version_parts(active):
            return [], [f'{SOURCE} publisher rollback: latest {state.version}, active {active}']
    return [], []


class Schedule:
    """The service tick starts at most one background job. It performs no network or disk I/O."""

    def __init__(self):
        self.worker = None
        self.next_due = 0.0

    def tick(self, ship) -> None:
        if SOURCE not in ship.release_sources or self.worker is not None and self.worker.is_alive():
            return
        if time.monotonic() < self.next_due:
            return
        self.worker = threading.Thread(target=self._work, args=(ship.runtime, ship.program_pins.get(SOURCE)),
                                       name='program-release-discovery', daemon=True)
        self.worker.start()

    def _work(self, runtime: Path, pin) -> None:
        try:
            delay = poll(runtime, pin)
        except (Held, OSError):
            delay = HOUR
        self.next_due = time.monotonic() + delay
