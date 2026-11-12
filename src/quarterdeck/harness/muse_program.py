"""Locate the installed Muse binary without executing its updating launcher."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil

from ..ship import ShipError

RELEASE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+-R[0-9]+(?:\.[0-9]+)?")


def binary(program: str = "muse", *, path: str | None = None) -> Path:
    """Read the install marker and return its executable; never run any command.

    The caller supplies the same selected-program PATH as ordinary Seat launch.
    Marker text identifies a candidate, not a measured or supported version.
    """
    selected = shutil.which(program, path=path)
    if selected is None:
        raise ShipError("Muse is not on the selected program PATH; install it before launch")
    try:
        directory = Path(selected).resolve(strict=True).parent
        with (directory / ".muse-version").open("rb") as stream:
            raw = stream.read(129)
        if len(raw) > 128:
            raise ValueError
        release = raw.decode("ascii").strip()
        if RELEASE.fullmatch(release) is None:
            raise ValueError
        candidate = directory / f"muse-bin-{release}"
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise ValueError
        resolved = candidate.resolve(strict=True)
        if resolved.name != candidate.name:
            raise ValueError
        return resolved
    except (OSError, ValueError, RuntimeError):
        raise ShipError("Muse install marker or its versioned binary is unavailable; repair the installation before launch") from None
