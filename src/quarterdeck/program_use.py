"""Public program activity, with historical turns bound to saved launch evidence."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from . import harness as harnesses, record_owner


def instant(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(timezone.utc) if parsed.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


def schema(db):
    """Add evidence beside the unchanged positional launch table, writer only."""
    db.execute("""CREATE TABLE IF NOT EXISTS launch_harnesses (
        record TEXT NOT NULL, at TEXT NOT NULL, seat TEXT NOT NULL, pool TEXT NOT NULL,
        harness TEXT NOT NULL, PRIMARY KEY (record, at, seat, pool))""")
    for action in ('INSERT', 'UPDATE', 'DELETE'):
        keys = ('NEW',) if action == 'INSERT' else ('OLD', 'NEW') if action == 'UPDATE' else ('OLD',)
        body = ' '.join(f'DELETE FROM launch_harnesses WHERE record={key}.record AND at={key}.at '
                        f'AND seat={key}.seat AND pool={key}.pool;' for key in keys)
        db.execute(f'CREATE TRIGGER IF NOT EXISTS launch_harnesses_{action.lower()} '
                   f'AFTER {action} ON record_launches BEGIN {body} END')


def remember(db, key, harness):
    """Missing evidence adds nothing; contradictions at one launch stay unknown."""
    if harness is None:
        return False
    harness = harness if isinstance(harness, str) and harness in harnesses.NAMES else '-'
    row = db.execute('SELECT harness FROM launch_harnesses WHERE record=? AND at=? AND seat=? AND pool=?', key).fetchone()
    if row is None:
        db.execute('INSERT INTO launch_harnesses VALUES (?,?,?,?,?)', (*key, harness))
        return True
    if row[0] not in (harness, '-'):
        db.execute("UPDATE launch_harnesses SET harness='-' WHERE record=? AND at=? AND seat=? AND pool=?", key)
        return True
    return False


def working(seat):
    """Read the current saved join only: no join, registration or screen writes."""
    from .seat import UnresolvedSeat
    if isinstance(seat, UnresolvedSeat):
        return None
    try:
        launched = seat.launched()
        name = record_owner.launch_reader(launched)
        if launched is None or name != seat.harness.name:
            return None
        pane = seat.pane()
        if pane is None or pane.dead:
            return False
        record = Path((seat.folder / 'record').read_text().strip())
        # A historical reader binding alone cannot prove a current launch's join.
        events = record_owner.events(seat.folder / 'record-launches.jsonl')
        names = {row.get('harness') for row in events if row.get('record') == str(record)
                 and row.get('at') == launched.at and row.get('pool') == launched.pool}
        if names != {name}:
            return None
        state = seat.harness.state(record)
        if (type(state.turn_open) is not bool or seat.launched() != launched
                or (seat.folder / 'record').read_text().strip() != str(record)):
            return None
        return bool(state.turn_open and not seat.harness.waiting_for_human(state))
    except (OSError, ValueError, TypeError, KeyError):
        return None


def aggregate(values):
    if any(value is True for value in values):
        return True
    return False if values and all(value is False for value in values) else None


def history(db):
    """Read actual launch intervals; filenames and current declarations prove none."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='launch_harnesses'").fetchone():
        return {}, None
    launches = {}
    names = set()
    for record, at, seat, pool, name in db.execute(
            'SELECT l.record,l.at,l.seat,l.pool,h.harness FROM record_launches l '
            'LEFT JOIN launch_harnesses h USING(record,at,seat,pool)'):
        launches.setdefault(record, []).append((instant(at), seat, pool, name))
        if name in harnesses.NAMES:
            names.add(name)
    latest = dict.fromkeys(names)
    for record, seat, started, ended in db.execute('SELECT record,seat,started,ended FROM turns'):
        start, end = instant(started), instant(ended)
        rows = launches.get(record, [])
        if start is None or end is None or end < start or any(row[0] is None for row in rows):
            continue
        eligible = [row for row in rows if row[0] <= start]
        if not eligible:
            continue
        at = max(row[0] for row in eligible)
        choices = {(owner, pool, name) for stamp, owner, pool, name in eligible if stamp == at}
        if len(choices) != 1:
            continue
        owner, _, name = choices.pop()
        if owner != seat or name not in names:
            continue
        # Crossing any later launch cannot establish one unambiguous launch owner.
        if any(start < row[0] <= end for row in rows):
            continue
        if latest[name] is None or end > latest[name]:
            latest[name] = end
    return latest, None


def collect(ship, selected):
    from .ledger import Ledger
    declared = {spec.for_pool(pool).harness for spec in ship.seats.values() for pool in spec.pools}
    states = {name: [] for name in declared}
    for seat in selected:
        name = getattr(getattr(seat, 'harness', None), 'name', None)
        if name is not None:
            states.setdefault(name, []).append(working(seat))
        else:
            for pool in seat.spec.pools:
                states.setdefault(seat.spec.for_pool(pool).harness, []).append(None)
    last, error = {}, None
    try:
        book = Ledger.reading(ship.runtime / 'ledger.sqlite')
        if book is not None:
            with book:
                last, error = history(book.db)
                if book.catching_up() and error is None:
                    error = 'program history last known while accounting catches up'
    except (sqlite3.Error, OSError, ValueError, TypeError):
        error = 'program history unavailable'
    return [dict(harness=name, working_now=aggregate(states.get(name, [])),
                 last_turn_ended=last[name].isoformat() if last.get(name) is not None else None)
            for name in sorted(set(states) | set(last))], error
