"""The fleet is an index of project declarations, never another configuration."""

from . import ship as shipmod
from .ship import ShipError


def peers(ship: shipmod.Ship) -> dict[str, shipmod.Ship]:
    """Fixed project office and direct non-template ships, keyed by address name."""
    folder = ship.folder
    if getattr(ship, 'kind', None) == 'office' and folder.name == 'office':
        project = folder.parent
    elif folder.parent.name == 'ships' and folder.name not in shipmod.TEMPLATES:
        project = folder.parent.parent
    else:
        project = None
    if project is None:
        return {ship.folder.name: ship}
    found = {}
    folders = [project / 'office']
    ships = project / 'ships'
    if ships.is_dir() and not ships.is_symlink():
        folders += [folder for folder in sorted(ships.iterdir())
                    if folder.name not in shipmod.TEMPLATES and folder.name != 'office']
    for folder in folders:
        if not shipmod.valid_name(folder.name) or '@' in folder.name or folder.is_symlink():
            continue
        try:
            candidate = shipmod.load(folder)
            if folder == project / 'office' and getattr(candidate, 'kind', None) != 'office':
                continue
            found[folder.name] = candidate
        except (ShipError, OSError):
            continue  # an unavailable destination remains waiting, never guessed
    return found
