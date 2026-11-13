"""Claude's native allowance fields, model families and safe shape diagnostics."""

from dataclasses import replace
from datetime import datetime, timezone
import json
import math
import re

from .json_record import when
from .limits import Usage, Wall, Window

PERIODS = {"five_hour": "5h", "seven_day": "7d"}
FAMILIES = {"fable": "fable", "sonnet": "sonnet", "opus": "opus", "haiku": "haiku"}
WARNING_SEVERITIES = {"critical", "exceeded", "error"}  # Near or at a limit: warning evidence, never a wall.
NATIVE_PERIODS = {"weekly_scoped": "7d"}
RENDER_GROUPS = {"session", "weekly"}  # Display group examples, never a period classifier.
RESET_TOLERANCE = 2.0
DIAGNOSTIC_WORD_MAX = 32


def family(model: str | None) -> str | None:
    if not isinstance(model, str):
        return None
    tokens = re.findall(r"[a-z]+", model.lower())
    if len({FAMILIES[token] for token in tokens if token in FAMILIES}) != 1:
        return None
    match = re.match(r"^(?:claude[- ])?(fable|sonnet|opus|haiku)(?=$|[- .\[\d])", model.lower())
    return FAMILIES[match[1]] if match else None


def shape(value, depth=0):
    """Only top-level kind/group may expose bounded ASCII words; everything else is types."""
    if isinstance(value, dict) and depth < 4:
        return {key: ({"type": "str", "word": item}
                      if depth == 0 and key in ("kind", "group") and isinstance(item, str)
                      and re.fullmatch(r"[A-Za-z0-9_-]{1," + str(DIAGNOSTIC_WORD_MAX) + "}", item)
                      else shape(item, depth + 1))
                for key, item in value.items() if isinstance(key, str)}
    if isinstance(value, list) and depth < 4:
        return {"list_item_types": sorted({type(item).__name__ for item in value})}
    return type(value).__name__


def fraction(value):
    return value / 100 if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 100 else None


def period_for(row, reset, general):
    kind = row.get("kind") if isinstance(row, dict) else None
    if isinstance(kind, str) and kind in NATIVE_PERIODS:
        return NATIVE_PERIODS[kind], None
    matching = [period for period, known in general.items() if reset is not None
                and abs((reset - known).total_seconds()) <= RESET_TOLERANCE]
    if len(matching) == 1:
        return matching[0], None
    return "unknown period", "model allowance period unknown: no unique general reset match within 2 seconds"


def model_window(canonical, period, used, reset, **metadata):
    replaces = ((f"{canonical} / unknown period", RESET_TOLERANCE),) if canonical in FAMILIES and period != "unknown period" else ()
    label = canonical if canonical != "unknown" else "unknown model"
    return Window(f"{label} / {period}", used, reset, group=canonical, replaces=replaces, **metadata)


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
                candidate = replace(candidate, warning=old.warning or candidate.warning)
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
            keep(model_window(canonical, name, used, reset,
                        error=None if used is not None and reset else "model allowance fraction or reset unavailable"))

    unmapped, observations, container_notes = [], [], []
    limits = data.get("limits", [])
    if not isinstance(limits, list):
        container_notes.append("limits container unavailable; key/type shape: " + json.dumps(shape(limits), sort_keys=True))
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
        period, uncertainty = period_for(row, reset, resets)
        used = fraction(row.get("percent")) if isinstance(row, dict) else None
        severity = row.get("severity") if isinstance(row, dict) else None
        active = row.get("is_active") if isinstance(row, dict) else None
        valid = canonical is not None and used is not None and reset is not None and isinstance(severity, str) and type(active) is bool
        error = None if valid else "model allowance fields or family unavailable"
        unknown_kind = isinstance(row, dict) and "kind" in row and (not isinstance(row["kind"], str) or row["kind"] not in NATIVE_PERIODS)
        unknown_group = isinstance(row, dict) and "group" in row and (not isinstance(row["group"], str) or row["group"] not in RENDER_GROUPS)
        if error or uncertainty or unknown_kind or unknown_group:
            observations.append(shape(row))
        keep(model_window(canonical or "unknown", period, used, reset,
                    error=error, warning=isinstance(severity, str) and severity.lower() in WARNING_SEVERITIES,
                    uncertainty=uncertainty))
    windows.extend(scoped.values())
    diagnostics = list(container_notes)
    if unmapped:
        diagnostics.append(f"unmapped limits: {len(unmapped)}; key/type shapes: " + json.dumps(unmapped, sort_keys=True))
    if observations:
        diagnostics.append(f"limits diagnostics: {len(observations)}; key/type shapes: " + json.dumps(observations, sort_keys=True))
    diagnostics = tuple(diagnostics)
    return Usage(tuple(windows), now, diagnostics=diagnostics) if windows else Usage((), now, "no utilization in response", diagnostics=diagnostics)


# Claude Code 2.1.317 writes a refused request as a synthetic assistant row. The usage-limit form, read
# from its installed source and the shape of a recorded row: ``error`` "rate_limit", ``apiError``
# "usage_limit_reached", and ``apiErrorParams.rate_limit_info`` with ``status`` "rejected", the
# provider's ``rateLimitType`` and ``resetsAt`` in Unix seconds. Only these types name a window the
# usage reading also names. Every other form, among them a plain "rate_limit", a model that needs
# usage credits, an overage limit or an unknown type, stays unknown and is no wall.
REFUSED_WINDOWS = {"five_hour": ("5h", None), "seven_day": ("7d", None),
                   "seven_day_sonnet": ("sonnet / 7d", "sonnet"), "seven_day_opus": ("opus / 7d", "opus")}


def refused_at_limit(entry: dict, key: str, at: datetime | None) -> Wall | None:
    """The wall a recorded usage-limit refusal proves, for its pool and window until its reset; else None."""
    if not (isinstance(entry, dict) and entry.get("type") == "assistant" and entry.get("isApiErrorMessage") is True
            and entry.get("error") == "rate_limit" and entry.get("apiError") == "usage_limit_reached"):
        return None
    params = entry.get("apiErrorParams")
    info = params.get("rate_limit_info") if isinstance(params, dict) else None
    if not isinstance(info, dict) or info.get("status") != "rejected":
        return None
    kind, seconds = info.get("rateLimitType"), info.get("resetsAt")
    if not isinstance(kind, str) or kind not in REFUSED_WINDOWS or type(seconds) is not int or at is None:
        return None
    try:
        reset = datetime.fromtimestamp(seconds, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    if reset <= at:
        return None
    window, group = REFUSED_WINDOWS[kind]
    return Wall(key, at, reset, f"refused at its {window} limit",
                signature=f"claude:usage_limit_reached:{kind}:{seconds}", group=group)
