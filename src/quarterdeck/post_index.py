"""A disposable index of immutable post filenames, references and addresses."""

from __future__ import annotations

from contextlib import closing
import ctypes
import fcntl
import os
import re
import sqlite3
import stat
import struct
import time

from . import fleet_post, forward_post, post, post_dir

CACHE = 'message-index-v1.sqlite3'
SCHEMA = {
    'snapshot': ('CREATE TABLE snapshot (singleton INTEGER PRIMARY KEY CHECK (singleton = 1),'
                 ' revision INTEGER, stamp TEXT, certified_at INTEGER)'),
    'messages': ('CREATE TABLE messages (name TEXT PRIMARY KEY, identity TEXT, ref TEXT,'
                 ' recipient TEXT, short TEXT)'),
    'message_ref': 'CREATE INDEX message_ref ON messages(ref)',
    'message_recipient': 'CREATE INDEX message_recipient ON messages(recipient)',
    'message_short': 'CREATE INDEX message_short ON messages(short)',
}
SQLITE_SIDECARS = ('-journal', '-wal', '-shm')
STAMP = re.compile(r'\((\d{1,20}), (\d{1,20}), (-?\d{1,20}), (-?\d{1,20})\)', re.ASCII)
EVENT = struct.Struct('iIII')
CHILD_EVENTS = 0x000003ce  # modify, attrib, close-write, moved from/to, create, delete
WATCH_EVENTS = CHILD_EVENTS | 0x00000c00  # delete or move of the directory itself


class IndexUnavailable(RuntimeError):
    """The cache could not supply a trustworthy result; use the canonical Store."""


class _InvalidCache(RuntimeError):
    """The disposable cache needs to be replaced."""


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns


class _Changes:
    """Names changed since a watched inventory. An overflow or lost watch requests a new inventory."""

    def __init__(self, directory, identity):
        self.identity = identity
        self.fd = None
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            descriptor = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
            if descriptor >= 0:
                self.fd = descriptor
                if libc.inotify_add_watch(descriptor, os.fsencode(f'/proc/self/fd/{directory}'), WATCH_EVENTS) < 0:
                    self.close()
        except (AttributeError, OSError):
            self.close()

    def names(self):
        if self.fd is None:
            return None
        names, lost = set(), False
        for _ in range(64):
            try:
                data = os.read(self.fd, 65536)
            except BlockingIOError:
                return None if lost else names
            except OSError:
                self.close()
                return None
            offset = 0
            while offset < len(data):
                _, mask, _, size = EVENT.unpack_from(data, offset)
                offset += EVENT.size
                name = os.fsdecode(data[offset:offset + size].split(b'\0', 1)[0])
                offset += size
                if not name:
                    lost = True
                    if mask & 0x00008c00:  # ignored, moved or deleted watch
                        self.close()
                        return None
                elif name.endswith('.json'):
                    names.add(name)
        return None  # Continuous arrivals can prevent a complete drain.

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class MessageIndex:
    """Read exact references or one inbox; the files remain authoritative.

    Warm opens use a certified directory stamp. An open reader also tracks
    changed names, including publications by older processes and fleet import.
    After a missed notification it inventories names and reopens only changed
    files. The database contains no bodies and can be deleted and rebuilt.
    """

    def __init__(self, store: post.Store, *, cache=None):
        self.store = store
        self.runtime = store.dir.parent.parent
        self.cache = cache if cache is not None else store.dir.parent / CACHE
        self._cache_path = os.fsdecode(self.cache)
        self.changes = None
        self.revision = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self.changes is not None:
            self.changes.close()

    def _open_lock(self):
        flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(self._cache_path + '.lock', flags, 0o600)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise OSError('post index lock is not a regular file')
        return fd

    def _connect(self):
        try:
            if not stat.S_ISREG(os.lstat(self.cache).st_mode):
                raise OSError('post index is not a regular file')
        except FileNotFoundError:
            if any(os.path.lexists(self._cache_path + suffix) for suffix in SQLITE_SIDECARS):
                raise _InvalidCache('post index sidecars without a database')
        return sqlite3.connect(self.cache, timeout=0.25)

    def _snapshot(self, db):
        schema = dict(db.execute('SELECT name, sql FROM sqlite_schema WHERE sql IS NOT NULL'))
        if not schema:
            for statement in SCHEMA.values():
                db.execute(statement)
        elif schema != SCHEMA:
            raise _InvalidCache('unexpected post index schema')

        rows = db.execute('SELECT singleton, revision, stamp, certified_at FROM snapshot LIMIT 2').fetchall()
        if not rows and db.execute('SELECT 1 FROM messages LIMIT 1').fetchone() is None:
            return 0, None, None
        if len(rows) != 1:
            raise _InvalidCache('missing or duplicate post index snapshot')
        singleton, revision, stamp, certified_at = rows[0]
        generation = STAMP.fullmatch(stamp) if isinstance(stamp, str) else None
        if singleton != 1 or not isinstance(revision, int) or not 1 <= revision < 2**63 - 1 or generation is None:
            raise _InvalidCache('invalid post index snapshot')
        stamp = tuple(map(int, generation.groups()))
        if certified_at is not None and (not isinstance(certified_at, int)
                or certified_at <= max(stamp[2:]) + post.POST_TIMESTAMP_TICK_NS):
            raise _InvalidCache('invalid post index certification')
        return revision, stamp, certified_at

    def _replace_cache(self):
        # The stable lock excludes live SQLite handles before we remove a
        # discarded database or its journals. Keep the lock in place.
        for suffix in ('', *SQLITE_SIDECARS):
            try:
                os.unlink(self._cache_path + suffix)
            except FileNotFoundError:
                pass

    def _raw(self, directory, name):
        path = self.store.dir / name
        try:
            data = post_dir._read_nofollow(directory, name, path)
        except (FileNotFoundError, post_dir.NotRegular):
            return None
        raw = post.Message.from_json(data.decode('utf-8'))
        if name != raw.id + '.json':
            raise ValueError('post index filename and id differ')
        return raw, data

    def _update(self, db, directory, names, *, inventory):
        previous = dict(db.execute('SELECT name, identity FROM messages')) if inventory else {}
        for name in names:
            if not isinstance(name, str) or not post_dir._plain_name(name) or not name.endswith('.json'):
                continue
            before = previous.pop(name, None)
            try:
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                info = None
            if info is not None and (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                identity = repr((*_stamp(info), info.st_size, info.st_mode))
                if identity == before:
                    continue
                entry = self._raw(directory, name)
                if entry is not None:
                    raw, _ = entry
                    db.execute('INSERT OR REPLACE INTO messages VALUES (?, ?, ?, ?, ?)',
                               (name, identity, raw.re, raw.to, raw.short))
                    continue
            db.execute('DELETE FROM messages WHERE name = ?', (name,))
        if previous:
            db.executemany('DELETE FROM messages WHERE name = ?', ((name,) for name in previous))

    def _refresh(self, db, directory):
        info = os.fstat(directory)
        identity = info.st_dev, info.st_ino
        if self.changes is None or self.changes.identity != identity or self.changes.fd is None:
            if self.changes is not None:
                self.changes.close()
            self.changes = _Changes(directory, identity)
            self.revision = None
        watched = self.changes.fd is not None
        revision, cached_stamp, certified_at = self._snapshot(db)
        reconcile = False
        for _ in range(3):
            started = time.time_ns()
            stamp = _stamp(os.fstat(directory))
            changed = self.changes.names()
            if watched and self.changes.fd is None:
                break
            if _stamp(os.fstat(directory)) != stamp:
                # An append after the drain can invalidate this generation.
                # Inventory on retry because the drain consumed names.
                reconcile = True
                continue
            old_enough = max(stamp[2:]) + post.POST_TIMESTAMP_TICK_NS < started <= time.time_ns()
            if (not reconcile and stamp == cached_stamp and certified_at is not None
                    and certified_at <= started and (changed == set() or not watched)):
                return revision
            if not watched and not old_enough:
                break
            observed = (not reconcile and changed is not None and self.revision == revision
                        and cached_stamp is not None and cached_stamp[:2] == stamp[:2])
            if observed and not changed and stamp == cached_stamp:
                if old_enough:
                    db.execute('UPDATE snapshot SET certified_at = ? WHERE singleton = 1', (started,))
                elif self.changes.names() != set() or _stamp(os.fstat(directory)) != stamp:
                    reconcile = True
                    continue
                return revision
            if observed and changed:
                self._update(db, directory, changed, inventory=False)
            else:
                with os.scandir(directory) as entries:
                    self._update(db, directory, (entry.name for entry in entries), inventory=True)
            # The watch also catches arrivals within one filesystem timestamp bucket.
            pending = self.changes.names()
            if _stamp(os.fstat(directory)) != stamp or (watched and pending != set()):
                reconcile = True
                continue
            db.execute('INSERT OR REPLACE INTO snapshot VALUES (1, ?, ?, ?)',
                       (revision + 1, repr(stamp), started if old_enough else None))
            return revision + 1
        raise IndexUnavailable('post index could not establish a stable store generation')

    def _read_cache(self, directory, search):
        try:
            with closing(self._connect()) as db, db:
                db.execute('BEGIN IMMEDIATE')
                revision = self._refresh(db, directory)
                result = search(db, directory)
        except sqlite3.DatabaseError as error:
            code = getattr(error, 'sqlite_errorcode', 0) & 0xff
            if code in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
                raise _InvalidCache('damaged post index') from error
            raise
        self.revision = revision
        return result

    def _query(self, search):
        try:
            descriptor = post_dir.open_directory(self.store.dir, create=False)
        except FileNotFoundError:
            return search(None, None)
        try:
            lock = self._open_lock()
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                try:
                    return self._read_cache(descriptor, search)
                except _InvalidCache:
                    self.revision = None
                # Recheck under the exclusive lock in case a peer repaired it.
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    return self._read_cache(descriptor, search)
                except _InvalidCache:
                    self._replace_cache()
                    return self._read_cache(descriptor, search)
            finally:
                os.close(lock)
        except (IndexUnavailable, _InvalidCache, OSError, sqlite3.Error, ValueError) as error:
            # Discovery may have consumed changes before a write rolled back.
            self.revision = None
            raise IndexUnavailable('post index unavailable') from error
        finally:
            os.close(descriptor)

    def _read(self, directory, names):
        messages = []
        for name in names:
            entry = self._raw(directory, name)
            if entry is not None:
                message, data = entry
                messages.append(fleet_post.local_view(self.runtime, message, data=data))
        return sorted(messages, key=lambda message: (message.at, message.id))

    def has_short(self, short):
        def search(db, directory):
            return db is not None and db.execute(
                'SELECT 1 FROM messages WHERE short = ? LIMIT 1', (short,)).fetchone() is not None

        return self._query(search)

    def _replies(self, db, directory, exact_id):
        rows = db.execute('SELECT name FROM messages WHERE ref = ?', (exact_id,))
        return [message for message in self._read(directory, (name for name, in rows)) if message.re == exact_id]

    def replies(self, exact_id):
        """Canonical replies to this exact full id, oldest first, including fleet imports."""
        def search(db, directory):
            if db is None:
                return []
            routes = forward_post.declared(self.runtime)
            return [self._recipient_view(db, directory, message, routes)
                    for message in self._replies(db, directory, exact_id)]

        return self._query(search)

    def get(self, exact_id):
        """One full message id, including context-only files; None means absent."""
        name = exact_id + '.json'
        if not post_dir._plain_name(name):
            return None

        def search(db, directory):
            if db is None or db.execute('SELECT 1 FROM messages WHERE name = ?', (name,)).fetchone() is None:
                return None
            messages = self._read(directory, (name,))
            if not messages:
                return None
            return self._recipient_view(db, directory, messages[0], forward_post.declared(self.runtime))

        return self._query(search)

    def _recipient_view(self, db, directory, message, routes):
        if message.obligation and message.to in routes:
            replies = self._replies(db, directory, message.id)
            closed = any(reply.kind == ('report' if message.kind == 'order' else 'answer')
                         and (message.kind != 'order' or reply.sender == message.to) for reply in replies)
            return forward_post.projected(self.runtime, message, open_obligation=not closed)
        return message

    def for_seat(self, seat):
        """One recipient's inbox, with the same route and context rules as Store.for_seat."""
        def search(db, directory):
            if db is None:
                return []
            routes = forward_post.declared(self.runtime)
            owner = fleet_post.owner(self.runtime)
            inputs = {seat, *(alias for alias, target in routes.items() if target == seat)}
            if owner is not None:
                inputs.update(f'{alias}@{owner}' for alias in tuple(inputs) if '@' not in alias)
            names = set()
            for address in inputs:
                rows = db.execute('SELECT name FROM messages WHERE recipient = ?', (address,))
                names.update(name for name, in rows)
            # A pinned route can change an inbox even after its declaration is removed.
            for path in post_dir.json_paths(self.store.dir.parent / 'forwarded'):
                route = forward_post.recorded(self.runtime, path.stem)
                if route is not None and route['to'] in inputs:
                    names.add(path.name)
            messages = (self._recipient_view(db, directory, message, routes)
                        for message in self._read(directory, names))
            return [m for m in messages if m.to == seat and not fleet_post.context_only(self.runtime, m.id)]

        return self._query(search)
