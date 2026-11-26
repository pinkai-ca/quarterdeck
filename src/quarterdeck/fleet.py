"""The fleet is an index of project declarations, never another configuration."""

from pathlib import Path

from . import ship as shipmod
from .ship import ShipError


def project_of(ship: shipmod.Ship) -> Path | None:
    """The project owning this office or non-template ship, when it has one."""
    folder = ship.folder
    if getattr(ship, 'kind', None) == 'office' and folder.name == 'office':
        return folder.parent
    if folder.parent.name == 'ships' and folder.name not in shipmod.TEMPLATES:
        return folder.parent.parent
    return None


def declaration_files(ship: shipmod.Ship) -> tuple:
    """The only peer files a jailed post needs; never a peer runtime or home."""
    project = project_of(ship)
    if project is None:
        return (ship.folder / 'ship.toml',)
    folders = [project / 'office']
    ships = project / 'ships'
    if ships.is_dir() and not ships.is_symlink():
        folders += [folder for folder in sorted(ships.iterdir())
                    if folder.name not in shipmod.TEMPLATES and folder.name != 'office']
    files = []
    for folder in folders:
        if not shipmod.valid_name(folder.name) or '@' in folder.name or folder.is_symlink():
            continue
        file = folder / 'ship.toml'
        if folder.is_dir() and file.is_file() and not file.is_symlink():
            files.append(file)
    return tuple(files)


def peers(ship: shipmod.Ship) -> dict[str, shipmod.Ship]:
    """Fixed project office and direct non-template ships, keyed by address name."""
    if ship.folder.parent.name != 'ships' and getattr(ship, 'kind', None) != 'office':
        return {ship.folder.name: ship}
    found = {}
    for file in declaration_files(ship):
        folder = file.parent
        try:
            candidate = shipmod.load(folder)
            if folder.name == 'office' and folder.parent.name != 'ships' and getattr(candidate, 'kind', None) != 'office':
                continue
            found[folder.name] = candidate
        except (ShipError, OSError):
            continue  # an unavailable destination remains waiting, never guessed
    return found


def subscription_homes(ship: shipmod.Ship):
    """Inspect fleet declarations alone, without validating peers' programs, seats or homes."""
    for file in declaration_files(ship):
        folder = file.parent
        if folder == ship.folder:
            continue
        try:
            data, _ = shipmod.toml_table(file.read_text())
        except (OSError, UnicodeError):
            continue
        if data is None:
            continue
        head = data.get("ship", {})
        if folder.name == "office" and (not isinstance(head, dict) or head.get("kind") != "office"):
            continue
        pools = data.get("pools", {})
        if not isinstance(pools, dict):
            continue
        for name, table in pools.items():
            if not isinstance(table, dict) or not name.isprintable() or "api_key" in table or "endpoint" in table:
                continue
            try:
                home = shipmod.pool_config_dir(table, folder)
                if home is not None:
                    yield folder.name, shipmod.Pool(name, table["provider"], config_dir=home)
            except (ShipError, KeyError, TypeError, ValueError, RuntimeError):
                continue
