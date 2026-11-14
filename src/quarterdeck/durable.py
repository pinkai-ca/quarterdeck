"""Whole-file replacement without forcing writes to storage.

The temporary is closed before rename, so process interruption leaves the old
file or the complete new one. A power cut may lose recent writes; the operating
system decides when they reach storage. Ordinary replacements request no sync;
the refusal quarantine and compaction transition record retain their barriers.
"""

from __future__ import annotations

import locale
import os
import secrets
from pathlib import Path


def replace_file(path: Path, text: str, *, encoding: str | None = None) -> None:
    """Put ``text`` at ``path`` whole, as ``Path.write_text`` would encode it unless told otherwise, without a storage sync."""
    data = text.encode(encoding or locale.getpreferredencoding(False))
    # A name of its own, so two writers never share (or rename away) one temporary;
    # it never ends in a reader's suffix, and a cut can only leave it unread beside the file.
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o666)
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(descriptor, view):]
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


# The refusal quarantine and compaction transition record alone keep these
# barriers: context-is-cache requires their claims to survive a power cut.
def _replace_law_record(path: Path, text: str, *, encoding: str | None = None) -> None:
    """Put ``text`` at ``path`` whole, as ``Path.write_text`` would encode it unless told otherwise, durably."""
    data = text.encode(encoding or locale.getpreferredencoding(False))
    from .post_dir import replace_bytes, under_post
    if under_post(path):
        # The same exclusive temporary and rename, opened from the post directory
        # so a link under runtime/post is a hold and is not followed. The folder
        # sync stays here, where a caller that records durability can see it.
        replace_bytes(path, data, sync=True)
        sync_folder(path.parent)
        return
    # A name of its own, so two writers never share (or rename away) one temporary;
    # it never ends in a reader's suffix, and a cut can only leave it unread beside the file.
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o666)
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(descriptor, view):]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    sync_folder(path.parent)


def sync_folder(folder: Path) -> None:
    """Make the folder's entries, such as a rename into it, last a power cut."""
    from .post_dir import open_directory, under_post
    if under_post(folder):
        descriptor = open_directory(folder, create=False)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return
    descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
