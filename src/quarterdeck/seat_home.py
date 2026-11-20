"""A seat's home is its owner's alone: runtime/seats/<seat>/home, mode 0700, for every program.

The home holds a seat's settings, its credential link and its conversations. Once the home is 0700, nothing beneath it
is reachable by another user, so the home alone is the barrier; what a program makes inside it is left as it makes it.

The home is made 0700 wherever the seat code first makes it (``seat_files`` makes the name ``home`` directly under a
seat folder so, and so does ``owner_only``). Every launch, first, relaunch, fresh start and resume alike, then calls
``owner_only`` before anything in the home is built or read: the home is proved an owned real directory by name
beneath its opened parent, opened without following a link, its mode set with ``fchmod`` on that descriptor (never
``chmod`` by path) and read back with ``fstat``. A widened mode is mended and the launch goes ahead; a link, a folder
of another user, or a mode that will not hold refuses the launch, and nothing behind a link is changed. A launch that
expects the home already built (a resume, or a program's launch right after its own build) refuses an absent home in one line that
names it. The parents of the home are resolved by path: they are runtime/seats/<seat>, the framework's own.

No record reader needs this: it is the launch's, and lives outside every reader's stamp set.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from .ship import ShipError

MODE = 0o700
_OPEN = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY | os.O_CLOEXEC


class Absent(ShipError):
    """A launch that expects a built home found none: nothing is launched."""


def absent(home: Path) -> Absent:
    return Absent(f"{home}: the seat home is absent, and this launch expects it already built; "
                  "nothing was launched")


def _not_owned(home: Path, detail: str = '') -> ShipError:
    return ShipError(f"{home}: the seat home is not an owned real directory{detail}; "
                     "nothing was launched")


def require_built(home: Path) -> None:
    """Read-only: refuse an absent home in one line that names it. A link or a folder of another user is left to
    ``owner_only``, which the launch itself calls."""
    try:
        Path(home).lstat()
    except FileNotFoundError:
        raise absent(Path(home)) from None


def owner_only(home: Path, *, built: bool = False) -> None:
    """Prove ``home`` an owned real directory and mend it to 0700 through its own descriptor.

    ``built``: the launch expects the home already built, so an absent one refuses (``Absent``); otherwise an absent
    home is made 0700 here. A widened mode is mended, not refused; a link, a non-directory, a folder another user
    owns, a folder swapped between the proof and the open, or a mode that is still not 0700 refuses, and changes
    nothing."""
    home = Path(home)
    try:
        parent = os.open(home.parent, _OPEN)
    except FileNotFoundError:
        if built:
            raise absent(home) from None
        from . import seat_files
        seat_files.make_folder(home.parent)  # the seat folder, the framework's own; never through a link
        parent = os.open(home.parent, _OPEN)
    try:
        try:
            info = os.stat(home.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            if built:
                raise absent(home) from None
            try:
                os.mkdir(home.name, MODE, dir_fd=parent)
            except FileExistsError:
                pass  # made meanwhile: proved below like any other
            info = os.stat(home.name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise _not_owned(home)
        try:
            descriptor = os.open(home.name, _OPEN, dir_fd=parent)
        except OSError as error:
            raise _not_owned(home, f" ({error.strerror})") from None
        try:
            opened = os.fstat(descriptor)
            if ((opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                    or not stat.S_ISDIR(opened.st_mode) or opened.st_uid != os.getuid()):
                raise _not_owned(home, " (it changed between its proof and its opening)")
            if stat.S_IMODE(opened.st_mode) != MODE:
                os.fchmod(descriptor, MODE)
            after = os.fstat(descriptor)
            if stat.S_IMODE(after.st_mode) != MODE or after.st_uid != os.getuid():
                raise ShipError(f"{home}: the seat home is still not mode 0700 for its owner alone; "
                                "nothing was launched")
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)
