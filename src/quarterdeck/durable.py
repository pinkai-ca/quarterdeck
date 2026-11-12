"""Whole-file writes that survive a power cut: a reader finds the old file or the complete new one.

A file renamed into place before its data reaches the disk can come back empty
after a power loss (delayed allocation), and the rename itself lasts only once
its folder is synced. So the new text goes to a temporary beside the file, is
synced, is renamed over the name, and the folder is synced last.
"""

from __future__ import annotations

import locale
import os
import secrets
from pathlib import Path


def replace_file(path: Path, text: str, *, encoding: str | None = None) -> None:
    """Put ``text`` at ``path`` whole, as ``Path.write_text`` would encode it unless told otherwise, durably."""
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
    descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
