"""Derived settlement of exact informational notices; never a read receipt."""

from collections import Counter
from datetime import datetime
from pathlib import Path
import re

from . import post


def notice_class(message):
    if message.sender != post.SHIP or message.kind != 'report' or message.re is not None:
        return None
    if message.body in (f"ship's orders changed · read runtime/brief/{message.to}.md",
                        f"brief changed · read runtime/brief/{message.to}.md"):
        return 'changed orders'
    if re.fullmatch(r'moved to pool [^\s]+ from [^\s]+ (?:at its wall|by qd set)', message.body):
        return 'pool move'
    return None


def launched_at(runtime, seat):
    """Only a readable, validated recorded launch can settle earlier notices."""
    from .seat import Seat
    from . import ship as shipmod
    if Path(seat).name != seat or seat in ('', '.', '..'):
        return None
    try:
        ship = post.reading_value('notice-ship', runtime, lambda: shipmod.load(runtime.parent))
        launched = Seat(ship, ship.seat(seat), None).launched()
        if launched is None:
            return None
        at = launched.when
        return at if at.tzinfo is not None else None
    except (OSError, ValueError, shipmod.ShipError):
        return None  # missing/unreadable evidence grants no settlement


def settled(store, seat, *, brief_at=None):
    """A later launch or newer exact notice; no durable certificate or message edits.

    brief_at is used only while planning an actual launch which supplies the brief.
    If startup fails, ordinary readers still use the unchanged recorded launch.
    """
    incoming = store.for_seat(seat)
    candidates = [(m, notice_class(m)) for m in incoming]
    candidates = [(m, kind) for m, kind in candidates if kind is not None and m.at.tzinfo is not None]
    launch = launched_at(store.dir.parent.parent, seat) if candidates else None
    if isinstance(brief_at, datetime) and brief_at.tzinfo is not None:
        launch = max(launch, brief_at) if launch is not None else brief_at
    latest = {}
    for message, kind in candidates:
        latest[kind] = max(latest.get(kind, (message.at, message.id)), (message.at, message.id))
    result = {}
    for message, kind in candidates:
        if launch is not None and message.at < launch:
            result[message.id] = f'{kind}: settled by a later launch'
        elif (message.at, message.id) < latest[kind]:
            result[message.id] = f'{kind}: settled by a newer notice'
    # A saved group/cascade terminal notice has no further duty after its own
    # root is closed. An arbitrary report on a closed root proves no such thing.
    terminal = [m for m in incoming if m.sender == post.SHIP and m.kind == 'report' and m.re]
    if terminal:
        from .thread_context import _terminal_notice
        by_id = {m.id: m for m in store.all()}
        for message in terminal:
            root = by_id.get(message.re)
            if (root is not None and root.to == seat and root.obligation and not store.is_open(root)
                    and _terminal_notice(store, message, root, seat, by_id)):
                result[message.id] = 'terminal notice: settled by closure of its work'
    return result


def summary(settled):
    """One bounded history line, with classes/counts only, never bodies or ids."""
    counts = Counter(settled.values())
    return ('qd > notice history: ' + '; '.join(f'{count} {reason}' for reason, count in sorted(counts.items()))
            if counts else '')
