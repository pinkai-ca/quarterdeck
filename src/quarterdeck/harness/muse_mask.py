"""Muse's fixed write and auth-fallback paths, bound to seat-owned directories.

The host stays visible and writable. This is independent of the endpoint jail:
Muse ignores HOME for its session-name authority and TMPDIR for its attach root.
Native proof of the generated command belongs to independent validation.
"""

from __future__ import annotations

import os
from pathlib import Path
import pwd
import stat

from ..ship import ShipError

PROGRAM = Path("/usr/bin/bwrap")


def authority_target() -> Path:
    """The native passwd-home path, never the caller's HOME environment value."""
    try:
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError):
        raise ShipError("Muse passwd home is unavailable; cannot plan its fixed-path mask") from None
    if not home.is_absolute():
        raise ShipError("Muse passwd home is not absolute; cannot plan its fixed-path mask")
    return home / ".local" / "share" / "muse"


def config_target() -> Path:
    return authority_target().parents[2] / ".config" / "muse"


def real_directory(path: Path, *, missing: bool = False) -> None:
    for parent in reversed((path, *path.parents)):
        try:
            mode = parent.lstat().st_mode
        except FileNotFoundError:
            if missing:
                continue
            raise ShipError(f"{parent}: Muse mask directory is missing") from None
        except OSError:
            raise ShipError(f"{parent}: Muse mask directory is unreadable") from None
        if not stat.S_ISDIR(mode):
            raise ShipError(f"{parent}: Muse mask needs a real directory")


def preflight(home: Path | None = None) -> None:
    if not PROGRAM.is_file() or not os.access(PROGRAM, os.X_OK):
        raise ShipError("Muse requires the system /usr/bin/bwrap for its fixed-path HOME mask")
    # Do not let bwrap create a mount destination in an operator directory.
    real_directory(authority_target())
    real_directory(config_target())
    if home is not None:
        if not home.is_absolute() or home == Path("/tmp") or Path("/tmp") in home.parents:
            raise ShipError("Muse home must be absolute and outside the /tmp path it masks")
        real_directory(home, missing=True)


def prepare(home: Path) -> None:
    preflight(home)
    for name in ("mask", "mask/passwd-muse", "mask/passwd-config", "mask/tmp"):
        path = home / name
        real_directory(path, missing=True)
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = path.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ShipError(f"{path}: Muse mask directory must be owned and mode 0700")


def prefix(home: Path, cwd: Path, *, offline: bool = False) -> tuple[str, ...]:
    """One mask builder; explicit offline proof adds a network namespace here."""
    preflight(home)
    for name in ("mask/passwd-muse", "mask/passwd-config", "mask/tmp"):
        path = home / name
        real_directory(path)
        info = path.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ShipError(f"{path}: Muse mask directory must be owned and mode 0700")
    return (str(PROGRAM), "--die-with-parent", "--dev-bind", "/", "/",
            "--bind", str(home / "mask/passwd-muse"), str(authority_target()),
            "--bind", str(home / "mask/passwd-config"), str(config_target()),
            "--bind", str(home / "mask/tmp"), "/tmp",
            *(("--unshare-net",) if offline else ()), "--chdir", str(cwd), "--")


def command(home: Path, cwd: Path, argv: tuple[str, ...], *, offline: bool = False) -> tuple[str, ...]:
    """Plan only; caller owns preparation. Nothing executes here."""
    return (*prefix(home, cwd, offline=offline), *argv)


def native_argv(argv) -> tuple[str, ...] | None:
    """Recognize the adapter's exact wrapper shape in saved launch metadata."""
    if (len(argv) < 18 or tuple(argv[:5]) != (str(PROGRAM), "--die-with-parent", "--dev-bind", "/", "/")
            or argv[5] != "--bind" or argv[8] != "--bind" or argv[11] != "--bind" or argv[13] != "/tmp"):
        return None
    source, config, temporary = Path(argv[6]), Path(argv[9]), Path(argv[12])
    if (source.name != "passwd-muse" or source.parent.name != "mask"
            or config != source.parent / "passwd-config"
            or temporary != source.parent / "tmp"
            or Path(argv[7]) != authority_target() or Path(argv[10]) != config_target()):
        return None
    tail = 15 if argv[14] == "--unshare-net" else 14
    if len(argv) <= tail + 3 or argv[tail] != "--chdir" or argv[tail + 2] != "--":
        return None
    return tuple(argv[tail + 3:])
