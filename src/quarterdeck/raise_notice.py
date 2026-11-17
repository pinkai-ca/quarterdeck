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


def yes_of(answer: Path | None) -> dict | None:
    """The yes a notice is for: its answer record, written once (raise_ship.write_once), by path and the sha256 of its
    bytes. The same record unchanged is the same yes; a new yes writes a new record."""
    if answer is None:
        return None
    import hashlib
    return {"answer": str(answer), "answer_sha256": hashlib.sha256(Path(answer).read_bytes()).hexdigest()}


def told(runtime: Path, name: str, answer: Path | None, *, store, desk: str, ship_folder: Path) -> str | None:
    """The order already posted for this ship and this yes, or None: a rerun resuming on it posts no second one.

    A notice file is no proof by itself: one written by hand could name this yes and any closed order, and buy silence.
    So its order must resolve in the office's store, by its full id and to one message, as the order a raise posts: kind
    order, from the ship, to the office's one desk, under SUBJECT, its body naming this ship's folder and this answer
    record. In every other case the notice is ignored and the raise posts a new order."""
    from . import post
    from .ship import ShipError
    yes = yes_of(answer)
    if yes is None:
        return None
    for path in sorted((folder(runtime) / name).glob("notice-*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, UnicodeError, ValueError):
            continue  # an unreadable notice is named on the status line (unseen); it is no proof of a notice
        if not (isinstance(data, dict) and data.get("answer") == yes["answer"]
                and data.get("answer_sha256") == yes["answer_sha256"] and isinstance(data.get("message"), str)):
            continue
        try:
            message = store.get(data["message"])
        except ShipError:
            continue  # no such order, or a name of several: ambiguity is never resolved by guessing
        if (message.id == data["message"] and message.kind == "order" and message.sender == post.SHIP
                and message.to == desk and message.subject == SUBJECT
                and f" at {ship_folder} " in message.body and f"(answer record {answer})" in message.body):
            return message.id
    return None


def record(runtime: Path, name: str, message: str, terminal: str, answer: Path | None = None) -> None:
    """Note the order that tells the desk of this raise, beside the raise's own answer records, with the yes it is for."""
    from . import durable
    path = folder(runtime) / name / f"notice-{message}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    durable.replace_file(path, json.dumps({"format": 1, "ship": name, "message": message,
                                           "terminal": terminal, **(yes_of(answer) or {})}, sort_keys=True) + "\n")


def unseen(runtime: Path, store) -> list[str]:
    """One status line for each raise whose order to the desk is still open, or whose notice cannot be read."""
    from .ship import ShipError
    lines = []
    # While the post store is held (watch._HeldStore), no notice can be judged open or reported: a readable notice is
    # not shown, never named unreadable, and the hold names itself on every status line. An unreadable file still is.
    held = getattr(store, "held", False)
    for path in sorted(folder(runtime).glob("*/notice-*.json")):
        try:
            data = json.loads(path.read_text())
            data["ship"], data["terminal"], data["message"]
            if held:
                continue
            message = store.get(data["message"])
            if store.is_open(message):
                lines.append(f"ship {data['ship']} raised from {data['terminal']}: "
                             f"the desk's report on {message.short} clears this")
        except (OSError, UnicodeError, ValueError, TypeError, KeyError, ShipError):
            lines.append(f"raise notice unreadable: {path}; inspect it before clearing")
    return lines
