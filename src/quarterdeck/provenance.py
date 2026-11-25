"""Durable launch boundaries for conversation resumes and charging turns to their pools."""

from __future__ import annotations

from dataclasses import replace

from . import durable, seat_files, trusted_cache

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from . import trusted_files
from .harness.base import record_fingerprint

UNKNOWN = "-"
LAUNCH_ROUTES = "launch-routes"
_ROUTE_EVIDENCE = trusted_cache.Kept()


def provider_route(pool) -> dict[str, str]:
    """Identify the declared address without retaining its text or any key reference."""
    address = 'default' if pool.endpoint is None else hashlib.sha256(pool.endpoint.encode()).hexdigest()
    return {'provider': pool.provider, 'address': address}


def valid_provider_route(route) -> bool:
    return (isinstance(route, dict) and route.keys() == {'provider', 'address'}
            and isinstance(route['provider'], str) and bool(route['provider'].strip())
            and isinstance(route['address'], str) and bool(re.fullmatch(r'default|[0-9a-f]{64}', route['address'])))


def launch_route_binding(launch) -> dict:
    """Stable across updates to the same launch's notice, answers, and price."""
    return {'pane': launch.pane, 'pid': launch.pid, 'start': launch.start, 'at': launch.at,
            'harness': launch.harness, 'pool': launch.pool}


def launch_route_path(folder: Path, launch) -> Path:
    identity = json.dumps(launch_route_binding(launch), sort_keys=True).encode()
    return folder / LAUNCH_ROUTES / (hashlib.sha256(identity).hexdigest() + '.json')


def save_launch_route(folder: Path, launch) -> None:
    path = launch_route_path(folder, launch)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    evidence = {'identity': launch_route_binding(launch), 'provider_route': launch.provider_route}
    durable.replace_file(path, json.dumps(evidence) + '\n')


def load_launch_route(folder: Path, launch):
    """The current launch has either matching evidence, an older companion, or an unproved side record."""
    path = launch_route_path(folder, launch)
    key, started = str(folder), trusted_cache.now()
    try:
        stamp = trusted_cache.link_stamp(path)
        if stamp is None:
            _ROUTE_EVIDENCE.drop(key)
            return launch
        if kept := _ROUTE_EVIDENCE.get(key, (stamp,)):
            identity, route = kept
        else:
            raw = seat_files.read_text(path)
            data = json.loads(raw) if raw is not None else {}
            identity, route = data['identity'], data['provider_route']
            after = trusted_cache.link_stamp(path)
            if stamp != after:
                raise ValueError('provider route changed during its read')
            _ROUTE_EVIDENCE.keep(key, (stamp,), (after,), started, (identity, route))
        if not launch.start or identity != launch_route_binding(launch) or not valid_provider_route(route):
            raise ValueError('unproved launch route')
        return replace(launch, provider_route=dict(route), route_unproved=False)
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        _ROUTE_EVIDENCE.drop(key)
        return replace(launch, route_unproved=True)


def before_cutover(at: str, cutover: datetime | None) -> bool:
    if cutover is None:
        return False
    try:
        when = datetime.fromisoformat(at)
        return when.utcoffset() is not None and when < cutover
    except (ValueError, TypeError):
        return False


def launch_event(record: Path, launched) -> dict:
    from .record_owner import launch_reader

    event = {"record": str(record), "at": launched.at, "pool": launched.pool,
             "harness": launch_reader(launched), "price": getattr(launched, "price", None)}
    if getattr(launched, 'route_unproved', False):
        event['provider_route'] = None
    elif (route := getattr(launched, 'provider_route', None)) is not None:
        event['provider_route'] = route
    return event


def retained_routes(folder: Path, record: Path, *, cutover: datetime | None = None):
    """Read this record's proved routes. A pre-guard join may lack one."""
    try:
        stream = (folder / trusted_files.LAUNCHES).open()
    except FileNotFoundError:
        return
    with stream:
        for line in stream:
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get('record') != str(record):
                continue
            if 'provider_route' not in event and before_cutover(event.get('at'), cutover):
                yield None, event.get('pool')
                continue
            route, pool = event.get('provider_route'), event.get('pool')
            if not valid_provider_route(route) or not isinstance(pool, str):
                raise ValueError('invalid retained provider route')
            yield route, pool


def retained_launch(folder: Path, record: Path, launched) -> bool:
    """Read-only proof that this exact launch boundary is already in the journal."""
    if launched is None:
        return True
    try:
        text = (folder / trusted_files.LAUNCHES).read_text()
    except FileNotFoundError:
        return False
    event = launch_event(record, launched)
    return event in [json.loads(line) for line in text.splitlines() if line.strip()]


def remember_launch(folder: Path, record: Path, launched) -> None:
    """Journal a proven join before its launch file can be replaced or removed."""
    if launched is None:
        return
    from .record_location import retired
    from .ship import ShipError
    if retired(record):
        raise ShipError("retired conversation cannot be joined to another launch; start fresh")

    event = launch_event(record, launched)
    from . import ship_locks
    # The lock is the ship's, not the file's: the seat's own jail sees its folder and could hold the file.
    with ship_locks.held(ship_locks.for_seat_file(folder, trusted_files.LAUNCHES), f"{folder.name}: record launches"), \
            seat_files.open_append(folder / trusted_files.LAUNCHES, "a+") as stream:  # never through a link
        stream.seek(0)
        events = [json.loads(line) for line in stream if line.strip()]
        if event not in events:
            stream.write(json.dumps(event) + "\n")


def declared_events(seat, events):
    """The seat's launch history as accounting may use it. The file sits in the seat's folder, which a jail can
    write: a line that is not a launch qd wrote is dropped, and for a jailed seat a pool not declared for it is
    charged nothing (UNKNOWN), so no jail's turns, walls or prices land on another pool. An unjailed seat's
    history keeps its pools: it holds the operator's power, and a switch leaves its old pool behind it."""
    from .seat import declared_pools, jailed_seat
    declared = declared_pools(seat) if jailed_seat(seat) else None  # an unjailed seat's history is its own
    kept = []
    for event in events:
        if (not isinstance(event, dict) or not isinstance(event.get('record'), str)
                or not isinstance(event.get('at'), str) or not isinstance(event.get('pool'), str)):
            continue
        try:
            datetime.fromisoformat(event['at'])
        except ValueError:
            continue
        kept.append(event if declared is None or event['pool'] in declared
                    else {**event, 'pool': UNKNOWN, 'price': None})
    return kept


def canonical_launch(event):
    """One join, with omitted price and harness meaning the same as null."""
    if not isinstance(event, dict) or not isinstance(event.get('record'), str):
        return None
    return {'record': event['record'], 'at': event.get('at'), 'pool': event.get('pool'),
            'harness': event.get('harness'), 'price': event.get('price')}


def owning_launch_evidence(events, record: str) -> str:
    """The launches joined to this record. A seat's current launch is not one of them unless it names this record."""
    owned = []
    for event in events:
        item = canonical_launch(event)
        if item is not None and item['record'] == record and item not in owned:
            owned.append(item)
    return json.dumps(owned, sort_keys=True)


class Provenance:
    def __init__(self, db, create: bool = True):
        self.db = db
        self.sources = {}
        self.launch_events = {}
        if create:  # a reading opens the book read-only and makes no table
            db.execute("""CREATE TABLE IF NOT EXISTS record_launches (
                record TEXT NOT NULL, at TEXT NOT NULL, seat TEXT NOT NULL, pool TEXT NOT NULL,
                PRIMARY KEY (record, at, seat, pool))""")
            db.execute("""CREATE TABLE IF NOT EXISTS launch_prices (
                record TEXT NOT NULL, at TEXT NOT NULL, seat TEXT NOT NULL, pool TEXT NOT NULL, price TEXT,
                PRIMARY KEY (record, at, seat, pool))""")
            from .program_use import schema
            schema(db)

    def remember(self, record: Path, seat: str, pool: str, at: datetime, price: dict | None = None,
                 *, commit: bool = True, harness: str | None = None) -> bool:
        stamp = at.astimezone(timezone.utc).isoformat()
        key = (str(record), stamp, seat, pool)
        saved = json.dumps(price, sort_keys=True) if price is not None else None
        changed = not self.db.execute("SELECT 1 FROM record_launches WHERE record=? AND at=? AND seat=? AND pool=?", key).fetchone()
        if changed:
            self.db.execute("INSERT INTO record_launches VALUES (?,?,?,?)", key)
        previous = self.db.execute("SELECT price FROM launch_prices WHERE record=? AND at=? AND seat=? AND pool=?", key).fetchone()
        inserted = previous is None
        if inserted:
            self.db.execute("INSERT INTO launch_prices VALUES (?,?,?,?,?)", (*key, saved))
        if not inserted and saved is not None:
            previous = previous[0]
            if previous != saved:
                # Contradictory snapshots at one launch are unknown, never last-writer-wins.
                saved = saved if previous is None else "{}"
                if previous != saved:
                    changed = True
                    self.db.execute("UPDATE launch_prices SET price=? WHERE record=? AND at=? AND seat=? AND pool=?", (saved, *key))
        from .program_use import remember
        program_changed = remember(self.db, key, harness)
        if commit and self.db.in_transaction:
            self.db.commit()
        return bool(changed or inserted or program_changed)

    def prepare(self, seat, current: Path | None, launched):
        """Read launch evidence before the record transaction; cache the pre-read stamp."""
        path = seat.trusted / trusted_files.LAUNCHES
        active = launch_event(current, launched) if current is not None and launched is not None else None
        stamp = (record_fingerprint(path), json.dumps(active, sort_keys=True))
        if self.sources.get(path) == stamp and path in self.launch_events:
            return path, stamp, None
        events = []
        for line in (path.read_text().splitlines() if path.is_file() else []):
            try:
                events.append(json.loads(line)) if line.strip() else None
            except ValueError:
                continue  # not a line qd wrote: dropped, as declared_events drops any other
        if active is not None:
            events.append(active)
        events = declared_events(seat, events)
        self.launch_events[path] = events
        return path, stamp, events

    def owning_launch(self, path: Path, record: Path) -> str:
        """Checkpoint key for the launches joined to ``record``, from the events ``prepare`` just read."""
        return owning_launch_evidence(self.launch_events.get(path, ()), str(record))

    def sync(self, seat, current: Path | None, launched, *, commit: bool = True, prepared=None) -> set[Path]:
        """Import the journal and current proven join, even if record bytes have not changed."""
        changed = set()
        path, stamp, events = prepared if prepared is not None else self.prepare(seat, current, launched)
        if events is None:
            return changed
        for event in events:
            record = Path(event["record"])
            if self.remember(record, seat.name, event["pool"], datetime.fromisoformat(event["at"]), event.get("price"),
                             commit=False, harness=event.get("harness")):
                changed.add(record)
        if commit and self.db.in_transaction:
            self.db.commit()
        self.sources[path] = stamp
        return changed

    def resolve(self, record: Path, started: datetime | None, ended: datetime | None, seat: str, pool: str) -> tuple[str, str]:
        """Explicit caller provenance is a fallback only when no launch history exists.

        Service callers supply UNKNOWN for that fallback: a current seat preference
        cannot establish which pool ran an old turn. A known launch takes precedence.
        """
        launches = self.db.execute("SELECT at, seat, pool FROM record_launches WHERE record = ? ORDER BY at",
                                    (str(record),)).fetchall()
        if not launches:
            return seat, pool
        eligible = [r for r in launches if started is not None and datetime.fromisoformat(r[0]) <= started]
        if eligible:
            latest = eligible[-1][0]
            owners = {(s, p) for at, s, p in eligible if at == latest}
            if len(owners) == 1:
                owner = owners.pop()
                # A turn spanning a switch may contain usage from both pools. Without
                # an end time, a later switch cannot safely be ruled out either.
                crosses = any(datetime.fromisoformat(at) > started and
                              (ended is None or datetime.fromisoformat(at) <= ended) and (s, p) != owner
                              for at, s, p in launches)
                if not crosses:
                    return owner
        return seat, UNKNOWN

    def price(self, record: Path, started: datetime | None, ended: datetime | None) -> dict | None:
        """One unambiguous launch interval, never the pool's current preference."""
        if started is None:
            return None
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='launch_prices'").fetchone():
            return None
        rows = self.db.execute("SELECT l.at,l.seat,l.pool,p.price FROM record_launches l LEFT JOIN launch_prices p USING(record,at,seat,pool) WHERE l.record=? ORDER BY l.at", (str(record),)).fetchall()
        eligible = [r for r in rows if datetime.fromisoformat(r[0]) <= started]
        if not eligible:
            return None
        latest = eligible[-1][0]
        choices = {tuple(r[1:]) for r in eligible if r[0] == latest}
        if len(choices) != 1:
            return None
        chosen = choices.pop()
        if any(datetime.fromisoformat(r[0]) > started and (ended is None or datetime.fromisoformat(r[0]) <= ended)
               and tuple(r[1:]) != chosen for r in rows):
            return None
        try:
            return json.loads(chosen[2]) if chosen[2] is not None else None
        except (TypeError, ValueError):
            return None

    def owners(self, record: str, seat: str, pool: str) -> list[tuple[str, str]]:
        """Every known pool may have missing turns, including a launch with none decoded yet."""
        known = self.db.execute("SELECT seat, pool FROM turns WHERE record = ? UNION "
                                "SELECT seat, pool FROM record_launches WHERE record = ? ORDER BY seat, pool",
                                (record, record)).fetchall()
        return known or [(seat, pool)]
