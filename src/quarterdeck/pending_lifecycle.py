"""Writer-owned journal retirement and the last exact join of a retiring launch.

Callers hold the seat's lifecycle claim. Readings never call these repairs.
"""

import fcntl
import json
import os
from pathlib import Path
import stat

from . import durable, pending, post, post_dir, record_owner
from .ship import ShipError

UNKNOWN = 'delivery journal had no complete launch identity; outcome unknown; never resent'


def _ordinary_unbound(data, name):
    """Only null identity fields may fail the unchanged ordinary validator."""
    core = {'seat', 'identity', 'at', 'since', 'payload', 'phase', 'record', 'shown', 'attempts'}
    optional = {'resolution', 'screen_queue', 'paste_boundary', 'service_owner', 'delivery_token', 'look'}
    if (not isinstance(data, dict) or not core <= data.keys() or data.keys() - core - optional
            or any(key in data for key in ('recovery', 'receipt_refresh', 'paste_clear', 'paste_refusal'))):
        return False
    try:
        identity = data['identity']
        if (not isinstance(identity, dict) or set(identity) != {'pane', 'pid', 'launch', 'harness'}
                or not any(identity[key] is None for key in ('pane', 'pid', 'launch'))):
            return False
        # These stand-ins only ask the strict validator about the OTHER fields.
        # They never leave this local shape check or become a recovered identity.
        shape = {**data, 'identity': {**identity,
                 'pane': identity['pane'] if identity['pane'] is not None else 'unbound-pane',
                 'pid': identity['pid'] if identity['pid'] is not None else 1,
                 'launch': identity['launch'] if identity['launch'] is not None else data['at']}}
        pending.validate(shape, name)
        if 'resolution' in data and not isinstance(data['resolution'], str):
            return False
        return True
    except (KeyError, ValueError, TypeError, AttributeError):
        return False


def _retirement_log(runtime, name, filename):
    path = runtime / 'log' / 'service.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    row = dict(at=post.now().isoformat(), event='pending-retired', seat=name,
               journal=filename, result='unknown', detail=UNKNOWN)
    with path.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.write(json.dumps(row) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    durable.sync_folder(path.parent)


def retire_missing_identity(runtime, name, errors, attempts):
    """Preserve recognized legacy files, settling only their own unresolved attempts.

    The caller supplies the errors from its ordinary journal scan and the pass's
    attempt map: no second ship-wide inventory or timer is introduced.
    """
    retired = {}
    folder = runtime / 'post' / 'pending' / name
    with post.admission(runtime, name):
        for filename in errors:
            path = folder / filename
            if Path(filename).name != filename:
                continue
            descriptor = None
            writing = False
            try:
                if post_dir.under_post(path):
                    parent, filename_at = post_dir._open_parent(path, create=False)
                    try:
                        descriptor = os.open(filename_at, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                             dir_fd=parent)
                    finally:
                        os.close(parent)
                else:
                    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode):
                    continue
                with os.fdopen(descriptor, 'rb') as stream:
                    descriptor = None
                    original = stream.read()
                data = json.loads(original)
                if not _ordinary_unbound(data, name):
                    continue
                matching = [attempts[value] for value in data['attempts']]
                if any(a.seat != name or a.path.name != value
                       or not value.startswith(a.message + '-')
                       or any(data['identity'][key] is not None and data['identity'][key] != expected
                              for key, expected in (('pane', a.pane), ('pid', a.pid), ('launch', a.launched_at)))
                       for a, value in zip(matching, data['attempts'])):
                    continue
                destination = folder / 'retired'
                saved = destination / filename
                if post_dir.under_post(destination):
                    post_dir.ensure_directory(destination)
                    if not post_dir.exists_dir(destination):
                        continue
                    if post_dir.exists(saved):
                        continue  # never replace earlier evidence, even with equal bytes
                else:
                    destination.mkdir(exist_ok=True)
                    if destination.is_symlink() or not destination.is_dir():
                        continue
                    if saved.exists() or saved.is_symlink():
                        continue  # never replace earlier evidence, even with equal bytes
                current = path.lstat()
                if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino) or post_dir.read_bytes(path) != original:
                    continue
                writing = True
                updated = post.Attempts(runtime).settle_missing_identity(matching, UNKNOWN)
                if updated is None:
                    continue
                if post_dir.under_post(path):
                    post_dir.rename(path, saved)  # same inode and original bytes, outside journals()'s glob
                else:
                    path.rename(saved)
                durable.sync_folder(destination)
                durable.sync_folder(folder)
                _retirement_log(runtime, name, filename)
                retired[filename] = updated
            except FileNotFoundError as error:
                if writing:
                    error.add_note(f'pending legacy journal: {path}')
                    raise
                continue
            except (KeyError, ValueError, TypeError, UnicodeError):
                continue  # malformed or incomplete evidence is still held by the strict reader
            except OSError as error:
                error.add_note(f'pending legacy journal: {path}')
                raise
            finally:
                if descriptor is not None:
                    os.close(descriptor)
    return retired


def link_launch_record(seat, launched):
    """Bind ordinary unresolved journals before their exact old launch is discarded."""
    if launched is None:
        return
    name = record_owner.launch_reader(launched)
    if name != seat.harness.name:
        return
    bound = dict(pane=launched.pane, pid=launched.pid, launch=launched.at, harness=name)
    with post.admission(seat.ship.runtime, seat.name):
        if seat.launched() != launched:
            raise ShipError(f'{seat.name}: launch changed before its final record link; launch retained')
        record = seat.record()  # the existing one-record join; never a historical search
        if record is None:
            return
        if seat.launched() != launched:
            raise ShipError(f'{seat.name}: launch changed during its final record link; launch retained')
        proofs, _ = pending.journals(seat)
        for proof in proofs:
            if (proof.data['identity'] != bound or proof.data['phase'] in pending.TERMINAL
                    or proof.data['record'] not in (None, str(record))
                    or any(key in proof.data for key in ('recovery', 'receipt_refresh', 'paste_clear', 'paste_refusal'))):
                continue
            current = json.loads(proof.path.read_text())
            pending.validate(current, seat.name)
            if current != proof.data:
                continue
            if proof.data['record'] is None:
                proof.save(record=str(record))
