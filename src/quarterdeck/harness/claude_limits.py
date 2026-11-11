"""Claude's native allowance fields, model families and safe shape diagnostics."""

from dataclasses import replace
import json
import math
import re

from .json_record import when
from .limits import Usage, Window

PERIODS = {"five_hour": "5h", "seven_day": "7d"}
FAMILIES = {"fable": "fable", "sonnet": "fable", "opus": "opus", "haiku": "haiku"}
WALL_SEVERITIES = {"critical", "exceeded", "error"}


def family(model: str | None) -> str | None:
    if not isinstance(model, str):
        return None
    tokens = re.findall(r"[a-z]+", model.lower())
    if len({FAMILIES[token] for token in tokens if token in FAMILIES}) != 1:
        return None
    match = re.match(r"^(?:claude[- ])?(fable|sonnet|opus|haiku)(?=$|[- .\[\d])", model.lower())
    return FAMILIES[match[1]] if match else None


def shape(value, depth=0):
    """Key names and types only, including malformed nested scope; never scalar values."""
    if isinstance(value, dict) and depth < 4:
        return {key: shape(item, depth + 1) for key, item in value.items() if isinstance(key, str)}
    if isinstance(value, list) and depth < 4:
        return {"list_item_types": sorted({type(item).__name__ for item in value})}
    return type(value).__name__


def fraction(value):
    return value / 100 if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 100 else None


def snapshot(data: dict, now) -> Usage:
    windows, resets = [], {}
    for key, name in PERIODS.items():
        row = data.get(key) or {}
        if isinstance(row, dict) and (reset := when(row.get("resets_at"))) is not None:
            resets[name] = reset
        if not isinstance(row, dict) or row.get("utilization") is None:
            continue  # Preserve the legacy general-window reader.
        try:
            used = float(row["utilization"]) / 100.0
        except (TypeError, ValueError):
            return Usage((), now, "bad JSON: utilization not a number")
        windows.append(Window(name, used, when(row.get("resets_at"))))
    scoped = {}

    def keep(candidate):
        old = scoped.get(candidate.name)
        if old is not None:
            # The same native cap may have both a named window and a limits entry.
            if (old.used, old.resets_at, old.error, old.uncertainty) == (
                    candidate.used, candidate.resets_at, candidate.error, candidate.uncertainty):
                candidate = replace(candidate, exhausted=old.exhausted or candidate.exhausted)
            else:
                candidate = Window(candidate.name, None, None, group=candidate.group,
                                   error="conflicting model allowance representations")
        scoped[candidate.name] = candidate

    for period, name in PERIODS.items():
        for native, canonical in FAMILIES.items():
            key = f"{period}_{native}"
            if key not in data or data[key] is None:
                continue  # A null optional window is not a reported model allowance.
            row = data[key]
            used = fraction(row.get("utilization")) if isinstance(row, dict) else None
            reset = when(row.get("resets_at")) if isinstance(row, dict) else None
            keep(Window(f"{canonical} / {name}", used, reset, group=canonical,
                        error=None if used is not None and reset else "model allowance fraction or reset unavailable"))

    unmapped = []
    limits = data.get("limits", [])
    if not isinstance(limits, list):
        unmapped.append({"limits": shape(limits)})
        keep(Window("unknown model / unknown period", None, None, group="unknown",
                    error="model allowance list unavailable"))
        limits = []
    for row in limits:
        scope = row.get("scope") if isinstance(row, dict) else None
        if isinstance(row, dict) and "scope" in row and scope is None:
            unmapped.append(shape(row))  # Confirmed non-model scope; no invented model window.
            continue
        model = scope.get("model") if isinstance(scope, dict) else None
        label = model.get("display_name") if isinstance(model, dict) else None
        canonical = family(label)
        reset = when(row.get("resets_at")) if isinstance(row, dict) else None
        matching = [period for period, known in resets.items() if reset is not None and reset == known]
        period = matching[0] if len(matching) == 1 else "unknown period"
        used = fraction(row.get("percent")) if isinstance(row, dict) else None
        severity = row.get("severity") if isinstance(row, dict) else None
        active = row.get("is_active") if isinstance(row, dict) else None
        valid = canonical is not None and used is not None and reset is not None and isinstance(severity, str) and type(active) is bool
        uncertainty = None if len(matching) == 1 else "model allowance period unknown: no unique general reset match"
        error = None if valid else "model allowance fields or family unavailable"
        if error or uncertainty:
            unmapped.append(shape(row))
        keep(Window(f"{canonical or 'unknown model'} / {period}", used, reset, group=canonical or "unknown",
                    error=error, exhausted=isinstance(severity, str) and severity.lower() in WALL_SEVERITIES,
                    uncertainty=uncertainty))
    windows.extend(scoped.values())
    diagnostics = ((f"unmapped limits: {len(unmapped)}; key/type shapes: " + json.dumps(unmapped, sort_keys=True),) if unmapped else ())
    return Usage(tuple(windows), now, diagnostics=diagnostics) if windows else Usage((), now, "no utilization in response", diagnostics=diagnostics)
