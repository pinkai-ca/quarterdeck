"""Which seat a commit of the ship folder came from, by the session trailer a program writes on its own commits.

Claude Code ends a commit it makes with ``Claude-Session: <url ending in session_<id>>``, the Remote Control session it
runs under, and records that session in the home of the seat that runs it, as ``bridgeSessionId`` in
``<home>/sessions/<pid>.json``. A commit whose trailer names a session recorded in exactly one seat's home came from
that seat. Nothing else of a home is read, and nothing is written. No record reader imports this module.
"""
from __future__ import annotations

import json
from pathlib import Path
import re

TRAILER = re.compile(r'(?m)^Claude-Session: \S*?(session_[A-Za-z0-9]+)\s*$')


def sessions(runtime: Path) -> dict[str, str]:
    """Remote Control sessions to the seats whose homes record them; a session two homes record names neither."""
    found: dict[str, str | None] = {}
    for path in (Path(runtime) / 'seats').glob('*/home/sessions/*.json'):
        try:
            session = json.loads(path.read_text()).get('bridgeSessionId')
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(session, str) and session:
            seat = path.parents[2].name
            found[session] = seat if found.get(session, seat) == seat else None
    return {session: seat for session, seat in found.items() if seat is not None}


def seats_named(message: str, runtime: Path, known: dict[str, str] | None = None) -> list[str]:
    """The seats a commit message's session trailers name, by the sessions the seats' homes record."""
    found = TRAILER.findall(message)
    if not found:
        return []
    known = sessions(runtime) if known is None else known
    return [known[session] for session in found if session in known]
