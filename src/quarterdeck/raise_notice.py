"""Every raise is seen: a crew started by qd raise stays on the status line until the desk has seen it.

``qd raise`` posts an order to the office's desk before it starts a crew, and records it here. The
order stays open until the desk reports on it (law: open-until-closed); while it is open, every
status line of the office names the raise (law: nothing-waits-silently). The desk clears it with an
ordinary report on that order. Nothing here starts, stops or writes a ship.
"""
from __future__ import annotations

import json
from pathlib import Path

SUBJECT = "A new ship was raised"


def folder(runtime: Path) -> Path:
    return runtime / "raise"


def record(runtime: Path, name: str, message: str, terminal: str) -> None:
    """Note the order that tells the desk of this raise, beside the raise's own answer records."""
    from . import durable
    path = folder(runtime) / name / f"notice-{message}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    durable.replace_file(path, json.dumps({"format": 1, "ship": name, "message": message,
                                           "terminal": terminal}, sort_keys=True) + "\n")


def unseen(runtime: Path, store) -> list[str]:
    """One status line for each raise whose order to the desk is still open, or whose notice cannot be read."""
    from .ship import ShipError
    lines = []
    for path in sorted(folder(runtime).glob("*/notice-*.json")):
        try:
            data = json.loads(path.read_text())
            message = store.get(data["message"])
            if store.is_open(message):
                lines.append(f"ship {data['ship']} raised from {data['terminal']}: "
                             f"the desk's report on {message.short} clears this")
        except (OSError, UnicodeError, ValueError, TypeError, KeyError, ShipError):
            lines.append(f"raise notice unreadable: {path}; inspect it before clearing")
    return lines
