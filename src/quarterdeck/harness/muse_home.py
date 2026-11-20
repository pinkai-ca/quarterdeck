"""Muse 1.4.20's private native HOME/XDG layout and declared settings."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat

from ..ship import ShipError
from .. import durable
from .home import merged


def contributor(model: object) -> bool:
    if not isinstance(model, str):
        return False
    name = model.strip().casefold()
    return name.endswith("-contributor") or name == "muse-spark" or name.startswith("muse-spark-")


def configuration(model: str | None, effort: str | None, template: dict, overrides: dict) -> dict:
    """Seed one explicit model; never inherit a native catalog's default.

    A contributor choice in any declared layer is an error even when another
    layer would replace it. A safe launch model does not prove the live model.
    """
    for source, value in (("seat model", model), ("ship Muse template", template.get("model")),
                          ("seat Muse settings", overrides.get("model"))):
        if contributor(value):
            raise ShipError(f"{source}: contributor model is not allowed; declare a noncontributor model")
    settings = deepcopy(merged(template, overrides))
    selected = model if model is not None else settings.get("model")
    if not isinstance(selected, str) or not selected or selected.strip() != selected:
        raise ShipError("Muse requires an explicit noncontributor model in the seat or ship settings")
    # Measured on Wonder at 1.4.20 with the explicit per-run off flag too.
    # The screen must still prove the effective state before the first input.
    settings.update(schema_version=1, provider="meta", model=selected, contributor_mode_enabled=False)
    if effort is not None:
        settings["reasoning_effort"] = effort
    context = settings.setdefault("context", {})
    if not isinstance(context, dict):
        raise ShipError("Muse settings context must be a table")
    # These are explicit launch overrides, not a claim about effective tooling.
    context.update(foreign_personal_rules=False, foreign_personal_skills=False)
    return settings


def environment(home: Path) -> dict[str, str]:
    """Native 1.4.20 paths measured with explicit HOME and XDG variables."""
    return {"HOME": str(home / "user-home"),
            "XDG_CONFIG_HOME": str(home / "config"), "XDG_DATA_HOME": str(home / "data"),
            "XDG_CACHE_HOME": str(home / "cache"), "XDG_STATE_HOME": str(home / "state"),
            "MUSE_NO_AUTO_UPDATE": "1", "TMPDIR": str(home / "tmp")}


def directory(path: Path, *, create: bool = False) -> None:
    """An owned real directory; never follow an inherited home link."""
    if create and not path.exists() and not path.is_symlink():
        path.mkdir(mode=0o700)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ShipError(f"{path}: Muse home directory is not an owned real directory")


FOLDERS = ("user-home", "config", "config/muse", "data", "cache", "state", "tmp")
_OPEN = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY | os.O_CLOEXEC


def prepare(home: Path) -> None:
    directory(home, create=True)
    for name in FOLDERS:
        directory(home / name, create=True)
    owner_only(home)


def owner_only(home: Path) -> None:
    """Mend the home and its seven folders to 0700, each through its own descriptor.

    Every folder is first proved an owned real directory by path; then each is
    opened without following a link, beneath its already opened parent, and
    set with fchmod, never chmod by path. A widened mode is mended, not refused;
    a folder that is still not 0700, or not owned, refuses. An absent folder is
    left as directory() leaves it.
    """
    directory(home)
    for name in FOLDERS:
        directory(home / name)
    try:
        root = os.open(home, _OPEN)
    except FileNotFoundError:
        return
    except OSError as error:
        raise ShipError(f"{home}: Muse home directory is not an owned real directory ({error.strerror})") from None
    opened = {"": root}
    try:
        _mend(home, root)
        for name in FOLDERS:
            parent, _, leaf = name.rpartition("/")
            if parent not in opened:
                continue
            try:
                folder = os.open(leaf, _OPEN, dir_fd=opened[parent])
            except FileNotFoundError:
                continue
            except OSError as error:
                raise ShipError(f"{home / name}: Muse home directory is not an owned real directory "
                                f"({error.strerror})") from None
            opened[name] = folder
            _mend(home / name, folder)
    finally:
        for descriptor in opened.values():
            os.close(descriptor)


def _mend(path: Path, descriptor: int) -> None:
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ShipError(f"{path}: Muse home directory is not an owned real directory")
    if stat.S_IMODE(info.st_mode) != 0o700:
        os.fchmod(descriptor, 0o700)
    info = os.fstat(descriptor)
    if stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid():
        raise ShipError(f"{path}: Muse home directory is still not mode 0700 for its owner alone")


def home_identity(home: Path) -> list[int]:
    info = home.stat()
    return [info.st_dev, info.st_ino]


def owned_json(path: Path) -> dict:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ShipError(f"{path}: Muse proof/settings is not an owned regular file")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ShipError(f"{path}: Muse proof/settings is not an object")
    return value


def write_settings(home: Path, settings: dict) -> None:
    path = home / 'config/muse/settings.json'
    if path.exists() or path.is_symlink():
        previous = owned_json(path)
        # Preserve only the measured native acknowledgement in this same home;
        # no operator configuration or unclassified native values are imported.
        if previous.get('contributor_mode_disclosure_acknowledged') is True:
            settings['contributor_mode_disclosure_acknowledged'] = True
    durable.replace_file(path, json.dumps(settings, indent=2) + '\n')
    proof = home / 'muse-settings-seed.json'
    if proof.exists() or proof.is_symlink():
        owned_json(proof)
    durable.replace_file(proof, json.dumps(dict(home=home_identity(home), model=settings['model'],
                                              digest=settings_digest(settings))) + '\n')


def settings_digest(settings: dict) -> str:
    # Native 1.4.20 adds this one-time disclosure acknowledgement itself. That
    # change is measured; every other framework-seeded field stays bound.
    projected = {key: value for key, value in settings.items()
                 if key != 'contributor_mode_disclosure_acknowledged'}
    return hashlib.sha256(json.dumps(projected, sort_keys=True).encode()).hexdigest()


def seeded_settings(home: Path) -> dict | None:
    try:
        path = home / 'config/muse/settings.json'
        settings = owned_json(path)
        proof = owned_json(home / 'muse-settings-seed.json')
        if (proof.get('home') != home_identity(home)
                or proof.get('digest') != settings_digest(settings)
                or proof.get('model') != settings.get('model')
                or settings.get('contributor_mode_enabled') is not False
                or contributor(settings.get('model'))):
            return None
        return settings
    except (OSError, ValueError, ShipError):
        return None


def remember_off(home: Path) -> None:
    path = home / 'muse-contributor-off.json'
    if path.exists() or path.is_symlink():
        owned_json(path)
    durable.replace_file(path, json.dumps(dict(home=home_identity(home), version='1.4.20')) + '\n')


def previously_off(home: Path) -> bool:
    try:
        proof = owned_json(home / 'muse-contributor-off.json')
        return proof == dict(home=home_identity(home), version='1.4.20')
    except (OSError, ValueError, ShipError):
        return False
