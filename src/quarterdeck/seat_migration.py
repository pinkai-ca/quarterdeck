"""Once per seat: the files its own folder held before they moved to its trusted folder (trusted_files).

Until this change the service kept a seat's launch record, its holds and its journals in ``runtime/seats/<seat>``,
which the seat's jail can write. A copy found there is therefore a claim, never a fact. Each is read once, without
following a link and bounded in size, and taken into ``runtime/trusted/<seat>`` only where live state confirms it or
where it can only touch that seat itself. Everything else is rebuilt by the service's own next reading. The old copies
are then set aside unchanged in ``runtime/evidence/seat-move/<seat>-<time>/`` and never read again.

- launch.json: taken only from the seat's own live window, found by the command tmux started it with (``sh
  runtime/seats/<seat>/launch.sh``), which tmux fixes at the window's birth and nothing inside the pane can change.
  The pane and pid come from tmux and the start from /proc, never from the file. The record must name a pool the ship
  file declares for the seat, and that pool's program. Its other fields (its time, the records before it, its resume
  id, version, answered questions, recovery and notice) touch that seat alone and are taken as its own claim. With no
  live window the seat is down and nothing is taken. A live window that the record cannot be joined to holds the seat
  (``launch-unconfirmed.json``), as an unreadable record does, until a person brings it down.
- launch-price.json: rebuilt. The rates are taken only when they are the pool's declared price; the model is the
  seat's own claim.
- pool and pool-explicit: taken only when they name a pool the ship file declares for the seat.
- record-readers.jsonl and record-launches.jsonl: copied row by row. A row is kept only when its pool is declared for
  the seat, its program is one of those pools' programs, and its record lies inside the seat's own home (or the
  program keeps no home of its own).
- record: taken only when it names a record inside the seat's home that a kept row binds to the adopted launch;
  otherwise the join finds it again.
- human-wake.json: taken only when it is bound to the adopted launch's pane, pid and time.
- retiring.json, relaunch-refused.json and person-quiet-wake.json: holds and journals on that seat alone, taken
  when well-formed. A forged one can only hold or quiet its own seat.
- The five alarm journals, compaction.json, refusal.json, move-hold.json, model-mismatch.json, turn-failure.json,
  person-quiet.json, human-wake-status.json, human-wake-history/, acts.jsonl, acts-notices.jsonl and launch.sh: not
  taken. The service writes them afresh (the compaction count of a long-lived seat starts again from 0), and the old
  copies stay in the evidence for a person to read.

It runs at the service's start for every declared seat and before every launch, under the seat's own migration
lock; a mark in the trusted folder says it is done.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
from datetime import datetime, timezone
from pathlib import Path

from . import trusted_files
from .ship import ShipError

MARK = '.moved'  # in the trusted folder: the seat's old files were considered once
UNCONFIRMED = 'launch-unconfirmed.json'  # a live window whose old launch record could not be joined to it
LIMIT = 1 << 20  # a file qd wrote is far smaller; a larger one is not read
REGISTRY_LIMIT = 64 << 20  # a registry grows a row per record and launch
EVIDENCE = 'seat-move'

ADOPT_AS_OWN = (trusted_files.RETIRING, trusted_files.REFUSED_RELAUNCH, trusted_files.PERSON_QUIET_WAKE)


def done(runtime: Path, name: str) -> bool:
    return (trusted_files.folder(runtime, name) / MARK).exists()


def _read(directory: int, name: str, limit: int = LIMIT) -> bytes | None:
    """A regular file of the old seat folder, opened without following a link, at most ``limit`` bytes; else None."""
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        chunks, total = [], 0
        while chunk := os.read(descriptor, 1 << 16):
            total += len(chunk)
            if total > limit:
                return None
            chunks.append(chunk)
        return b''.join(chunks)
    finally:
        os.close(descriptor)


def _json(raw: bytes | None):
    if raw is None:
        return None
    try:
        return json.loads(raw.decode())
    except (UnicodeError, ValueError):
        return None


def _rows(raw: bytes | None) -> list[dict]:
    if raw is None:
        return []
    rows = []
    try:
        lines = raw.decode().splitlines()
    except UnicodeError:
        return []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get('record'), str):
            rows.append(row)
    return rows


def _inside(record: str, home: Path) -> bool:
    """Inside the seat's home as written and with every link resolved: a home is its jail's to write, so a path there
    may be a link to another seat's record (harness.base.inside_home)."""
    from .harness.base import inside_home
    return inside_home(Path(record), home)


def _window(tmux, seat_folder: Path) -> tuple[str, int] | None | str:
    """The seat's one live window, by the command tmux started it with: (pane, pid); None for none; 'many'."""
    reading = getattr(tmux, 'started_with', None)
    if reading is None:
        raise ShipError(f"{seat_folder.name}: its windows cannot be read; its files stay where they are until they can")
    wanted = ['sh', str(seat_folder / trusted_files.LAUNCH_SCRIPT)]
    live = []
    for pane, pid, dead, command in reading():
        try:
            words = shlex.split(command)
            if len(words) == 1:  # tmux shows a command given as one string quoted whole: "sh /path/launch.sh"
                words = shlex.split(words[0])
        except ValueError:
            continue
        if words == wanted and not dead:
            live.append((pane, pid))
    if len(live) > 1:
        return 'many'
    return live[0] if live else None


def migrate(ship, spec, tmux) -> dict[str, str]:
    """Consider the seat's old files once; returns, per name found, what became of it. Names only."""
    from . import ship_locks

    runtime = ship.runtime
    name = spec.name
    if done(runtime, name):
        return {}
    lock = ship_locks.path(runtime, 'seat-files', f'{name}--move.lock')
    with ship_locks.held(lock, f'{name}: seat files move'):
        if done(runtime, name):
            return {}
        trusted = trusted_files.make(runtime, name)
        old = runtime / 'seats' / name
        try:
            directory = os.open(old, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            _mark(trusted)
            return {}
        try:
            present = [entry for entry in trusted_files.MOVED if _exists(directory, entry)]
            # A launch is judged by the seat's live windows: where they cannot be read, nothing moves yet, so a
            # running seat is never read as down.
            window = _window(tmux, old) if trusted_files.LAUNCH in present else None
            outcome = _adopt(ship, spec, window, directory, old, trusted, set(present)) if present else {}
            if present:
                _set_aside(runtime, name, directory, present, outcome)
        finally:
            os.close(directory)
        _mark(trusted)
        return outcome


def _exists(directory: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _mark(trusted: Path) -> None:
    from . import durable
    durable.replace_file(trusted / MARK, datetime.now(timezone.utc).isoformat() + '\n')


def _write(path: Path, data: bytes | str) -> None:
    from . import durable
    text = data.decode() if isinstance(data, bytes) else data
    durable.replace_file(path, text)


def _adopt(ship, spec, window, directory: int, old: Path, trusted: Path, present: set[str]) -> dict[str, str]:
    from . import harness as harnesses
    from .seat import parse_launch, LaunchUnreadable, process_start

    outcome = {name: 'set aside' for name in present}
    pools = tuple(spec.pools)
    programs = {}
    for pool in pools:
        try:
            programs[pool] = spec.for_pool(pool).harness
        except Exception:  # a pool the ship file names that the seat cannot run: none of its rows are kept
            continue
    home = old / 'home'

    # The pool selection.
    for file in (trusted_files.POOL, trusted_files.EXPLICIT):
        if file in present and not (trusted / file).exists():
            raw = _read(directory, file)
            chosen = raw.decode(errors='replace').strip() if raw is not None else None
            if chosen in programs:
                _write(trusted / file, chosen + '\n')
                outcome[file] = 'taken: a declared pool'

    # The registries.
    kept_launches: list[dict] = []
    for file in (trusted_files.READERS, trusted_files.LAUNCHES):
        if file not in present or (trusted / file).exists():
            continue
        rows = []
        for row in _rows(_read(directory, file, REGISTRY_LIMIT)):
            program = row.get('harness')
            if file == trusted_files.LAUNCHES and row.get('pool') not in programs:
                continue
            if program is not None and program not in programs.values():
                continue
            own_home = program is None or harnesses.get(program).home_variable is not None
            if own_home and not _inside(row['record'], home):
                continue
            rows.append(row)
        if rows:
            _write(trusted / file, ''.join(json.dumps(row) + '\n' for row in rows))
        if file == trusted_files.LAUNCHES:
            kept_launches = rows
        outcome[file] = f'taken: {len(rows)} rows of the seat\'s own'

    # The launch record, from the live window alone.
    launch = None
    if trusted_files.LAUNCH in present and not (trusted / trusted_files.LAUNCH).exists():
        data = None
        try:
            raw = _read(directory, trusted_files.LAUNCH)
            data = parse_launch(raw.decode() if raw is not None else '', spec.name) if raw is not None else None
        except (LaunchUnreadable, UnicodeError):
            data = None
        if window is None:
            outcome[trusted_files.LAUNCH] = 'set aside: no live window of the seat'
        else:
            why = None
            if window == 'many':
                why = 'more than one live window started for the seat'
            elif data is None:
                why = 'its old launch record is unreadable'
            elif data['pool'] not in programs:
                why = 'its old launch record names a pool not declared for it'
            elif (data.get('harness') or programs[data['pool']]) != programs[data['pool']]:
                why = "its old launch record names another program than its pool's"
            elif (data['pane'], data['pid']) != window:
                why = 'its old launch record names another window than the one started for it'
            if why is None:
                data = dict(data, harness=programs[data['pool']], start=process_start(window[1]))
                if data['start'] is None:
                    data.pop('start')
                launch = data
                _write(trusted / trusted_files.LAUNCH, json.dumps(data, indent=2) + '\n')
                outcome[trusted_files.LAUNCH] = 'taken: confirmed by its live window'
            else:
                _write(trusted / UNCONFIRMED, json.dumps(dict(
                    pane=window[0] if isinstance(window, tuple) else None,
                    pid=window[1] if isinstance(window, tuple) else None, reason=why,
                    at=datetime.now(timezone.utc).isoformat())) + '\n')
                outcome[trusted_files.LAUNCH] = f'held: {why}'

    # Its price, rebuilt from the pool's declared rates.
    if launch is not None:
        from .pricing import Price, save_price
        price = None
        old_price = _json(_read(directory, trusted_files.PRICE)) if trusted_files.PRICE in present else None
        declared = getattr(ship.pool(launch['pool']), 'price', None)
        if declared is not None and isinstance(old_price, dict) and isinstance(old_price.get('price'), dict):
            claimed = old_price['price']
            try:
                rates = Price.parse(declared)
            except ValueError:
                rates = None
            if rates is not None and isinstance(claimed.get('model'), str):
                price = rates.snapshot(claimed['model'])
        save_price(trusted / trusted_files.LAUNCH, price)
        if trusted_files.PRICE in present:
            outcome[trusted_files.PRICE] = 'rebuilt from the pool\'s declared price' if price else 'rebuilt: no price'

    # The record pointer, only where a kept row binds it to the adopted launch.
    if launch is not None and trusted_files.RECORD in present and not (trusted / trusted_files.RECORD).exists():
        raw = _read(directory, trusted_files.RECORD)
        named = raw.decode(errors='replace').strip() if raw is not None else ''
        bound = any(row.get('record') == named and row.get('at') == launch['at'] for row in kept_launches)
        if named and bound and (_inside(named, home) or harnesses.get(launch['harness']).home_variable is None):
            _write(trusted / trusted_files.RECORD, named + '\n')
            outcome[trusted_files.RECORD] = 'taken: bound to the confirmed launch'

    # The human wake's proof, only for the adopted launch.
    if launch is not None and trusted_files.HUMAN_WAKE in present and not (trusted / trusted_files.HUMAN_WAKE).exists():
        raw = _read(directory, trusted_files.HUMAN_WAKE)
        proof = _json(raw)
        identity = proof.get('identity') if isinstance(proof, dict) else None
        if (isinstance(identity, dict) and identity.get('pane') == launch['pane']
                and identity.get('pid') == launch['pid'] and identity.get('launch') == launch['at']):
            _write(trusted / trusted_files.HUMAN_WAKE, raw)
            outcome[trusted_files.HUMAN_WAKE] = 'taken: bound to the confirmed launch'

    # Holds and journals on the seat alone.
    for file in ADOPT_AS_OWN:
        if file in present and not (trusted / file).exists():
            raw = _read(directory, file)
            if isinstance(_json(raw), dict):
                _write(trusted / file, raw)
                outcome[file] = "taken: the seat's own hold"
    return outcome


def _set_aside(runtime: Path, name: str, directory: int, present: list[str], outcome: dict[str, str]) -> None:
    """Move every old copy, unchanged and never followed, into the evidence; record what became of each."""
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    target = runtime / 'evidence' / EVIDENCE / f'{name}-{stamp}'
    target.mkdir(mode=0o700, parents=True)
    into = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for entry in present:
            try:
                os.rename(entry, entry, src_dir_fd=directory, dst_dir_fd=into)
            except FileNotFoundError:
                outcome[entry] = outcome.get(entry, '') + '; gone before it was set aside'
    finally:
        os.close(into)
    from . import durable
    durable.replace_file(target / 'outcome.json', json.dumps(outcome, indent=2, sort_keys=True) + '\n')


def unconfirmed(trusted: Path) -> dict | None:
    """The hold a move left in a seat's trusted folder when its live window could not be joined to its old record;
    None when there is none."""
    try:
        data = json.loads((Path(trusted) / UNCONFIRMED).read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {'reason': 'its hold is unreadable'}
    return data if isinstance(data, dict) else {'reason': 'its hold is unreadable'}


def close_unconfirmed(ship, name: str, tmux) -> str | None:
    """qd down on a seat the move held: close the window tmux started for it, only while tmux still shows that very
    window as the seat's own (same pane, same pid, started with the seat's launch.sh), then lift the hold. None when
    there is no such hold."""
    hold = unconfirmed(trusted_files.folder(ship.runtime, name))
    if hold is None:
        return None
    window = _window(tmux, ship.runtime / 'seats' / name)
    named = (hold.get('pane'), hold.get('pid'))
    if isinstance(window, tuple) and window == named:
        tmux.kill_window(window[0], expected_pid=window[1])
        if tmux.pane(window[0]) is not None:
            raise ShipError(f"{name}: pane {window[0]} did not close; the seat stays held")
        closed = f" · pane {window[0]} closed"
    elif window == 'many':
        raise ShipError(f"{name}: more than one live window was started for it; close them by hand, then qd down {name}")
    elif window is not None:
        raise ShipError(f"{name}: its live window is not the one the hold names; the seat stays held")
    else:
        closed = " · its window is gone"
    (trusted_files.folder(ship.runtime, name) / UNCONFIRMED).unlink(missing_ok=True)
    return f"{name} down{closed} · the hold the files move left is lifted; qd up {name} launches it afresh"
