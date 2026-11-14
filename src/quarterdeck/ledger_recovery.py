"""Preserve malformed operational ledgers; readers never repair a running writer."""

from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import secrets
import shutil
import sqlite3

from . import durable, servicectl

PREFIX = 'ledger-corrupt-'


def malformed(error):
    code = getattr(error, 'sqlite_errorcode', None)
    return isinstance(error, sqlite3.DatabaseError) and type(code) is int and code & 255 in {
        sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}


def check(path, *, rollback=False):
    """Read all pages without changing a pragma or repairing the original in place."""
    if not path.exists():
        return True
    db = sqlite3.connect(path) if rollback else sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    try:
        if rollback:
            db.execute('PRAGMA synchronous=OFF')
        return db.execute('SELECT quick_check FROM pragma_quick_check').fetchall() == [('ok',)]
    finally:
        db.close()


def _folder(path):
    folder = path.parent / 'evidence' / (PREFIX + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ-') + secrets.token_hex(4))
    folder.mkdir(parents=True)
    return folder


def _move(path, folder):
    # Sidecars first: none can attach to the replacement database. A partial
    # batch preserves whole files in their original or evidence positions.
    for suffix in ('-journal', '-wal', '-shm', ''):
        source = Path(str(path) + suffix)
        if source.exists():
            source.rename(folder / source.name)


@contextmanager
def opening(path):
    """Serialize writer initialization, and exclude a live service before any move."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        with ExitStack() as owners:
            folder = None
            try:
                healthy = check(path)
            except sqlite3.DatabaseError as error:
                if malformed(error):
                    healthy = False
                elif getattr(error, 'sqlite_errorcode', None) == sqlite3.SQLITE_READONLY_ROLLBACK:
                    # A healthy process crash needs SQLite's rollback, not
                    # quarantine. Preserve the original bytes before it can
                    # alter anything, retaining them if corruption is found.
                    owners.enter_context(servicectl.acquire_lock(path.parent))
                    folder = _folder(path)
                    before = folder / 'pre-rollback'
                    before.mkdir()
                    for suffix in ('', '-journal', '-wal', '-shm'):
                        source = Path(str(path) + suffix)
                        if source.exists():
                            shutil.copyfile(source, before / source.name)
                    try:
                        healthy = check(path, rollback=True)
                    except sqlite3.DatabaseError as rollback_error:
                        if not malformed(rollback_error):
                            raise
                        healthy = False
                    if healthy:
                        shutil.rmtree(folder)  # only this call's healthy-crash copy
                        folder = None
                else:
                    raise  # busy, I/O, permissions and full storage are not corrupt data
            if not healthy:
                if folder is None:
                    owners.enter_context(servicectl.acquire_lock(path.parent))
                    folder = _folder(path)
                _move(path, folder)
            # A crash after moving the DB but before schema setup also resumes
            # as a fresh catching-up ledger, without losing its evidence alarm.
            yield not healthy or not path.exists() and bool(pending(path.parent))
    finally:
        os.close(descriptor)


def pending(runtime):
    """Evidence remains an alarm until explicitly acknowledged, even after replay."""
    return [folder for folder in sorted((runtime / 'evidence').glob(PREFIX + '*'))
            if folder.is_dir() and not (folder / 'acknowledged').exists()]


def alarms(runtime):
    return [f'ledger recovery: {folder.relative_to(runtime)}'
            + ('/ledger.sqlite' if (folder / 'ledger.sqlite').exists() else ' (partial archive)')
            + '; qd ledger --clear-recovery' for folder in pending(runtime)]


def acknowledge(runtime):
    folders = pending(runtime)
    for folder in folders:
        durable.replace_file(folder / 'acknowledged', datetime.now(timezone.utc).isoformat() + '\n')
    return [str(folder.relative_to(runtime)) for folder in folders]
