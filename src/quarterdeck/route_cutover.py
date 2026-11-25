"""One-time proof of the start of provider-route recording on an installed ship."""

from datetime import datetime
import json
import shlex
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

from . import copy_state, durable, provenance, seat_migration, trusted_files
from .ship import ShipError

FILE = 'provider-route-cutover.json'
REFERENCE = 'provider_route_cutover'
MODULE = 'quarterdeck/route_cutover.py'
ENROLL = '--enroll-provider-route-cutover'
SKIP = '--skip-provider-route-cutover'
MODES = ('enroll', 'virgin')
IDENTITY = ('commit', 'instance', 'package_digest')


def _entry(path: Path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _entries(path: Path) -> list[Path]:
    info = _entry(path)
    if info is None:
        return []
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError('expected a plain directory')
    return list(path.iterdir())


def _copy_has_guard(root: Path, fact: dict) -> bool:
    from .install import package_origin

    try:
        package, digest = package_origin(root, fact.get('have_laws', True))
    except ShipError as error:
        raise ValueError('installed package proof could not be read') from error
    if fact.get('package_digest', digest) != digest or not stat.S_ISDIR(package.lstat().st_mode):
        raise ValueError('installed package proof does not match')
    info = _entry(package / Path(MODULE).name)
    if info is None:
        return False
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('route capability is unreadable')
    return True


def _command(ship, commit: str) -> str:
    return shlex.join(['qd', '--ship', str(ship.folder), 'sync', commit, ENROLL])


def _partial(ship) -> str:
    return (f'{ship.folder.name}: provider route enrollment is incomplete or inconsistent; '
            'keep the existing receipts and inspect qd doctor before retrying')


def _receipt(ship, state: dict, *, activating: bool = False) -> dict | None:
    from . import install

    path = ship.runtime / FILE
    reference = state.get(REFERENCE)
    pending = state.get('pending') or {}
    if not isinstance(pending, dict):
        raise ShipError(_partial(ship))
    proposed = pending.get(REFERENCE)
    if (activating and reference is None and isinstance(proposed, dict)
            and install.STARTING_ROUTE_ENROLLMENT == proposed and pending.get('phase') == 'target_ready'):
        current = copy_state.current(state)
        if current and all(proposed.get(key) == current[key] for key in (*IDENTITY, 'at')):
            reference = proposed
    try:
        info = _entry(path)
        if info is None and REFERENCE not in state and proposed is None:
            return None
        if info is None or not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
            raise ValueError('unreadable cutover receipt')
        receipt = json.loads(path.read_text())
        if (not isinstance(receipt, dict) or receipt != reference
                or receipt.keys() != {*IDENTITY, 'at', 'format', 'mode', 'ship'}
                or type(receipt.get('format')) is not int
                or receipt['format'] != 2 or receipt.get('mode') not in MODES
                or receipt.get('ship') != str(ship.folder.resolve())):
            raise ValueError('cutover receipts disagree')
        fact = dict(receipt, ref=receipt['commit'], proof='good', have_laws=True)
        if 'instance' in receipt and copy_state.valid_fact(fact):
            return receipt
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        pass
    raise ShipError(_partial(ship))


def require_virgin(ship) -> None:
    """Conservative absence check across current, retired and earlier seat storage."""
    runtime = ship.runtime
    try:
        trusted = _entries(trusted_files.root(runtime))
        if (any(path.name != seat_migration.MARK for seat in trusted for path in _entries(seat))
                or _entries(runtime / 'seats')
                or _entries(runtime / 'locks/lifecycle')
                or _entries(runtime / 'evidence/seat-move')
                or _entries(runtime / 'tmux')):
            raise ShipError(f'{ship.folder.name}: runtime contains launch or retained seat history')
    except (OSError, ValueError) as error:
        raise ShipError(f'{ship.folder.name}: absence of launch history could not be proved') from error


def check_transition(ship, state: dict, commit: str, target_has_route: bool, *,
                     mode: str | None = None, predecessor: Path | None = None) -> None:
    """Read both copies before deciding how this guard-aware target may be activated."""
    if mode is not None and mode not in MODES:
        raise ShipError('provider route enrollment mode is unknown')
    if not target_has_route:
        if mode:
            raise ShipError('target does not implement provider route enrollment')
        return
    live = ship.runtime / 'venv' if predecessor is None else predecessor
    receipt = _receipt(ship, state)
    if receipt is not None:
        if mode:
            raise ShipError(f'{ship.folder.name}: provider route cutover is already recorded')
        return
    old = copy_state.current(state)
    try:
        if old is None and _entry(live) is None:
            if mode:
                raise ShipError('provider route enrollment options require an installed predecessor')
            require_virgin(ship)
            return
        usable = copy_state.usable(live, old) is None
        if usable and _copy_has_guard(live, old):
            if mode:
                raise ShipError('the installed predecessor already records provider routes')
            return
        if mode and not usable:
            raise ShipError('provider route enrollment requires a proved installed predecessor; inspect qd doctor')
        if mode == 'virgin':
            require_virgin(ship)
        if mode:
            # A guard in an older slot without either receipt cannot establish a new first cutoff.
            trusted = trusted_files.root(ship.runtime)
            previous = ship.runtime / 'venv.prev'
            if previous != live and _entry(previous) is not None:
                # An unrecorded older copy is still read through its own interpreter; unreadable stays unknown.
                if _copy_has_guard(previous, copy_state.previous(state) or {}):
                    raise ShipError(_partial(ship))
            if any(_entry(seat / provenance.LAUNCH_ROUTES) is not None for seat in _entries(trusted)):
                raise ShipError(_partial(ship))
            return
    except (OSError, ValueError, TypeError) as error:
        raise ShipError(f'{ship.folder.name}: installed provider route boundary is unknown; inspect qd doctor') from error
    raise ShipError(f'{ship.folder.name}: first provider route update requires enrollment; '
                    f'from the guard-aware checkout run {_command(ship, commit)}')


def check_install_proof(argv: list[str]) -> None:
    """An older installer observes this refusal and restores its predecessor."""
    if argv != ['--version']:
        return
    from . import install

    live = Path(sys.prefix)
    runtime = live.parent
    if live.name != 'venv' or runtime.name != 'runtime' or Path.cwd() != runtime:
        return
    ship = SimpleNamespace(folder=runtime.parent, runtime=runtime)
    state = install._state(runtime)
    pending = state.get('pending')
    if pending is None:
        current = copy_state.current(state)
        if current is None:
            require_virgin(ship)  # includes a disposable copy with its own empty runtime
        elif not install.LOADED_COPY or install.LOADED_COPY['commit'] != current['commit']:
            raise ShipError('provider route installation proof cannot identify its transaction')
        return
    if pending.get('phase') != 'proving' or pending.get('kind') not in ('sync', 'back'):
        raise ShipError('provider route installation proof cannot identify its transaction')
    fact = pending.get('target')
    if (not isinstance(fact, dict) or not install.LOADED_COPY
            or any(install.LOADED_COPY[key] != fact.get(key) for key in ('commit', 'instance'))):
        raise ShipError('provider route installation proof cannot identify the target copy')
    if pending['kind'] == 'back':
        old_role = 'venv.prev'
    elif pending.get('old_live_identity') is None:
        old_role = None
    else:
        old_role = pending.get('backup') or 'venv.prev'
    # The live directory already contains this target. Read the saved predecessor.
    previous = {**state, 'current': pending.get('old_current')}
    check_transition(ship, previous, fact['commit'], True, mode=pending.get('route_cutover_mode'),
                     predecessor=runtime / old_role if old_role else runtime / 'venv.backup')


def begin(ship, state: dict, fact: dict, mode: str) -> dict:
    """Write the receipt first; final activation publishes its peer in installed state."""
    path = ship.runtime / FILE
    if (_receipt(ship, state) is not None or not copy_state.valid_fact(fact)
            or fact.get('proof') != 'good' or 'instance' not in fact):
        raise ShipError('provider route cutover could not be proved once')
    receipt = {key: fact[key] for key in (*IDENTITY, 'at')}
    receipt.update(format=2, mode=mode, ship=str(ship.folder.resolve()))
    durable.replace_file(path, json.dumps(receipt, sort_keys=True) + '\n')
    return receipt


def first(ship) -> datetime | None:
    """The proved first activation time, independent of later changes to the active copy."""
    if ship.code == 'checkout':
        return None
    try:
        state = copy_state.read(ship.runtime)
        if copy_state.current(state) is None:
            return None
        receipt = _receipt(ship, state, activating=True)
        if receipt and receipt['mode'] == 'enroll':
            return datetime.fromisoformat(receipt['at'])
    except (OSError, ValueError, KeyError, TypeError, RecursionError, ShipError):
        pass
    return None


def doctor(ship, state: dict) -> str | None:
    try:
        _receipt(ship, state)
    except (OSError, ShipError):
        return _partial(ship)
    return None
