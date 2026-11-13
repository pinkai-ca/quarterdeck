"""Ship-owned code at one real live path, with one backup and durable recovery.

Reads select only recorded usable code. Sync/back/up own every mutation under the
install lock; a backup is never executed until restored to the canonical live path.
"""

from __future__ import annotations

import io
import ast
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
from uuid import uuid4
from datetime import datetime, timezone
from pathlib import Path

from . import copy_state, durable, resources, servicectl
from .ship import Ship, ShipError, fence
from .tmux import Tmux

RECORD = copy_state.RECORD
# Targets declare their own support. Never insert this into a historical target.
COPY_SAFETY = "real-directory-v1"
# Public failure categories are fixed; exception/provider text is never a cause.
FAILURE_CAUSES = frozenset({
    'source archive unavailable', 'resource declaration invalid',
    'service could not be stopped', 'brief snapshot failed',
    'previous copy could not be saved', 'copy swap failed',
    'environment could not be made', 'package build failed',
    'resource synchronization failed', 'proof command failed',
    'copy activation failed', 'copy cleanup failed',
})
PHASE_CAUSES = {
    'stopping': 'service could not be stopped', 'snapshotting': 'brief snapshot failed',
    'backing_up': 'previous copy could not be saved', 'backup_ready': 'previous copy could not be saved',
    'swapping': 'copy swap failed', 'resources': 'resource synchronization failed',
    'proving': 'proof command failed', 'target_ready': 'copy activation failed',
    'activated': 'copy cleanup failed', 'cleanup': 'copy cleanup failed',
}
FAILURE_STEPS = frozenset(PHASE_CAUSES) | {'preparing', 'installing', 'origin', '--version', 'laws', '--help'}
LOADED_COPY = copy_state.loaded_identity(__file__, sys.prefix)
# The directory roles under runtime/: the live copy, the copy before it, back's swap, and sync's backup of
# the live copy, which becomes the copy before it only once the new copy is active.
ROLES = ('venv', 'venv.prev', 'venv.swap', 'venv.backup')
BACKUP = 'venv.backup'


def launch_stamp(path: Path) -> str | None:
    """Bind the complete saved launch without copying its command/brief into requests."""
    if not path.exists():
        return None
    data = json.dumps(json.loads(path.read_text()), sort_keys=True)
    return hashlib.sha256(data.encode()).hexdigest()


def brief_snapshot(ship: Ship) -> dict:
    """Capture the existing offered brief and complete launch before installation/rendering."""
    before, launches = {}, {}
    for name in ship.seats:
        brief = ship.runtime / 'brief' / f'{name}.md'
        launch = ship.runtime / 'seats' / name / 'launch.json'
        before[name] = hashlib.sha256(brief.read_bytes()).hexdigest() if brief.exists() else None
        launches[name] = launch_stamp(launch)
    return dict(before=before, launches=launches)


def queue_rebrief(ship: Ship, commit: str, previous: dict | None, snapshot: dict) -> tuple[Path, dict]:
    """Reuse incomplete work on the current commit; another sync is a new event."""
    folder = ship.runtime / 'requests'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f'upgrade-rebrief-{commit}.json'
    if previous and previous['commit'] == commit and path.exists():
        data = json.loads(path.read_text())
    else:
        data = dict(kind='rebrief', commit=commit, token=uuid4().hex,
                    at=datetime.now(timezone.utc).isoformat(), **snapshot)
    path.with_suffix('.done').unlink(missing_ok=True)  # an old unknown-kind answer is not completion
    write_rebrief(path, data)
    return path, data


def write_rebrief(path: Path, data: dict) -> None:
    """Persist notice intent before rendering/posting; reuse the existing request file."""
    durable.replace_file(path, json.dumps(data) + '\n')


def rebrief_result(path: Path, data: dict, timeout: float = servicectl.ANSWER_SECONDS) -> str:
    deadline = time.monotonic() + timeout
    answer = path.with_suffix('.done')
    while time.monotonic() < deadline:
        if answer.exists():
            try:
                result = json.loads(answer.read_text())
                if result.get('token') == data['token'] and result.get('status') == 'complete':
                    return result['proof']
            except (ValueError, AttributeError, KeyError):
                pass
            return 'rebrief incomplete · installed service did not confirm the request'
        time.sleep(0.1)
    return 'rebrief pending · service will retry; no delivery or read is confirmed'


def venv_qd(folder: Path) -> Path | None:
    return copy_state.select(folder).qd


def running_from(bin_dir: Path) -> bool:
    """Use the interpreter's folder, not the external Python its symlink resolves to."""
    return Path(sys.executable).parent.resolve() == bin_dir.resolve()


def handoff(folder: Path, argv: list[str]) -> None:
    selected = copy_state.select(folder)
    if selected.warning:
        print(f'qd: {selected.warning}; continuing with this qd', file=sys.stderr)
    elif selected.qd is not None and not running_from(selected.qd.parent):
        try:
            os.execv(str(selected.qd), [str(selected.qd), *argv])
        except OSError:
            print('qd: installed copy could not start; continuing with this qd; '
                  'use qd sync from the checkout or qd doctor', file=sys.stderr)


def installed(runtime: Path) -> dict | None:
    """Legacy projection for existing consumers; unknown is never a successful default."""
    try:
        data = copy_state.read(runtime)
    except (OSError, ValueError, TypeError, RecursionError, UnicodeError):
        return None
    fact = copy_state.current(data)
    if fact is None:
        return None
    return {**fact, 'previous': data.get('previous'), 'pending': data.get('pending'),
            'last_failure': data.get('last_failure')}


def source_repo(ship: Ship | None = None) -> Path:
    """Prefer the executing checkout; otherwise require one unambiguous local candidate."""
    executing = Path(__file__).resolve().parents[2]
    def framework(path):
        return ((path / '.git').exists() and (path / 'pyproject.toml').is_file()
                and (path / 'src/quarterdeck/install.py').is_file())
    if framework(executing):
        return executing
    candidates = {p.resolve() for p in (ship.folder.parent.parent,) if framework(p)} if ship is not None else set()
    if len(candidates) == 1:
        return candidates.pop()
    raise ShipError('no unambiguous local framework checkout; run qd sync from the checkout')


def git(repo: Path, *args: str) -> bytes:
    try:
        done = subprocess.run(['git', '-C', str(repo), *args], capture_output=True)
    except OSError:
        raise ShipError('local git could not start; invoke qd sync from a framework checkout with git available') from None
    if done.returncode:
        raise ShipError(f"git {' '.join(args)} failed; invoke qd sync from a local framework checkout")
    return done.stdout


def previous_install(runtime: Path) -> dict | None:
    return installed(runtime)


def install_direction(repo: Path, commit: str, previous: dict | None) -> str:
    if previous is None:
        return 'unknown'
    if previous['commit'] == commit:
        return 'same commit'
    if previous.get('previous') == commit:
        return 'rollback'
    for older, newer, direction in ((commit, previous['commit'], 'rollback'),
                                     (previous['commit'], commit, 'forward')):
        result = subprocess.run(['git', '-C', str(repo), 'merge-base', '--is-ancestor', older, newer],
                                capture_output=True)
        if result.returncode == 0:
            return direction
        if result.returncode != 1:
            return 'unknown'
    return 'unknown'


def dirty_notice(ship: Ship, fact: dict | None = None) -> list[str]:
    try:
        repo = source_repo(ship)
        dirty = git(repo, 'status', '--porcelain', '--untracked-files=normal')
        commit = fact['commit'] if fact else git(repo, 'rev-parse', 'HEAD').decode().strip()
    except ShipError:
        return []  # an installed up/back does not require a checkout
    if not dirty:
        return []
    if ship.code == 'checkout':
        return ['uncommitted changes · this ship follows the checkout, including its working files']
    return [f'uncommitted changes · copy is commit {commit[:7]}, not the working files']


def historical_notice(fact: dict | None) -> list[str]:
    if fact and fact.get('safety') != COPY_SAFETY:
        return ['historical target · its own sync handler has old in-place management; '
                'transactional recovery and service identity confirmation are not established for that code']
    return []


def _state(runtime: Path) -> dict:
    try:
        data = copy_state.read(runtime)
    except (OSError, ValueError, TypeError, RecursionError, UnicodeError):
        if (runtime / RECORD).exists():
            raise ShipError('installed state is unreadable; no copy operation attempted; inspect qd doctor') from None
        data = None
    if data and 'format' in data:
        if any(data.get(key) is not None and not copy_state.valid_fact(data[key])
               for key in ('current', 'previous_copy')):
            raise ShipError('installed copy facts are malformed; no directory roles guessed; inspect qd doctor')
        if data.get('pending') is not None and not isinstance(data['pending'], dict):
            raise ShipError('installed operation journal is malformed; inspect qd doctor')
        return data
    fact = copy_state.current(data)
    return dict(format=copy_state.FORMAT, current=fact, previous_copy=None,
                pending=None, last_failure=None)


def _save(runtime: Path, data: dict) -> None:
    """Keep the historical top-level projection, without inventing a historical proof."""
    fact, previous = data.get('current'), data.get('previous_copy')
    for key in ('commit', 'ref', 'at', 'proof', 'proof_command'):
        data.pop(key, None)
    if fact:
        for key in ('commit', 'ref', 'at'):
            data[key] = fact[key]
        if fact.get('proof') is not None:
            data['proof'] = fact['proof']
    data['previous'] = previous['commit'] if previous else None
    durable.replace_file(runtime / RECORD, json.dumps(data, indent=2) + '\n')


@contextmanager
def install_lock(ship: Ship, *, blocking: bool = True):
    resources.share_path(ship.runtime / 'venv/share/quarterdeck')
    resources.share_path(ship.runtime / 'upgrade')
    fence(ship)
    with (ship.runtime / 'install.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise ShipError('another copy operation holds the install lock; try qd up or qd sync after it finishes') from None
        try:
            try:
                yield
            except OSError:
                raise ShipError('copy storage/control failed; completion is not confirmed; '
                                'journal retained where written; inspect qd doctor') from None
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _phase(ship: Ship, data: dict, phase: str, **changes) -> None:
    data['pending'].update(phase=phase, **changes)
    _save(ship.runtime, data)


def _plain(path: Path) -> bool:
    try:
        return copy_state.root_id(path) is not None
    except ValueError:
        raise ShipError(f'{path.name} is not a real copy directory; no copy operation attempted') from None


def _remove(path: Path) -> None:
    if _plain(path):
        shutil.rmtree(path)
        durable.sync_folder(path.parent)


def _flush_tree(root: Path) -> None:
    """Sync the copied files and directories, never follow a venv's interpreter links."""
    for folder, directories, files in os.walk(root, topdown=False, followlinks=False):
        for name in files:
            path = Path(folder) / name
            if path.is_symlink():
                continue
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        for name in directories:
            path = Path(folder) / name
            if not path.is_symlink():
                durable.sync_folder(path)
        durable.sync_folder(Path(folder))
    durable.sync_folder(root.parent)


def _settle_rename(ship: Ship, data: dict) -> None:
    """An intent survives either side of rename; directory identity disambiguates same-SHA copies."""
    pending = data['pending']
    intent = pending.get('rename')
    if not intent:
        return
    if (intent.get('source') not in ROLES
            or intent.get('destination') not in ROLES
            or intent['source'] == intent['destination']):
        raise ShipError('copy journal rename is unknown; no rename attempted')
    source, destination = (ship.runtime / intent[k] for k in ('source', 'destination'))
    before, after = copy_state.root_id(source), copy_state.root_id(destination)
    if before == intent['identity'] and after is None:
        source.rename(destination)
        durable.sync_folder(ship.runtime)
    elif before is None and after == intent['identity']:
        durable.sync_folder(ship.runtime)
    else:
        raise ShipError('copy journal directory identity mismatch; recovery remains pending')
    pending['moves'].append(intent)
    pending['rename'] = None
    _save(ship.runtime, data)


def _rename(ship: Ship, data: dict, source: str, destination: str, *, undo: bool = False) -> None:
    identity = copy_state.root_id(ship.runtime / source)
    if identity is None or os.path.lexists(ship.runtime / destination):
        raise ShipError('copy rename source is missing or destination is occupied; recovery remains pending')
    data['pending']['rename'] = dict(source=source, destination=destination, identity=identity, undo=undo)
    _save(ship.runtime, data)  # intent BEFORE the rename
    _settle_rename(ship, data)


def _failure(data: dict, phase: str, *, cause: str | None = None) -> None:
    # Fixed vocabulary only: no native output, environment, raw exception or state-file text.
    pending = data.get('pending')
    if pending and pending.get('failure'):
        data['last_failure'] = pending['failure']
        return
    command = pending.get('target', {}).get('proof_command') if pending else None
    proof_step = phase == 'proving' and command in ('origin', '--version', 'laws', '--help')
    reason = cause if cause in FAILURE_CAUSES else 'copy operation did not complete'
    data['last_failure'] = dict(phase=phase, reason=reason,
                                at=datetime.now(timezone.utc).isoformat(timespec='seconds'))
    step = command if proof_step else phase if phase != 'proving' and phase in FAILURE_STEPS else None
    if step is not None:
        data['last_failure']['step'] = step
    if pending:
        pending['failure'] = dict(data['last_failure'])


def _cleanup(ship: Ship, data: dict) -> None:
    pending = data['pending']
    _phase(ship, data, 'cleanup')
    _settle_rename(ship, data)
    if pending.get('activated') and pending.get('backup') == BACKUP and os.path.lexists(ship.runtime / BACKUP):
        # Only now does the copy before the old one give way: until activation it stays for --back.
        if copy_state.root_id(ship.runtime / BACKUP) != pending['backup_identity']:
            raise ShipError('copy backup is missing or changed; cleanup remains pending')
        _remove(ship.runtime / 'venv.prev')
        _rename(ship, data, BACKUP, 'venv.prev')
    staging = ship.runtime / 'upgrade' / pending['token']
    _remove(staging)
    if (ship.runtime / 'upgrade').is_dir() and not any((ship.runtime / 'upgrade').iterdir()):
        (ship.runtime / 'upgrade').rmdir()
        durable.sync_folder(ship.runtime)
    if os.path.lexists(ship.runtime / 'venv.swap'):
        raise ShipError('copy swap still exists; cleanup remains pending')
    if os.path.lexists(ship.runtime / BACKUP):
        raise ShipError('copy backup still exists; cleanup remains pending')
    data['pending'] = None
    try:
        _save(ship.runtime, data)
    except BaseException:
        data['pending'] = pending
        raise


def _restart_old(ship: Ship, data: dict, *, bootstrap: bool) -> list[str]:
    pending = data['pending']
    if not pending['was_running']:
        return []
    if pending['old_live_identity'] is None:
        state = 'running' if servicectl.alive(ship.runtime) else 'stopped'
        return [f'no earlier installed copy to restart; service remains {state}']
    if bootstrap:
        return ['earlier copy restored; this service bootstrap will continue on it']
    fact = data.get('current')
    if copy_state.usable(ship.runtime / 'venv', fact):
        raise ShipError('earlier live copy restored but is unproven; service not restarted; inspect qd doctor')
    return [*historical_notice(fact), servicectl.start(ship, qd=ship.runtime / 'venv/bin/qd',
                            expected=fact if fact.get('safety') == COPY_SAFETY else None,
                            startup_token=pending['token'])]


def _recover(ship: Ship, data: dict, *, bootstrap: bool = False) -> list[str]:
    """Undo unfinished mutation, or finish cleanup after durable activation. Caller holds install lock."""
    pending = data.get('pending')
    if pending is None:
        return []
    if (not isinstance(pending, dict) or pending.get('kind') not in ('sync', 'back')
            or not isinstance(pending.get('token'), str)
            or not re.fullmatch(r'[0-9a-f]{32}', pending['token'])
            or not isinstance(pending.get('moves'), list)
            or pending.get('backup') not in (None, BACKUP)
            or any(type(pending.get(key)) is not bool for key in
                   ('was_running', 'mutable', 'backup_started', 'backup_ready', 'activated', 'restored'))):
        raise ShipError('unknown copy journal; recovery cannot infer directory roles; inspect qd doctor')
    if pending.get('activated'):
        _cleanup(ship, data)
        return ['completed the activated copy cleanup']
    phase = pending.get('phase', 'unknown')
    _failure(data, phase)
    _phase(ship, data, 'recovering', recovering=True,
           owner_pid=os.getpid(), owner_start=servicectl.process_start(os.getpid()))
    mutable = pending.get('mutable', False)
    if pending.get('restored') and copy_state.root_id(ship.runtime / 'venv') != pending.get('restored_identity'):
        raise ShipError('restored directory identity changed; recovery remains pending')
    if mutable and not pending.get('restored') and servicectl.alive(ship.runtime):
        servicectl.stop(ship.runtime)
    _settle_rename(ship, data)
    if pending['kind'] == 'back' and mutable:
        # Undo progress is itself journaled. Completed inverse moves count durably.
        forward = [m for m in pending['moves'] if not m.get('undo')]
        undone = len([m for m in pending['moves'] if m.get('undo')])
        for move in reversed(forward[:len(forward) - undone]):
            _rename(ship, data, move['destination'], move['source'], undo=True)
        if copy_state.root_id(ship.runtime / 'venv') != pending['old_live_identity']:
            raise ShipError('original live directory is not in its recorded role; recovery remains pending')
        previous_identity = copy_state.root_id(ship.runtime / 'venv.prev')
        if (previous_identity != pending['old_previous_identity']
                and not (pending.get('discard_previous') and previous_identity is None)):
            raise ShipError('previous directory is not in its recorded role; recovery remains pending')
        data['current'], data['previous_copy'] = pending['old_current'], None
    elif mutable:
        # A journal written before the backup had a role of its own kept it at venv.prev.
        backup = pending.get('backup') or 'venv.prev'
        # Retrying after backup->live is safe even if the crash preceded its state write.
        restored = any(m['source'] == backup and m['destination'] == 'venv'
                       for m in pending['moves'])
        if not restored:
            if pending.get('backup_ready'):
                if copy_state.root_id(ship.runtime / backup) != pending['backup_identity']:
                    raise ShipError('complete backup is missing or changed; recovery remains pending')
                _phase(ship, data, 'removing_failed_live')
                _remove(ship.runtime / 'venv')
                _rename(ship, data, backup, 'venv')
            elif pending.get('old_live_identity') is None:
                _phase(ship, data, 'removing_failed_first_install')
                _remove(ship.runtime / 'venv')
            else:
                raise ShipError('live mutation has no complete backup; recovery remains pending')
        data['current'], data['previous_copy'] = pending['old_current'], _kept_previous(ship, pending)
    else:
        # Before live mutation, the current directory must still be the recorded one.
        if copy_state.root_id(ship.runtime / 'venv') != pending['old_live_identity']:
            raise ShipError('current directory changed before live mutation; recovery remains pending')
        backup = pending.get('backup') or 'venv.prev'
        if pending.get('backup_started'):
            _remove(ship.runtime / backup)  # a partial backup; the copy before only in a journal that put it there
        # A journal from before the backup's own role had consumed the older rollback once its backup began.
        consumed = pending.get('backup_started') and backup == 'venv.prev'
        data['previous_copy'] = None if consumed else pending['old_previous']
        data['current'] = pending['old_current']
    _phase(ship, data, 'restored', restored=True,
           restored_identity=copy_state.root_id(ship.runtime / 'venv'))
    proofs = _restart_old(ship, data, bootstrap=bootstrap)
    if pending['kind'] == 'back' and mutable:
        # Keep the candidate until the original has been restored/restarted where possible.
        _phase(ship, data, 'discarding_failed_previous', discard_previous=True)
        _remove(ship.runtime / 'venv.prev')
    # A restored startup uses the same token without taking our install lock again.
    _phase(ship, data, 'restored', restored=True)
    _cleanup(ship, data)
    restored = ('interrupted copy operation undone; earlier copy restored'
                if pending['old_live_identity'] is not None else
                'incomplete first install removed; no installed copy selected')
    return [restored, *proofs]


def _kept_previous(ship: Ship, pending: dict) -> dict | None:
    """After live mutation, the copy before survives where its directory is still the one recorded at the start.

    A journal from before the backup's own role had consumed venv.prev: it keeps none.
    """
    if pending.get('backup') != BACKUP:
        return None
    if copy_state.root_id(ship.runtime / 'venv.prev') != pending['old_previous_identity']:
        return None
    return pending['old_previous']


def recover(ship: Ship) -> list[str]:
    if ship.code == 'checkout':
        return []
    try:
        data = copy_state.read(ship.runtime)
    except (OSError, ValueError, TypeError, RecursionError, UnicodeError):
        raise ShipError('installed state unreadable; no recovery guessed; inspect qd doctor') from None
    if not data or data.get('pending') is None:
        return []
    with install_lock(ship):
        return _recover(ship, _state(ship.runtime))


def _capable(source: Path) -> bool:
    """Only the target's own literal, never a marker manufactured by its installer."""
    try:
        path = source / 'install.py'
        if path.stat().st_size > 1048576:
            return False
        tree = ast.parse(path.read_text())
        return any(isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'COPY_SAFETY'
                   for t in node.targets) and isinstance(node.value, ast.Constant)
                   and node.value.value == COPY_SAFETY for node in tree.body)
    except (OSError, ValueError, SyntaxError, RecursionError, UnicodeError):
        return False


def _proof(ship: Ship, fact: dict) -> tuple[str, list[str]]:
    """Exactly the same live-path proof for sync and back; no ship handoff or source redirect."""
    live = ship.runtime / 'venv'
    if 'instance' not in fact:
        # The ordinary back proof can record an identity; a legacy no-op never does.
        fact['instance'] = uuid4().hex
        durable.replace_file(live / copy_state.MARKER,
                             json.dumps(dict(commit=fact['commit'], instance=fact['instance'])) + '\n')
    environment = dict(os.environ)
    for name in ('QD_SHIP', 'QD_SEAT', 'QD_COPY_START', 'PYTHONPATH', 'PYTHONHOME', 'PYTHONUSERBASE'):
        environment.pop(name, None)
    environment['PYTHONNOUSERSITE'] = '1'
    origin_code = (
        'import hashlib, json, pathlib, sys, quarterdeck\n'
        'root = pathlib.Path(sys.argv[1]).resolve()\n'
        'package = pathlib.Path(quarterdeck.__file__).resolve()\n'
        'assert pathlib.Path(sys.prefix).resolve() == root\n'
        'assert package.is_relative_to(root)\n'
        'if sys.argv[2] == "laws":\n'
        ' from quarterdeck import standing\n'
        ' assert pathlib.Path(standing.PROJECT_DIR).resolve() == root / "share/quarterdeck"\n'
        'digest = hashlib.sha256()\n'
        'for path in sorted(package.parent.rglob("*.py")):\n'
        ' digest.update(path.relative_to(package.parent).as_posix().encode() + b"\\0")\n'
        ' digest.update(path.read_bytes() + b"\\0")\n'
        'print(json.dumps({"package": str(package.parent), "package_digest": digest.hexdigest()}))\n'
    )
    have_laws = fact['have_laws']
    fact['proof_command'] = 'origin'
    origin = _run([str(live / 'bin/python'), '-I', '-c', origin_code, str(live),
                   'laws' if have_laws else '--help'], 'checking live package, prefix and resources',
                  timeout=60, env=environment, cwd=ship.runtime)
    try:
        identity = json.loads(origin.stdout)
        package = Path(identity['package'])
        fact['package_digest'] = identity['package_digest']
    except (ValueError, TypeError, KeyError):
        raise ShipError('live package identity proof returned an invalid answer') from None
    fact['proof_command'] = '--version'
    version = _run([str(live / 'bin/qd'), '--version'], 'proving installed version',
                   timeout=60, env=environment, cwd=ship.runtime).stdout.strip()
    command = 'laws' if have_laws else '--help'
    fact['proof_command'] = command
    _run([str(live / 'bin/qd'), command], 'proving installed copy answers',
         timeout=60, env=environment, cwd=ship.runtime)
    fact.update(proof='good', proof_command=command,
                safety=COPY_SAFETY if _capable(package) else None)
    warnings = historical_notice(fact)
    if fact.get('fallback'):
        warnings.append(resources.FALLBACK)
    if not have_laws:
        warnings.append(resources.NO_LAWS)
    return version, warnings


def _snapshot_operation(ship: Ship, data: dict, kind: str, target: dict) -> dict:
    for role in ('venv.swap', BACKUP):
        if os.path.lexists(ship.runtime / role):
            raise ShipError(f'{role} exists without a pending operation; directory roles unknown; inspect qd doctor')
    pending = dict(kind=kind, token=uuid4().hex, phase='preparing', target=target,
                   old_current=data.get('current'), old_previous=data.get('previous_copy'),
                   old_live_identity=copy_state.root_id(ship.runtime / 'venv'),
                   old_previous_identity=copy_state.root_id(ship.runtime / 'venv.prev'),
                   was_running=servicectl.alive(ship.runtime), mutable=False,
                   backup_started=False, backup_ready=False, moves=[], rename=None,
                   activated=False, restored=False, backup=None if kind == 'back' else BACKUP)
    pending.update(owner_pid=os.getpid(), owner_start=servicectl.process_start(os.getpid()))
    data['pending'] = pending
    _save(ship.runtime, data)
    return pending


def _stop_and_snapshot(ship: Ship, data: dict) -> list[str]:
    _phase(ship, data, 'stopping')
    proofs = [servicectl.stop(ship.runtime)] if data['pending']['was_running'] else []
    _phase(ship, data, 'snapshotting')
    data['pending']['snapshot'] = brief_snapshot(ship)
    _save(ship.runtime, data)
    return proofs


def _activate(ship: Ship, data: dict, fact: dict, tmux: Tmux | None) -> list[str]:
    pending = data['pending']
    _flush_tree(ship.runtime / 'venv')
    fact['at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
    data['current'] = fact
    data['previous_copy'] = pending['old_current']
    _phase(ship, data, 'target_ready')  # durable proof, still ineligible for ordinary handoff
    rebrief, request = queue_rebrief(ship, fact['commit'], pending['old_current'], pending['snapshot'])
    proofs = []
    if pending['was_running']:
        proofs.append(servicectl.start(ship, qd=ship.runtime / 'venv/bin/qd',
                                      expected=fact if fact.get('safety') == COPY_SAFETY else None,
                                      startup_token=pending['token']))
    # Only a confirmed startup (or an intentionally stopped ship) may commit activation.
    pending['activated'] = True
    _phase(ship, data, 'activated')
    _cleanup(ship, data)
    # Post-activation work is visible and retryable; never roll healthy code back for a status-line error.
    if pending['was_running']:
        try:
            proofs.append(rebrief_result(rebrief, request))
        except (OSError, ValueError, TypeError):
            data['last_failure'] = dict(phase='rebrief', reason='rebrief confirmation pending',
                                        at=datetime.now(timezone.utc).isoformat(timespec='seconds'))
            _save(ship.runtime, data)
            proofs.append('rebrief confirmation pending; inspect qd doctor; installed code remains active')
    else:
        proofs += ['service not running; qd up starts it on the installed copy',
                   'rebrief pending · qd up renders current briefs and posts changed-brief notices']
    if tmux is not None:
        try:
            pointed = [s.title for s in ship.sessions.values() if tmux.has_session(s.title)]
            for title in pointed:
                tmux.status_line(title, shlex.join([str(ship.runtime / 'venv/bin/qd'), '--ship', str(ship.folder), 'status', '--line']))
            if pointed:
                proofs.append(f"status lines of {', '.join(pointed)} point at the installed copy")
        except Exception:
            data['last_failure'] = dict(phase='status lines', reason='status-line update pending',
                                        at=datetime.now(timezone.utc).isoformat(timespec='seconds'))
            _save(ship.runtime, data)
            proofs.append('status-line update pending; installed code remains active; inspect qd doctor')
    return proofs


def resolve_target(repo: Path, ref: str = 'HEAD') -> tuple[Path, str]:
    """Pin one local commit for a single sync or a whole sequential project sync."""
    repo = repo.resolve()
    commit = git(repo, 'rev-parse', '--verify', f'{ref}^{{commit}}').decode().strip()
    if copy_state.COMMIT.fullmatch(commit) is None:
        raise ShipError('local target did not resolve to a complete commit')
    return repo, commit


def upgrade(ship: Ship, ref: str = 'HEAD', tmux: Tmux | None = None, *,
            back: bool = False, force: bool = False,
            target: tuple[Path, str] | None = None) -> list[str]:
    """Stable internal API for canonical sync and its shared upgrade alias handler."""
    if ship.code == 'checkout':
        raise ShipError(copy_state.CHECKOUT_REFUSAL)
    if back and (force or ref != 'HEAD'):
        raise ShipError('--back cannot be combined with a target or --force')
    if target is not None and (back or copy_state.COMMIT.fullmatch(target[1]) is None):
        raise ShipError('a pinned local target requires a complete commit and cannot go back')
    recovery_proofs = recover(ship)  # before source discovery, target resolution, or a no-op
    repo, commit = (None, None) if back else (target or resolve_target(source_repo(ship), ref))
    if not back:
        selected = copy_state.select(ship.folder, mode=ship.code, mode_known=True)
        if not force and selected.qd and selected.fact['commit'] == commit:
            return [*recovery_proofs, *dirty_notice(ship, selected.fact), *historical_notice(selected.fact),
                    f'already runs {commit[:7]}; nothing to do']
    # Preload controller dependencies before in-place pip can replace their source files.
    from .seat import qd_bin  # noqa: F401
    with install_lock(ship):
        data = _state(ship.runtime)
        recovered = [*recovery_proofs, *_recover(ship, data)]
        old = data.get('current')
        if back:
            target = copy_state.previous(data)
            reason = copy_state.usable(ship.runtime / 'venv.prev', target)
            if reason:
                raise ShipError('no usable previous copy: ' + reason + '; the first successful sync creates one')
            reason = copy_state.usable(ship.runtime / 'venv', old)
            if reason:
                raise ShipError('cannot swap an unproven current copy: ' + reason + '; use qd sync to repair it')
            fact = {**target, 'at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
        else:
            if (not force and old and old['commit'] == commit
                    and copy_state.usable(ship.runtime / 'venv', old) is None):
                return [*recovered, *dirty_notice(ship, old), *historical_notice(old),
                        f'already runs {commit[:7]}; nothing to do']
            fact = dict(commit=commit, ref=ref, at=datetime.now(timezone.utc).isoformat(timespec='seconds'),
                        instance=uuid4().hex, proof='unproven')
        pending = _snapshot_operation(ship, data, 'back' if back else 'sync', fact)
        proofs = [*recovered, *([] if back else dirty_notice(ship, fact))]
        cause = 'source archive unavailable'
        try:
            if not back:
                staging = ship.runtime / 'upgrade' / pending['token']
                staging.mkdir(parents=True)
                with tarfile.open(fileobj=io.BytesIO(git(repo, 'archive', '--format=tar', commit))) as archive:
                    archive.extractall(staging, filter='data')
                cause = 'resource declaration invalid'
                resource_files = resources.plan(staging)
            proofs += _stop_and_snapshot(ship, data)
            if back:
                _phase(ship, data, 'swapping', mutable=True)
                _rename(ship, data, 'venv', 'venv.swap')
                _rename(ship, data, 'venv.prev', 'venv')
                _rename(ship, data, 'venv.swap', 'venv.prev')
                fact.setdefault('have_laws', (ship.runtime / 'venv/share/quarterdeck/standing-orders/laws.md').is_file())
            else:
                # The live copy is backed up beside the copy before it, which stays untouched until the new
                # copy is active: a failure at any phase leaves both copies and their record as they were.
                _phase(ship, data, 'backing_up', backup_started=True)
                live, backup = ship.runtime / 'venv', ship.runtime / BACKUP
                if copy_state.root_id(live) != pending['old_live_identity']:
                    raise ShipError('current directory changed before backup; no live install attempted')
                if _plain(live):
                    _run(['cp', '-a', '--', str(live), str(backup)], 'copying current environment')
                    _flush_tree(backup)
                    _phase(ship, data, 'backup_ready', backup_ready=True,
                           backup_identity=copy_state.root_id(backup))
                cause = 'environment could not be made'
                _phase(ship, data, 'installing', mutable=True)
                if not (live / 'bin/python').exists():
                    _run([sys.executable, '-m', 'venv', '--system-site-packages', str(live)], 'making ship environment')
                cause = 'package build failed'
                _run([str(live / 'bin/python'), '-m', 'pip', 'install', '--quiet', '--no-index', '--no-deps',
                      '--no-build-isolation', str(staging)], 'installing archived commit')
                _phase(ship, data, 'resources')
                share = live / 'share/quarterdeck'
                if resource_files is not None:
                    resources.synchronize(staging, share, resource_files)
                    fact.update(have_laws=True, fallback=False)
                else:
                    fact.update(have_laws=resources.fallback(staging, share), fallback=True)
                durable.replace_file(live / copy_state.MARKER,
                                     json.dumps(dict(commit=commit, instance=fact['instance'])) + '\n')
            # A previous copy's last proof does not establish a command in this attempt.
            fact.pop('proof_command', None)
            _phase(ship, data, 'proving')
            version, warnings = _proof(ship, fact)
            proofs += warnings
            after = _activate(ship, data, fact, tmux)
        except Exception:
            phase = pending.get('phase', 'unknown')
            cause = PHASE_CAUSES.get(phase, cause)
            step = fact.get('proof_command') if phase == 'proving' else phase
            detail = cause + (f' at {step}' if step in FAILURE_STEPS and step != 'proving' else '')
            if pending.get('activated'):
                # A post-activation reporting write can fail after cleanup cleared
                # the journal. Reattach its cleanup-only intent before saying it is kept.
                if data.get('pending') is None:
                    data['pending'] = pending
                _failure(data, phase, cause=cause)
                _save(ship.runtime, data)
                raise ShipError(f'copy active but {phase} incomplete: {detail}; journal kept; qd up or qd sync retries cleanup') from None
            _failure(data, phase, cause=cause)
            try:
                _save(ship.runtime, data)
                restoration = _recover(ship, data)
            except Exception:
                raise ShipError(f'copy operation failed during {phase}: {detail}; restoration/restart incomplete; '
                                'journal kept; inspect qd doctor, then qd up or qd sync') from None
            raise ShipError(f'copy operation failed during {phase}: {detail}; recovery completed; '
                            + '; '.join(restoration)) from None
        before = old['commit'][:7] if old else 'unknown'
        action = 'returned' if back else 'installed'
        remedy = f'qd sync --back returns to {before}' if old else 'no proven previous copy'
        return [*proofs, f'{action} {fact["commit"][:7]} at runtime/venv · {version} · was {before} · {remedy}', *after]


def prepare_up(ship: Ship) -> tuple[list[str], bool]:
    """One seam for direct up and setup, after their existing consent, before any seat."""
    if ship.code == 'checkout':
        return dirty_notice(ship), False
    proofs = recover(ship)
    reload_required = bool(proofs)
    selected = copy_state.select(ship.folder, mode=ship.code, mode_known=True)
    if selected.qd is None:
        if selected.fact is not None or os.path.lexists(ship.runtime / 'venv'):
            raise ShipError((selected.warning or 'copy is unproven') + '; run qd sync from the checkout')
        proofs += upgrade(ship)
        reload_required = True
        selected = copy_state.select(ship.folder, mode=ship.code, mode_known=True)
        if selected.qd is None:
            raise ShipError('first install did not leave a proven copy; no seats started; inspect qd doctor')
    else:
        proofs += dirty_notice(ship, selected.fact) + historical_notice(selected.fact)
    return proofs, reload_required


def delegate_up(ship: Ship, *, seat: str | None, fresh: bool, reload_required: bool = False,
                pool: str | None = None) -> list[str] | None:
    selected = copy_state.select(ship.folder, mode=ship.code, mode_known=True)
    if selected.checkout:
        return None
    if selected.qd is None:
        raise ShipError('installed copy unavailable; no seats started; inspect qd doctor')
    if running_from(selected.qd.parent) and not reload_required:
        if ('instance' in selected.fact and LOADED_COPY !=
                {key: selected.fact.get(key) for key in ('commit', 'instance', 'package_digest')}):
            raise ShipError('loaded copy identity differs from selected code; no seats started; '
                            'run qd up from the checkout or inspect qd doctor')
        return None
    argv = [str(selected.qd), '--ship', str(ship.folder), '--json', 'up']
    if seat:
        argv.append(seat)
    if fresh:
        argv.append('--fresh')
    if pool:
        argv += ['--pool', pool]  # a copy that predates --pool refuses it: never dropped silently
    try:
        done = subprocess.run(argv, capture_output=True, text=True)
    except OSError:
        raise ShipError('installed up could not start; no fallback or retry made; inspect qd doctor') from None
    if done.returncode:
        # A child may have acted before failing. Never replay its up in the caller.
        raise ShipError('installed up failed; no retry made; inspect qd status and the installed copy')
    try:
        result = json.loads(done.stdout)
    except ValueError:
        raise ShipError('installed up returned unreadable proof; no retry made; inspect qd status') from None
    if not isinstance(result, list) or not all(isinstance(line, str) for line in result):
        raise ShipError('installed up returned unknown proof; no retry made; inspect qd status')
    return result


def bootstrap_service(ship: Ship, argv: list[str]) -> None:
    """Before construction/lock: recover, then exec if any loaded code may have changed."""
    if ship.code == 'checkout':
        return
    data = _state(ship.runtime)
    pending = data.get('pending')
    token = os.environ.get('QD_COPY_START')
    if (pending and pending.get('phase') in ('target_ready', 'restored')
            and token and token == pending.get('token')
            and _parent_holds_install_lock(ship, pending)):
        selected = data.get('current')
        if (not running_from(ship.runtime / 'venv/bin')
                or copy_state.usable(ship.runtime / 'venv', selected)
                or LOADED_COPY != {key: selected.get(key) for key in ('commit', 'instance', 'package_digest')}):
            raise ShipError('authorized service startup has unknown live identity; no service constructed')
        return  # the live parent owns the install lock and will confirm/finalize startup
    changed = False
    with install_lock(ship):
        data = _state(ship.runtime)
        if data.get('pending'):
            _recover(ship, data, bootstrap=True)
            changed = True
    selected = copy_state.select(ship.folder, mode=ship.code, mode_known=True)
    if selected.qd is None:
        raise ShipError((selected.warning or 'copy unavailable') + '; service not started')
    if (not changed and running_from(selected.qd.parent) and 'instance' in selected.fact
            and LOADED_COPY != {key: selected.fact.get(key) for key in ('commit', 'instance', 'package_digest')}):
        if LOADED_COPY and LOADED_COPY.get('instance') != selected.fact['instance']:
            changed = True  # another installer completed while this bootstrap waited for its lock
        else:
            raise ShipError('loaded service copy identity is unknown or mismatched; no service constructed; '
                            'run qd sync from the checkout')
    if changed or not running_from(selected.qd.parent):
        os.environ.pop('QD_COPY_START', None)
        os.execv(str(selected.qd), [str(selected.qd), *argv])


def _parent_holds_install_lock(ship: Ship, pending: dict) -> bool:
    pid = pending.get('owner_pid')
    if (type(pid) is not int or pending.get('owner_start') is None
            or servicectl.process_start(pid) != pending['owner_start']):
        return False
    try:
        with (ship.runtime / 'install.lock').open('rb') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        return False
    return False


def _run(argv: list[str], doing: str, timeout: float | None = None, **kwargs) -> subprocess.CompletedProcess:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, **kwargs)
    except (subprocess.TimeoutExpired, OSError):
        raise ShipError(f'{doing} failed or timed out') from None
    if done.returncode:
        raise ShipError(f'{doing} failed (exit {done.returncode})')
    return done
