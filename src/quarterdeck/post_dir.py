"""Directory descriptor walk under runtime/post.

A link is refused by name, never followed and never removed. A missing parent is
created as a real directory and opened the same way. Fleet publication uses this
same walk: its private helpers are these functions, so a later rebase imports
them instead of keeping a second copy.

O_DIRECTORY|O_NOFOLLOW on a symlink is ENOTDIR, the same as a non-directory, so
the kind is the lstat and the open never follows.
"""

from __future__ import annotations

import ctypes
import errno
import locale
import os
from pathlib import Path
import secrets
import stat

from .ship import ShipError


_LINK_HELD = 'post link held at '
LINK_HELD = _LINK_HELD


class NotRegular(ShipError):
    """The path is not a regular file.

    The store listing skips this kind only when the store file itself is not regular.
    The same kind from an imports record stops the listing.
    """


class ReadLimit(ShipError):
    """The regular file exceeds the caller's bounded read."""


class CountLimit(ShipError):
    """The directory exceeds the caller's bounded entry count."""


def _plain_name(name):
    return (isinstance(name, str) and name not in ('', '.', '..')
            and '/' not in name and '\\' not in name and '\x00' not in name)


def _link_held(folder):
    raise ShipError(f'{_LINK_HELD}{folder}')


def _split_under_post(path):
    """The runtime/post directory and the parts below it, when path lies there."""
    parts = Path(path).parts
    for index in range(len(parts) - 1, 0, -1):
        if parts[index] == 'post' and parts[index - 1] == 'runtime':
            return Path(*parts[:index + 1]), parts[index + 1:]
    return None, ()


def under_post(path) -> bool:
    root, relative = _split_under_post(path)
    return root is not None and (Path(path) == root or bool(relative))


def _ensure_real_ancestors(path):
    """Create missing real directories through path. path is runtime, above post."""
    missing, current = [], Path(path)
    while True:
        try:
            current.lstat()
        except FileNotFoundError:
            missing.append(current)
            parent = current.parent
            if parent == current:
                break
            current = parent
            continue
        break
    for folder in reversed(missing):
        try:
            os.mkdir(folder)
        except FileExistsError:
            pass


def _directory_at(parent_fd, parent, name, *, create):
    """Open one real directory. A symlink is a hold and is left where it is."""
    if not _plain_name(name):
        raise ShipError(f'invalid post path component {name!r}')
    located = Path(parent) / name
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        if not create:
            raise
        try:
            os.mkdir(name, dir_fd=parent_fd)
        except FileExistsError:
            pass
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if stat.S_ISLNK(info.st_mode):
        _link_held(located)
    if not stat.S_ISDIR(info.st_mode):
        raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), str(located))
    try:
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                       dir_fd=parent_fd)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            try:
                raced = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except OSError:
                raise error
            if stat.S_ISLNK(raced.st_mode):
                _link_held(located)
        raise


def _open_post(root, *, create):
    if create:
        _ensure_real_ancestors(root.parent)
    runtime = os.open(root.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        return _directory_at(runtime, root.parent, 'post', create=create)
    finally:
        os.close(runtime)


def _descend(root, parts, *, create):
    descriptor = _open_post(root, create=create)
    walked = root
    try:
        for name in parts:
            child = _directory_at(descriptor, walked, name, create=create)
            os.close(descriptor)
            descriptor = child
            walked = walked / name
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def open_directory(path, *, create):
    """A held descriptor for one real directory under runtime/post. The caller closes it.

    A path that is not under runtime/post keeps an ordinary open. The fixtures and the
    other trees have no post link to refuse, and the walk does not apply to them.
    """
    path = Path(path)
    root, relative = _split_under_post(path)
    if root is None or (path != root and not relative):
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    if path == root:
        return _open_post(root, create=create)
    return _descend(root, relative, create=create)


def _open_parent(path, *, create):
    root, rel = _split_under_post(path)
    if not rel:
        raise ShipError(f'invalid post path {path}')
    directories, name = rel[:-1], rel[-1]
    if not _plain_name(name):
        raise ShipError(f'invalid post path component {name!r}')
    return _descend(root, directories, create=create), name


def _rename_noreplace(source, destination, source_fd, destination_fd):
    """renameat2 with RENAME_NOREPLACE. An existing name is left where it is."""
    library = ctypes.CDLL(None, use_errno=True)
    rename = getattr(library, 'renameat2', None)
    if rename is None:
        raise OSError(errno.ENOSYS, 'atomic no-clobber rename is unavailable')
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(source_fd, os.fsencode(source), destination_fd, os.fsencode(destination), 1) != 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number))


CHANGED = 'post path changed during its read'


def _relative(path) -> str:
    """The path below runtime/post, for a refusal that names its file."""
    root, parts = _split_under_post(path)
    return '/'.join(parts) if root is not None and parts else Path(path).name


def _read_nofollow(parent_fd, name, path, limit=None):
    """Bytes of one regular file in an already opened directory. A link is a hold.

    Anything that is not a regular file is refused from the lstat, before the open.
    ``limit`` refuses a larger file before the open as well. A whole read whose
    file was atomically replaced, during the read or after it, starts over, at
    most three reads in all: the replacement moves only the old file's change
    time. A torn read, or an in-place change of the bytes, size or modification
    time of the file at the name, is still refused. The refusal names the path
    below runtime/post, never the contents.
    """
    stamp = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
    content = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
    changed = f'{CHANGED}: {_relative(path)}'  # the name, never the contents, so the next refusal points at its writer
    for attempt in range(3):
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            _link_held(path)
        if not stat.S_ISREG(info.st_mode):
            raise NotRegular('post path is not a regular file')
        if limit is not None and info.st_size > limit:
            raise ReadLimit('post path exceeds its read limit')
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                                 dir_fd=parent_fd)
        except OSError as error:
            if error.errno == errno.ELOOP:
                _link_held(path)
            raise
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise NotRegular('post path is not a regular file')
            if limit is not None and before.st_size > limit:
                raise ReadLimit('post path exceeds its read limit')
            with os.fdopen(descriptor, 'rb', closefd=False) as stream:
                data = stream.read() if limit is None else stream.read(limit)
            after = os.fstat(descriptor)
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if limit is not None and (after.st_size > limit or current.st_size > limit):
                raise ReadLimit('post path exceeds its read limit')
            # Torn: the bytes read are not one whole version of the file. Refused at once, as is a name that is
            # no longer a regular file or is over the limit.
            if (len(data) != after.st_size or content(before) != content(after)
                    or not stat.S_ISREG(current.st_mode)):
                raise ShipError(changed)
            if stamp(before) == stamp(after) == stamp(current):
                return data
            # The same file still at the name with other bytes or times: an in-place change. Refused.
            if ((after.st_dev, after.st_ino) == (current.st_dev, current.st_ino)
                    and content(after) != content(current)) or attempt == 2:
                raise ShipError(changed)
            # Whole, but replaced: a complete file renamed over the name during or after the read moves the old
            # file's change time as it loses its name, which the name may still show for an instant. Read again.
        finally:
            os.close(descriptor)


def _exists_nofollow(path):
    """Whether the final name exists under runtime/post. A link on the way is a hold."""
    if _split_under_post(path)[0] is None:
        return Path(path).exists()
    try:
        parent, name = _open_parent(path, create=False)
    except FileNotFoundError:
        return False
    try:
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(info.st_mode):
            _link_held(path)
        return True
    finally:
        os.close(parent)


def exists(path) -> bool:
    return _exists_nofollow(path)


def mode(path) -> int | None:
    """The final name's mode, without following it; None when missing.

    A link at the final name is reported as a link, not held, so a caller can hold it by
    its own reason. A link on the way to it is a hold, as everywhere under runtime/post.
    """
    path = Path(path)
    if not under_post(path):
        try:
            return path.lstat().st_mode
        except FileNotFoundError:
            return None
    try:
        parent, name = _open_parent(path, create=False)
    except FileNotFoundError:
        return None
    try:
        try:
            return os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode
        except FileNotFoundError:
            return None
    finally:
        os.close(parent)


def gone(path) -> bool:
    """Positive evidence that a listed name disappeared; unreadable is not gone."""
    try:
        return mode(path) is None
    except (OSError, ShipError):
        return False


def _json_under(directory):
    """*.json names in a runtime/post directory, without following a link to reach it."""
    return json_paths(directory)


def ensure_directory(path) -> None:
    """Create a real directory under runtime/post. A link on the way is a hold."""
    path = Path(path)
    if not under_post(path):
        path.mkdir(parents=True, exist_ok=True)
        return
    descriptor = open_directory(path, create=True)
    os.close(descriptor)


def exists_dir(path) -> bool:
    """True when path is a real directory. Missing is false. A link is a hold."""
    path = Path(path)
    if not under_post(path):
        return path.is_dir()
    try:
        descriptor = open_directory(path, create=False)
    except FileNotFoundError:
        return False
    os.close(descriptor)
    return True


def list_children(directory, *, limit: int | None = None):
    """(name, mode) for each child, without following a link to the directory or the child."""
    directory = Path(directory)
    if limit is not None and limit < 0:
        raise ValueError("directory limit must be nonnegative")
    if not under_post(directory):
        found = []
        seen = 0
        with os.scandir(directory) as scan:
            for entry in scan:
                if entry.name in ('.', '..'):
                    continue
                seen += 1
                if limit is not None and seen > limit:
                    raise CountLimit('directory exceeds entry limit')
                try:
                    kind = entry.stat(follow_symlinks=True).st_mode
                except FileNotFoundError:
                    if not gone(directory / entry.name):
                        raise
                    continue
                found.append((entry.name, kind))
        return found
    descriptor = open_directory(directory, create=False)
    try:
        found = []
        seen = 0
        with os.scandir(f'/proc/self/fd/{descriptor}') as scan:
            for entry in scan:
                if entry.name in ('.', '..'):
                    continue
                seen += 1
                if limit is not None and seen > limit:
                    raise CountLimit('directory exceeds entry limit')
                try:
                    kind = entry.stat(follow_symlinks=False).st_mode
                except FileNotFoundError:
                    continue
                found.append((entry.name, kind))
        return found
    finally:
        os.close(descriptor)


def json_paths(directory):
    """Real *.json files in one post directory. A link at the directory or a file is a hold."""
    directory = Path(directory)
    try:
        children = list_children(directory)
    except FileNotFoundError:
        return []
    paths = []
    for name, mode in sorted(children):
        if not name.endswith('.json'):
            continue
        if stat.S_ISLNK(mode):
            if gone(directory / name):
                continue
            _link_held(directory / name)
        paths.append(directory / name)
    return paths


def directory_names(directory):
    """Real child directory names. A link at this directory or a child is a hold."""
    directory = Path(directory)
    try:
        children = list_children(directory)
    except FileNotFoundError:
        return []
    names = []
    for name, mode in children:
        if stat.S_ISLNK(mode):
            if gone(directory / name):
                continue
            _link_held(directory / name)
        if stat.S_ISDIR(mode):
            names.append(name)
    return names


def read_bytes(path, limit: int | None = None) -> bytes:
    """The file's bytes. ``limit`` refuses a larger regular file before reading it."""
    path = Path(path)
    if not under_post(path):
        if limit is None:
            return path.read_bytes()
        with path.open('rb') as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ReadLimit('post path exceeds its read limit')
        return data
    parent, name = _open_parent(path, create=False)
    try:
        return _read_nofollow(parent, name, path, limit)
    finally:
        os.close(parent)


def read_text(path, *, encoding: str | None = None, limit: int | None = None) -> str:
    path = Path(path)
    if not under_post(path) and limit is None:
        return path.read_text() if encoding is None else path.read_text(encoding=encoding)
    return read_bytes(path, limit=limit).decode(encoding or locale.getpreferredencoding(False))


def replace_bytes(path, data: bytes, *, mode: int = 0o666) -> None:
    """Replace whole without a storage sync, through the held directory walk."""
    _replace_bytes(path, data, law_record=False, mode=mode)


def _replace_law_bytes(path, data: bytes) -> None:
    """The initial attempt's fixed law writer; never an ordinary write option."""
    if not under_post(path):
        raise ValueError('law post record is outside runtime/post')
    _replace_bytes(path, data, law_record=True, mode=0o666)


def _replace_bytes(path, data: bytes, *, law_record: bool, mode: int) -> None:
    """Put data at path through a random exclusive temporary in the held parent directory.

    A link at the parent, or at the final name when that name is looked up, is a
    hold and is left in place. The temporary is then replaced onto the final name.
    """
    path = Path(path)
    if not under_post(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f'.{path.name}.{secrets.token_hex(4)}.tmp')
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, mode)
        try:
            try:
                view = memoryview(data)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError('post write incomplete')
                    view = view[written:]
            finally:
                os.close(descriptor)
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return
    parent, name = _open_parent(path, create=True)
    temporary = None
    try:
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        if info is not None and stat.S_ISLNK(info.st_mode):
            _link_held(path)
        temporary = f'.{name}.{secrets.token_hex(4)}.tmp'
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             mode, dir_fd=parent)
        try:
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError('post write incomplete')
                view = view[written:]
            if law_record:
                os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        temporary = None
        if law_record:
            os.fsync(parent)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
        os.close(parent)


def open_lock(path, *, mode: int = 0o666, extra: int = 0) -> int:
    """A read-write descriptor for a regular lock file. The caller flocks and closes it.

    A link at the parent or at the lock name is a hold. The descriptor does not follow.
    """
    path = Path(path)
    if not under_post(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        return os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | extra, mode)
    parent, name = _open_parent(path, create=True)
    try:
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        if info is not None and stat.S_ISLNK(info.st_mode):
            _link_held(path)
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | extra
        try:
            return os.open(name, flags, mode, dir_fd=parent)
        except OSError as error:
            if error.errno == errno.ELOOP:
                _link_held(path)
            raise
    finally:
        os.close(parent)


def remove(path) -> None:
    """Unlink one real file. A link is a hold and is left in place. Missing raises FileNotFoundError."""
    path = Path(path)
    if not under_post(path):
        path.unlink()
        return
    parent, name = _open_parent(path, create=False)
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            _link_held(path)
        os.unlink(name, dir_fd=parent)
    finally:
        os.close(parent)


def rename(source, destination) -> None:
    """Rename one real file onto an absent name. A link at either name is a hold.

    The rename itself refuses to replace an existing name.
    """
    source, destination = Path(source), Path(destination)
    if not under_post(source) and not under_post(destination):
        try:
            _rename_noreplace(source, destination, -100, -100)  # AT_FDCWD
        except FileExistsError:
            raise ShipError('post destination already exists; both names retained') from None
        return
    source_parent, source_name = _open_parent(source, create=False)
    try:
        dest_parent, dest_name = _open_parent(destination, create=True)
        try:
            info = os.stat(source_name, dir_fd=source_parent, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                _link_held(source)
            try:
                _rename_noreplace(source_name, dest_name, source_parent, dest_parent)
            except FileExistsError:
                try:
                    dest_info = os.stat(dest_name, dir_fd=dest_parent, follow_symlinks=False)
                except FileNotFoundError:
                    dest_info = None
                if dest_info is not None and stat.S_ISLNK(dest_info.st_mode):
                    _link_held(destination)
                raise ShipError('post destination already exists; both names retained') from None
        finally:
            os.close(dest_parent)
    finally:
        os.close(source_parent)
