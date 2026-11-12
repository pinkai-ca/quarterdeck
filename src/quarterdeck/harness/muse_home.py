"""Pure Muse home-output draft; no native calls, credential reads or filesystem writes.

The settings field names come from authorized historical documentation. Their
current-version native effect remains pending independent measurement.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from ..ship import ShipError
from .home import merged


def contributor(model: object) -> bool:
    return isinstance(model, str) and model.strip().casefold().endswith("-contributor")


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
    settings.update(schema_version=1, provider="meta", model=selected)
    if effort is not None:
        settings["reasoning_effort"] = effort
    context = settings.setdefault("context", {})
    if not isinstance(context, dict):
        raise ShipError("Muse settings context must be a table")
    # Historical native switches, not a claim of measured 1.4.15 suppression.
    context.update(foreign_personal_rules=False, foreign_personal_skills=False)
    return settings


def environment(home: Path) -> dict[str, str]:
    """Proposed paths only; caller must build/validate the private directories.

    The canonical auth link stays directly in home for the existing shared
    credential Participant/reseed machinery. Native MUSE_AUTH_PATH behavior
    must be measured before this draft is used for a launch.
    """
    return {"HOME": str(home / "user-home"),
            "XDG_CONFIG_HOME": str(home / "config"), "XDG_DATA_HOME": str(home / "data"),
            "XDG_CACHE_HOME": str(home / "cache"), "XDG_STATE_HOME": str(home / "state"),
            "MUSE_AUTH_PATH": str(home / "auth.json"),
            "MUSE_NO_AUTO_UPDATE": "1", "MUSE_LOGIN": "0"}
