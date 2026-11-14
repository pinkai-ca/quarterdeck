"""Preserve malformed operational ledgers; readers never repair a running writer."""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import secrets
import sqlite3

from . import durable, servicectl

PREFIX = 'ledger-corrupt-'


def malformed(error):
    code = getattr(error, 'sqlite_errorcode', None)
    return isinstance(error, sqlite3.DatabaseError) and type(code) is int and code & 255 in {
        sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}


def check(path):
    """Read all pages without changing a pragma or repairing the original in place."""
    if not path.exists():
        return True
    db = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    try:
        return db.execute('SELECT quick_check FROM pragma_quick_check').fetchall() == [('ok',)]
    finally:
        db.close()


@contextmanager
def opening(path):
    """Serialize writer initialization, and exclude a live service before any move."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            healthy = check(path)
        except sqlite3.DatabaseError as error:
            if not malformed(error):
                raise  # busy, I/O, permissions and full storage are not corrupt data
            healthy = False
        if healthy:
            yield not path.exists() and bool(pending(path.parent))
        else:
            # Service construction precedes its lifetime lock. A competing live
            # service must stop before a writer can preserve its malformed book.
            with servicectl.acquire_lock(path.parent):
                folder = path.parent / 'evidence' / (PREFIX + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ-') + secrets.token_hex(4))
                folder.mkdir(parents=True)
                # Sidecars go first so no stale journal can attach to the fresh
                # database. Each rename preserves bytes, and a partial move is
                # left as evidence; a later open finishes by retiring the DB.
                for suffix in ('-journal', '-wal', '-shm', ''):
                    source = Path(str(path) + suffix)
                    if source.exists():
                        source.rename(folder / source.name)
                yield True
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
