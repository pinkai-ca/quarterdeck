"""Sequential project sync through the single-ship safe installer.

No fleet state is persisted. Dispositions come from observed copy selection,
never from parsing an installer's proof text or assuming recovery succeeded.
"""

from __future__ import annotations

from pathlib import Path

from . import copy_state, install, ship as shipmod
from .ship import ShipError
from .tmux import Tmux, TmuxError


def observe(folder: Path) -> tuple[str | None, str | None]:
    try:
        selected = copy_state.select(folder)
    except (ShipError, OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        return None, f'installed copy observation failed ({type(error).__name__})'
    if selected.qd is not None and selected.fact is not None:
        return selected.fact['commit'], None
    return None, selected.warning or 'no usable installed copy observed'


def plan(project: Path) -> list[dict]:
    """Snapshot only this project's direct ships, putting its marked ship last."""
    project = Path(project).resolve()
    ships = shipmod.ships_folder(project)
    if ships.is_symlink() or not ships.is_dir():
        raise ShipError(f'{ships} is not a plain project ships folder')
    # A stored name determines order. Its declaration is checked in its row.
    try:
        (ships / shipmod.MARK).lstat()
    except FileNotFoundError:
        last = None
    except OSError as error:
        raise ShipError(f'cannot inspect default mark ({type(error).__name__})') from None
    else:
        last = shipmod.read_default_mark(ships)
    try:
        folders = sorted(path for path in ships.iterdir()
                         if path.name != last and path.is_dir() and (path / 'ship.toml').is_file())
    except OSError as error:
        raise ShipError(f'cannot enumerate project ships: {error}') from None
    if last is not None:
        folders.append(ships / last)
    rows = []
    for folder in folders:
        row = dict(ship=folder.name, folder=folder, disposition='not tried',
                   before=None, commit=None, reason=None, proof=[], ship_object=None)
        marked = folder.name == last
        if not marked and folder.name in shipmod.TEMPLATES:
            row.update(disposition='passed over', reason='tracked template')
        else:
            try:
                resolved = folder.resolve()
                if resolved.parent != ships:
                    raise ShipError('ship folder resolves outside the project ships folder')
                ship = (shipmod.load_marked_ship(folder, resolved=resolved) if marked
                        else shipmod.load(resolved))
                row.update(folder=ship.folder, ship_object=ship)
                if ship.code == 'checkout':
                    row.update(disposition='passed over', reason='follows the checkout (code = "checkout")')
                else:
                    commit, reason = observe(ship.folder)
                    row.update(before=commit, commit=commit, reason=reason)
            except (ShipError, OSError, ValueError, TypeError, KeyError, AttributeError) as error:
                row['reason'] = f'cannot prepare ship: {error}'
        rows.append(row)
    return rows


def run(project: Path, ref: str = 'HEAD', *, force: bool = False) -> dict:
    """Resolve once, call the ordinary installer serially, stop on any failure."""
    target = install.resolve_target(project, ref)
    rows = plan(project)
    stopped = False
    archive = install.sync_archive(*target)
    if archive.guarded:
        # The whole fleet is read first, including ships after any that are already enrolled.
        for row in rows:
            ship = row['ship_object']
            if row['disposition'] == 'passed over' or ship is None:
                continue
            try:
                install.preflight_route_sync(ship, target[1])
            except ShipError as error:
                row.update(disposition='failed', reason=str(error))
                stopped = True
        if stopped:
            return dict(target=target[1], ships=[{key: value for key, value in row.items() if key != 'ship_object'}
                                                 for row in rows], ok=False)
    for row in rows:
        if row['disposition'] == 'passed over':
            continue
        if stopped:
            row['reason'] = 'stopped after an earlier ship failed'
            continue
        ship = row['ship_object']
        if ship is None:
            row['disposition'] = 'failed'
            stopped = True
            continue
        try:
            row['proof'] = install.upgrade(ship, ref, Tmux.for_ship(ship), force=force, target=target, archive=archive)
            commit, reason = observe(ship.folder)
            row.update(commit=commit, reason=reason)
            if commit != target[1] or reason is not None:
                raise ShipError(reason or 'installed copy does not match the pinned target')
            row.update(disposition='synced', reason=None)
        except Exception as error:
            commit, observation = observe(ship.folder)
            # The framework's own refusals say why in its own words: a ship's (ShipError) and its tmux server's
            # (TmuxError and its private-server kinds). Anything else is named by its type alone.
            row.update(disposition='failed', commit=commit,
                       reason=str(error) if isinstance(error, (ShipError, TmuxError)) else f'sync failed ({type(error).__name__})')
            if observation:
                row['reason'] += '; ' + observation
            stopped = True
    # A later ship was not operated on, but its copy may have changed independently.
    # Refresh that observation instead of claiming the initial snapshot is still current.
    for row in rows:
        if row['disposition'] == 'not tried':
            commit, observation = observe(row['folder'])
            row['commit'] = commit
            if observation:
                row['reason'] = (row['reason'] + '; ' if row['reason'] else '') + observation
    public = [{key: value for key, value in row.items() if key != 'ship_object'} for row in rows]
    return dict(target=target[1], ships=public, ok=not stopped)


def render(data: dict) -> list[str]:
    # Keep ordinary dirty-source, historical-target and activation notices. They
    # precede the final dispositions and never decide whether a ship succeeded.
    lines = [f'{row["ship"]}: {line}' for row in data['ships'] for line in row['proof']]
    for row in data['ships']:
        name, state, commit = row['ship'], row['disposition'], row['commit']
        if state == 'passed over':
            text = f'passed over: {row["reason"]}'
        elif state == 'synced':
            text = f'synced to {commit}'
        else:
            if commit is None:
                where = 'copy unknown'
            elif row['before'] is not None and commit == row['before']:
                where = f'still on {commit}'
            else:
                where = f'observed on {commit}'
            text = f'{state} and {where}' if commit is not None else f'{state}; {where}'
            if row['reason']:
                text += f': {row["reason"]}'
        lines.append(f'{name}: {text}')
    return lines or ['no project ships to sync']
