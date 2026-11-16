"""Service-local stop at a durable delivery boundary; all state is runtime-only."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import uuid

from . import durable
from .procstat import EXITED

BEFORE_PASTE = "prepared-before-paste-v1"
ORDINARY_GRACE = 3.0
DELIVERY_GRACE = 30.0
MAX_JOURNAL_BYTES = 8 * 1024 * 1024
_controller = None


def process_start(pid: int) -> str | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[19] if fields[0] not in EXITED else None
    except (OSError, IndexError, UnicodeError):
        return None


def process_gone(expected: dict) -> bool:
    """An unreadable identity is unknown; without a start, only an exited or absent pid proves gone."""
    try:
        fields = Path(f"/proc/{expected['pid']}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[0] in EXITED or (expected["start"] is not None and fields[19] != expected["start"])
    except (FileNotFoundError, ProcessLookupError):
        return True  # absent, or reaped between the file's open and its read (ESRCH): gone either way
    except (OSError, IndexError, UnicodeError):
        return False


def pointer_path(runtime: Path) -> Path:
    return runtime / "service.delivery"


def _read(path: Path, limit: int):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                return None
            raw = os.read(fd, limit + 1)
            return json.loads(raw), info
        finally:
            os.close(fd)
    except (OSError, ValueError):
        return None


def owner(runtime: Path, pid: int | None = None) -> dict | None:
    from .servicectl import lock_path
    read = _read(lock_path(runtime), 4096)
    if read is None:
        return None
    data, info = read
    if (not isinstance(data, dict) or data.get("version") != 1 or type(data.get("pid")) is not int
            or data["pid"] <= 0 or pid is not None and data["pid"] != pid
            or not isinstance(data.get("start"), str) or not data["start"]
            or not isinstance(data.get("token"), str) or not data["token"]
            or data.get("device") != info.st_dev or data.get("inode") != info.st_ino):
        return None
    return data


def journal_kind(runtime: Path, path: Path) -> str | None:
    try:
        parts = path.relative_to(runtime).parts
    except ValueError:
        return None
    if '..' in parts:
        return None
    if len(parts) == 4 and parts[:2] == ('post', 'pending'):
        return 'pending'
    if len(parts) == 3 and parts[:2] == ('post', 'attempts'):
        return 'attempt'
    if len(parts) == 3 and parts[0] == 'trusted' and parts[2] == 'human-wake.json':  # trusted_files
        return 'human wake'
    return None


def wake_token(data: dict) -> str:
    # Bind the reservation's immutable identity, not its evolving receipt/phase.
    fields = {key: data[key] for key in
              ('format', 'seat', 'identity', 'at', 'since', 'payload', 'attempts', 'generation')}
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def confined(runtime: Path, path: Path, *, missing=False) -> bool:
    try:
        relative = path.relative_to(runtime)
        if journal_kind(runtime, path) is None:
            return False
        current = runtime
        if current.is_symlink():
            return False
        for index, part in enumerate(relative.parts):
            current /= part
            try:
                info = current.lstat()
            except FileNotFoundError:
                return missing and index == len(relative.parts) - 1
            if stat.S_ISLNK(info.st_mode):
                return False
            if index < len(relative.parts) - 1 and not stat.S_ISDIR(info.st_mode):
                return False
        return stat.S_ISREG(info.st_mode)
    except (OSError, ValueError):
        return False


def inflight(runtime: Path, expected: dict | None) -> dict | None:
    read = _read(pointer_path(runtime), 4096)
    if expected is None or read is None:
        return None
    data, _ = read
    if (not isinstance(data, dict) or data.get("version") != 1 or data.get("owner") != expected
            or not isinstance(data.get("token"), str) or not data["token"]
            or type(data.get("before_paste")) is not bool or not isinstance(data.get("journal"), str)):
        return None
    relative = Path(data["journal"])
    if (relative.is_absolute() or not confined(runtime, runtime / relative)
            or data.get('kind', 'pending') != journal_kind(runtime, runtime / relative)):
        return None
    if data.get('kind') == 'attempt' and not data['before_paste']:
        return None
    return data


def never_pasted(data: dict) -> bool:
    return (data.get("paste_boundary") == BEFORE_PASTE and data.get("phase") == "prepared"
            and data.get("shown") is None and not data.get("paste_clear") and not data.get("recovery"))


def finish(runtime: Path, expected: dict | None, *, forced: bool | None) -> str:
    """Called only with the original lock held nonblocking after its service is gone."""
    if not pointer_path(runtime).exists():
        return ""
    if (expected is None or owner(runtime) != expected
            or not process_gone(expected)):
        return "delivery owner unknown or still alive; journal left unchanged"
    data = inflight(runtime, expected)
    if data is None:
        return "delivery boundary unknown; journal left unchanged"
    path = runtime / data["journal"]
    read = _read(path, MAX_JOURNAL_BYTES)
    if read is None:
        return "delivery journal unreadable or too large for bounded stop annotation; left unchanged"
    journal, _ = read
    if data.get('kind') == 'attempt':
        # This grants stop grace, never a delivery receipt or permission to resend.
        # A process crash here leaves the ordinary unjournaled-attempt rules in charge.
        from .post import Attempt
        try:
            Attempt(**journal, path=path)
        except (TypeError, ValueError):
            return "delivery attempt unreadable; left unchanged"
        pointer_path(runtime).unlink()
        return "delivery stopped before journal; attempt retained for receipt reconciliation"
    wake = data.get('kind') == 'human wake'
    if not isinstance(journal, dict):
        return "delivery journal identity unknown; left unchanged"
    from .pending import Proof, validate

    try:
        if wake:
            from .human_wake import validate as validate_wake
            validate_wake(journal, path.parent.name)
            matched = wake_token(journal) == data['token']
        else:
            validate(journal, path.parent.name)
            matched = journal.get('service_owner') == expected and journal.get('delivery_token') == data['token']
        if not matched:
            return "delivery journal identity unknown; left unchanged"
    except (ValueError, TypeError, KeyError):
        return "delivery journal boundary unknown; left unchanged"
    phase = journal["phase"]
    fields = {}
    if wake and phase == 'prepared':
        # The wake owns its reservation and already proves prepared means unsent.
        # Keep it byte-for-byte for the wake's existing next-pass settlement.
        pointer_path(runtime).unlink()
        return "human wake stopped before paste; reservation unchanged and unsent"
    if never_pasted(journal):
        fields.update(phase="cancelled", resolution="service stopped before paste; never typed")
        detail = "delivery cancelled before paste"
    elif phase in ("submitted", "queued", "delivered", "cancelled", "withheld"):
        detail = f"delivery reached {phase}"
    else:
        detail = f"delivery {'cut' if forced else 'unresolved'} at {phase}; outcome uncertain; never resent"
    fields["service_stop"] = dict(kind="interrupted" if forced is None else "forced" if forced else "stopped", phase=phase,
                                  at=datetime.now(timezone.utc).isoformat(), detail=detail)
    if not confined(runtime, path):
        return "delivery journal path changed; left unchanged"
    Proof(path, journal).save(**fields)
    pointer_path(runtime).unlink()
    return detail


def annotate(runtime: Path, expected: dict | None, *, forced: bool) -> str:
    """One nonblocking claim of the original inode; never wait for a new owner."""
    if expected is None:
        return "delivery owner unknown; journal left unchanged" if pointer_path(runtime).exists() else ""
    try:
        from .servicectl import lock_path
        fd = os.open(lock_path(runtime), os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (expected["device"], expected["inode"]):
                return "delivery lock identity changed; journal left unchanged"
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return "delivery lock has another owner; journal left unchanged"
            detail = finish(runtime, expected, forced=forced)
            if owner(runtime) == expected and process_gone(expected):
                from .servicectl import read_heartbeat, remove_heartbeat
                beat = read_heartbeat(runtime)
                if beat is not None and beat[0] == expected["pid"]:
                    remove_heartbeat(runtime)
            return detail
        finally:
            os.close(fd)
    except OSError:
        return "delivery stop annotation unavailable; journal left unchanged"


class Stop:
    def __init__(self, runtime: Path, lock=None):
        self.runtime, self.pid, self.lock = runtime, os.getpid(), lock
        self.requested = False
        self.delivering = False
        self.relaunching = False  # inside a relaunch section (relaunch_intent.section): held like a delivery
        self.owner = None

    def signal(self, *_):
        self.requested = True
        if not self.delivering and not self.relaunching:
            raise SystemExit(0)

    @contextmanager
    def installed(self):
        global _controller
        from .servicectl import acquire_lock

        previous = _controller
        old_handler = signal.getsignal(signal.SIGTERM)
        own_lock = self.lock is None
        if own_lock:
            self.lock = acquire_lock(self.runtime)
        try:
            old_owner = owner(self.runtime)
            if pointer_path(self.runtime).exists():
                detail = finish(self.runtime, old_owner, forced=None)
                if pointer_path(self.runtime).exists():
                    raise RuntimeError(f"previous delivery boundary unresolved: {detail}")
            info = os.fstat(self.lock.fileno())
            start = process_start(self.pid)
            if start is None:
                raise RuntimeError("service process start identity unavailable")
            self.owner = dict(version=1, pid=self.pid, start=start, token=uuid.uuid4().hex,
                              device=info.st_dev, inode=info.st_ino)
            self.lock.seek(0)
            self.lock.truncate()
            self.lock.write(json.dumps(self.owner) + "\n")
            self.lock.flush()
            _controller = self
            signal.signal(signal.SIGTERM, self.signal)
            yield
        finally:
            signal.signal(signal.SIGTERM, old_handler)
            _controller = previous
            if own_lock:
                self.lock.close()

    def reserve(self) -> dict:
        return dict(service_owner=self.owner, delivery_token=uuid.uuid4().hex)

    def opened(self, attempt):
        if journal_kind(self.runtime, attempt.path) != 'attempt' or not confined(self.runtime, attempt.path):
            raise RuntimeError("delivery attempt is not a confined regular runtime file")
        data = dict(version=1, owner=self.owner, token=uuid.uuid4().hex, kind='attempt',
                    before_paste=True, journal=attempt.path.relative_to(self.runtime).as_posix())
        durable.replace_file(pointer_path(self.runtime), json.dumps(data) + "\n")

    def publish(self, proof):
        if not confined(self.runtime, proof.path):
            raise RuntimeError("delivery journal is not a confined regular runtime file")
        kind = journal_kind(self.runtime, proof.path)
        if kind == 'human wake':
            from .human_wake import validate
            validate(proof.data, proof.path.parent.name)
            token = wake_token(proof.data)
            before = proof.data['phase'] == 'prepared' and proof.data['shown'] is None
        else:
            token = proof.data['delivery_token']
            before = proof.data.get('paste_boundary') == BEFORE_PASTE
        data = dict(version=1, owner=self.owner, token=token, kind=kind,
                    before_paste=before, journal=proof.path.relative_to(self.runtime).as_posix())
        durable.replace_file(pointer_path(self.runtime), json.dumps(data) + "\n")

    @contextmanager
    def delivery(self, proof):
        previous = self.delivering
        self.delivering = True
        path = pointer_path(self.runtime)
        try:
            active = inflight(self.runtime, self.owner)
            wake = journal_kind(self.runtime, proof.path) == 'human wake'
            token = wake_token(proof.data) if wake else proof.data.get('delivery_token')
            if (active is None or active['journal'] != proof.path.relative_to(self.runtime).as_posix()
                    or active['token'] != token):
                if not wake:
                    proof.save(**self.reserve())
                self.publish(proof)
            yield
        finally:
            try:
                if self.requested:
                    if never_pasted(proof.data):
                        proof.save(phase="cancelled", resolution="service stopped before paste; never typed")
                    # Leave the pointer for the exclusive stopper or next owner.
                else:
                    path.unlink(missing_ok=True)
            finally:
                self.delivering = previous
                if self.requested:
                    raise SystemExit(0)


def prepare(proof) -> dict:
    return _controller.reserve() if _controller is not None else {}


def prepared(proof):
    if _controller is not None:
        _controller.publish(proof)


def opened(runtime, attempt):
    if _controller is not None and _controller.runtime == runtime and _controller.delivering:
        _controller.opened(attempt)


def requested() -> bool:
    return _controller is not None and _controller.requested


@contextmanager
def transaction_scope(service):
    """Defer from the first delivery write, after interruptible preflight reads."""
    owner = _controller
    if owner is None or owner.runtime != service.runtime:
        yield
        return
    previous = owner.delivering
    if owner.requested and not previous:
        raise SystemExit(0)
    owner.delivering = True
    try:
        yield
    finally:
        owner.delivering = previous
        if not previous and not owner.requested:
            active = inflight(owner.runtime, owner.owner)
            if active is not None and active.get('kind') == 'attempt':
                pointer_path(owner.runtime).unlink(missing_ok=True)
        if owner.requested and not previous:
            raise SystemExit(0)


def transaction(method):
    @wraps(method)
    def wrapped(service, *args, **kwargs):
        with transaction_scope(service):
            return method(service, *args, **kwargs)
    return wrapped


@contextmanager
def delivery(proof):
    if _controller is None or proof is None:
        yield
    else:
        with _controller.delivery(proof):
            yield
