"""Writes into a seat's folder that never follow a link: runtime/seats/<seat>/, its home included.

A jailed seat can write its whole folder, so any name there, or any folder on the way to it, may be a link the jail
planted to a file of the operator. Every write the service or qd makes there goes through these functions (and
``durable.replace_file``, which uses them for such a path):

- the walk from the seat folder down opens each folder with O_NOFOLLOW; a link on the way is refused, and nothing is
  written through it (``LinkRefused``); a folder it makes is made as ``mkdir -p`` would, but the seat's home itself
  (runtime/seats/<seat>/home) is made 0700, its owner's alone (``seat_home``);
- a file is written to a temporary name created there with O_CREAT|O_EXCL|O_NOFOLLOW, then renamed over the name:
  a rename replaces a planted link, never follows it;
- a link (a credential's) is made under a temporary name and renamed over the name the same way;
- an append opens the name with O_NOFOLLOW: a planted link is refused;
- a read that feeds a write (a home file merged and written back) is made with O_NOFOLLOW: a link reads as nothing,
  so no operator's file is ever copied into the jail's reach.

A path outside any seat folder keeps the ordinary behaviour. The seat folder itself is a mount point inside its
jail and the folders above it are the runtime's own, so the walk starts there.
"""

from __future__ import annotations

import errno
import os
import secrets
import stat
import sys
from pathlib import Path

from .ship import ShipError

DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
# A read that feeds a write takes at most this much. The largest such file measured on the live ship on 15 November
# was a harness's own state file in a seat's home, about 160 KB; conversations.owned, the next, about 100 KB, grows by
# one line (about 150 bytes) per record a seat owns. 4 MiB is 25 times the first and some 28,000 records of the second, and small enough
# that a file its jail grew without bound is refused before it is read into memory or copied anywhere.
READ_LIMIT = 4 * 1024 * 1024


class LinkRefused(ShipError):
    """A link where the seat folder holds a folder or a file the service writes: nothing is written through it."""


class TooLarge(ShipError):
    """A file read to feed a write is larger than READ_LIMIT: it is not read, and nothing is written from it."""


def _bounded(stream, path) -> bytes:
    data = stream.read(READ_LIMIT + 1)
    if len(data) > READ_LIMIT:
        raise TooLarge(f'{path}: larger than {READ_LIMIT // (1024 * 1024)} MiB; not read, and nothing written from it')
    return data


def _announce(path: Path) -> None:
    """Name, as an audit event, the full path of a write this module opens by name within a folder's handle: the
    ``open`` event that follows carries the name alone. Paths are logical, and no link is followed on the way, so
    the path named is the file written."""
    sys.audit('quarterdeck.seat_files.open', os.fspath(path))


def seat_root(path) -> tuple[Path, tuple[str, ...]] | None:
    """(runtime/seats/<seat>, the parts below it) when ``path`` lies in a seat's folder; else None."""
    parts = Path(path).parts
    for index in range(len(parts) - 2, 0, -1):
        if parts[index] == 'seats' and parts[index - 1] == 'runtime':
            return Path(*parts[:index + 2]), tuple(parts[index + 2:])
    return None


def under_seat(path) -> bool:
    found = seat_root(path)
    return found is not None and bool(found[1])


def _walk(root: Path, parts: tuple[str, ...], *, create: bool) -> int:
    """A descriptor for the folder ``root/parts``, each step opened without following a link; the caller closes it."""
    try:
        descriptor = os.open(root, DIRECTORY)
    except FileNotFoundError:
        if not create:
            raise
        root.mkdir(parents=True, exist_ok=True)  # the seat folder and above: the runtime's own, never the jail's
        descriptor = os.open(root, DIRECTORY)
    walked = root
    try:
        for index, name in enumerate(parts):
            if name in ('', '.', '..'):
                raise LinkRefused(f'{walked / name}: not a plain name; nothing written')
            walked = walked / name
            try:
                child = os.open(name, DIRECTORY, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    # A seat's home is its owner's alone from the moment it is made (seat_home); a mkdir mode
                    # only ever narrows by the umask, so the home is made 0700 whatever the umask.
                    os.mkdir(name, 0o700 if (index, name) == (0, 'home') else 0o777, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(name, DIRECTORY, dir_fd=descriptor)
            except OSError as error:
                if error.errno in (errno.ELOOP, errno.ENOTDIR):
                    try:
                        if stat.S_ISLNK(os.stat(name, dir_fd=descriptor, follow_symlinks=False).st_mode):
                            raise LinkRefused(f'{walked}: a link where a folder belongs in the seat folder; '
                                              'nothing written through it') from None
                    except FileNotFoundError:
                        pass
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def make_folder(path, *, mode: int = 0o777) -> None:
    """The folder ``path`` and every folder on the way to it, made as ``mkdir -p`` would but never through a link:
    in a seat's folder each step is walked without following one (a link where a folder belongs refuses, and nothing
    is made beyond it). Elsewhere, an ordinary ``mkdir -p``."""
    found = seat_root(path)
    if found is None:
        Path(path).mkdir(mode=mode, parents=True, exist_ok=True)
        return
    root, parts = found
    os.close(_walk(root, parts, create=True))


def parent(path, *, create: bool = True) -> tuple[int, str] | None:
    """(a descriptor for the folder holding ``path``, its name) for a path in a seat folder; None elsewhere."""
    found = seat_root(path)
    if found is None or not found[1]:
        return None
    root, parts = found
    return _walk(root, parts[:-1], create=create), parts[-1]


def write_bytes(path, data: bytes, *, mode: int = 0o666, times: tuple[int, int] | None = None) -> None:
    """``data`` at ``path`` whole: a temporary made with O_EXCL|O_NOFOLLOW, renamed over the name.

    ``times``: (atime_ns, mtime_ns) the file is given before it takes the name (a credential copy reads as fresh as
    its source)."""
    _write(path, data, mode=mode, times=times, law_record=False)


def _replace_law_bytes(path, data: bytes) -> None:
    """The law records' forced write (durable._replace_law_record) in a seat's folder: the same walk, synced."""
    _write(path, data, mode=0o666, times=None, law_record=True)


def _write(path, data: bytes, *, mode: int, times, law_record: bool) -> None:
    sync = law_record
    located = parent(path)
    if located is None:
        _plain_write(Path(path), data, mode=mode, times=times)
        return
    folder, name = located
    temporary = f'.{name}.{secrets.token_hex(4)}.tmp'
    try:
        _announce(Path(path).with_name(temporary))
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode,
                             dir_fd=folder)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(descriptor, view):]
            if mode != 0o666:
                os.fchmod(descriptor, mode)
            if times is not None:
                os.utime(descriptor, ns=times)
            if sync:
                os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.replace(temporary, name, src_dir_fd=folder, dst_dir_fd=folder)
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=folder)
            except FileNotFoundError:
                pass  # renamed already: the name holds the whole new file
            raise
        if sync:
            os.fsync(folder)  # the rename lasts a power cut
    finally:
        os.close(folder)


def write_bytes_at(directory: int, name: str, data: bytes, *, mode: int, times, before_replace) -> None:
    """Atomically install into an already-open directory, checking the caller's binding before rename."""
    _plain_write(Path(name), data, mode=mode, times=times, directory=directory, before_replace=before_replace)


def _plain_write(path: Path, data: bytes, *, mode: int, times, directory=None, before_replace=None) -> None:
    temporary = path.with_name(f'.{path.name}.{secrets.token_hex(4)}.tmp')
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(temporary, flags, mode, dir_fd=directory)
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(descriptor, view):]
            if mode != 0o666:
                os.fchmod(descriptor, mode)
            if times is not None:
                os.utime(descriptor, ns=times)
        finally:
            os.close(descriptor)
        if before_replace is not None:
            before_replace()
        os.replace(temporary, path, src_dir_fd=directory, dst_dir_fd=directory)
    except BaseException:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        raise


def write_text(path, text: str, *, encoding: str = 'utf-8', mode: int = 0o666) -> None:
    write_bytes(path, text.encode(encoding), mode=mode)


def copy(source, path, *, mode: int = 0o600) -> None:
    """A copy of ``source`` (outside the seat folder: a pool's file) at ``path``, bytes and times kept."""
    source = Path(source)
    info = source.stat()
    write_bytes(path, source.read_bytes(), mode=mode, times=(info.st_atime_ns, info.st_mtime_ns))


def symlink(path, target) -> None:
    """``path`` made a link to ``target``: made under a temporary name and renamed over the name, never followed."""
    located = parent(path)
    if located is None:
        path = Path(path)
        temporary = path.with_name(f'.{path.name}.{secrets.token_hex(4)}.link')
        os.symlink(target, temporary)
        try:
            os.replace(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return
    folder, name = located
    temporary = f'.{name}.{secrets.token_hex(4)}.link'
    try:
        os.symlink(target, temporary, dir_fd=folder)
        try:
            os.replace(temporary, name, src_dir_fd=folder, dst_dir_fd=folder)
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=folder)
            except FileNotFoundError:
                pass  # renamed already: the name holds the whole new file
            raise
    finally:
        os.close(folder)


def unlink(path, *, missing_ok: bool = True) -> None:
    """Remove the name itself (a link is removed, never what it points to), reached without following a link."""
    try:
        located = parent(path, create=False) if under_seat(path) else None
    except FileNotFoundError:
        if missing_ok:
            return
        raise
    if located is None:
        Path(path).unlink(missing_ok=missing_ok)
        return
    folder, name = located
    try:
        os.unlink(name, dir_fd=folder)
    except FileNotFoundError:
        if not missing_ok:
            raise
    finally:
        os.close(folder)


def open_append(path, mode: str = 'a', *, encoding: str = 'utf-8'):
    """A file object appending to ``path``; a link at the name, or on the way, is refused."""
    flags = os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | (os.O_RDWR if '+' in mode else os.O_WRONLY)
    located = parent(path)
    try:
        if located is None:
            descriptor = os.open(path, flags, 0o666)
        else:
            folder, name = located
            try:
                _announce(Path(path))
                descriptor = os.open(name, flags, 0o666, dir_fd=folder)
            finally:
                os.close(folder)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise LinkRefused(f'{path}: a link where the service appends; nothing written through it') from None
        raise
    return os.fdopen(descriptor, mode, encoding=encoding)


def read_with_times(path) -> tuple[bytes, tuple[int, int]] | None:
    """The bytes at ``path`` and their (atime_ns, mtime_ns), read without following a link; None for a link or none."""
    try:
        located = parent(path, create=False) if under_seat(path) else None
    except FileNotFoundError:
        return None
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    try:
        if located is None:
            descriptor = os.open(path, flags)
        else:
            folder, name = located
            try:
                descriptor = os.open(name, flags, dir_fd=folder)
            finally:
                os.close(folder)
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno == errno.ELOOP:
            return None
        raise
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            return None
        return _bounded(stream, path), (info.st_atime_ns, info.st_mtime_ns)


def read_text(path, *, encoding: str = 'utf-8') -> str | None:
    """The text at ``path`` read without following a link: None when there is none, or when a link stands there."""
    try:
        located = parent(path, create=False) if under_seat(path) else None
    except FileNotFoundError:
        return None
    try:
        if located is None:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        else:
            folder, name = located
            try:
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=folder)
            finally:
                os.close(folder)
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno == errno.ELOOP:
            return None
        raise
    with os.fdopen(descriptor, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            return None
        return _bounded(stream, path).decode(encoding)
