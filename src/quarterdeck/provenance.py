"""Durable launch boundaries for charging a resumed record to the pool that ran each turn."""

from __future__ import annotations

import fcntl
import json
from datetime import datetime, timezone
from pathlib import Path

UNKNOWN = "-"


def remember_launch(folder: Path, record: Path, launched) -> None:
    """Journal a proven join before its launch file can be replaced or removed."""
    if launched is None:
        return
    from .record_owner import launch_reader

    event = {"record": str(record), "at": launched.at, "pool": launched.pool, "harness": launch_reader(launched)}
    with (folder / "record-launches.jsonl").open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        events = [json.loads(line) for line in stream if line.strip()]
        if event not in events:
            stream.write(json.dumps(event) + "\n")


class Provenance:
    def __init__(self, db, create: bool = True):
        self.db = db
        if create:  # a reading opens the book read-only and makes no table
            db.execute("""CREATE TABLE IF NOT EXISTS record_launches (
                record TEXT NOT NULL, at TEXT NOT NULL, seat TEXT NOT NULL, pool TEXT NOT NULL,
                PRIMARY KEY (record, at, seat, pool))""")

    def remember(self, record: Path, seat: str, pool: str, at: datetime) -> bool:
        stamp = at.astimezone(timezone.utc).isoformat()
        changed = self.db.execute("INSERT OR IGNORE INTO record_launches VALUES (?,?,?,?)",
                                  (str(record), stamp, seat, pool)).rowcount
        self.db.commit()
        return bool(changed)

    def sync(self, seat, current: Path | None, launched) -> set[Path]:
        """Import the journal and current proven join, even if record bytes have not changed."""
        changed = set()
        path = seat.home.parent / "record-launches.jsonl"
        events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.is_file() else []
        if current is not None and launched is not None:
            events.append({"record": str(current), "at": launched.at, "pool": launched.pool})
        for event in events:
            record = Path(event["record"])
            if self.remember(record, seat.name, event["pool"], datetime.fromisoformat(event["at"])):
                changed.add(record)
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

    def owners(self, record: str, seat: str, pool: str) -> list[tuple[str, str]]:
        """Every known pool may have missing turns, including a launch with none decoded yet."""
        known = self.db.execute("SELECT seat, pool FROM turns WHERE record = ? UNION "
                                "SELECT seat, pool FROM record_launches WHERE record = ? ORDER BY seat, pool",
                                (record, record)).fetchall()
        return known or [(seat, pool)]
