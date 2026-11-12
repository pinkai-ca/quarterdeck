"""Seat-local native folder trust; project contents never determine readiness."""

from pathlib import Path

from ..ship import ShipError


def check(home: Path, cwd: Path | None) -> None:
    """Draft checkpoint: native store schema must be measured before acceptance.

    --trust creates the native grant at launch. Until its serialization and path
    handling are established, do not invent TOML keys or accept an unknown grant.
    This checkpoint does not inspect project material or perform Git discovery.
    """
    path = home / "trusted_folders.toml"
    if path.is_symlink():
        raise ShipError(f"{path}: unexpected linked folder-trust state; retained for inspection")
    try:
        content = path.read_text()
    except FileNotFoundError:
        return
    except (OSError, UnicodeError):
        raise ShipError(f"{path}: folder-trust state unreadable; retained for inspection") from None
    if content.strip():
        raise ShipError(f"{path}: native trust-store format unproved; exact workspace grant checkpoint pending; retained")
