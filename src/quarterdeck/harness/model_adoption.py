"""Launch values for explicit adoption of a running model."""

from datetime import datetime, timezone
import json
from pathlib import Path
import re

from .. import seat_files
from ..ship import ShipError


# These exact names are paired with native IDs in the measured Claude model
# control evidence. The Fable pair has narrower release evidence.
_CLAUDE_LABELS = {
    "Haiku 5.5": "claude-haiku-5-5",
    "Sonnet 5.5": "claude-sonnet-5-5",
    "Opus 5.5": "claude-opus-5-5",
    "Fable 5.2": "claude-fable-5-2",
}
CODEX_FOOTER_ID = re.compile(r"GPT-[0-9]+(?:\.[0-9]+)*(?:-[A-Za-z0-9]+)*")
LOWERCASE_ID = re.compile(r"[a-z0-9][a-z0-9._:/+@-]*")


def _literal(value):
    return (value if isinstance(value, str) and 0 < len(value) <= 200
            and all(char.isprintable() and not char.isspace() for char in value) else None)


def _claude_catalog_conflicts(seat, target, label):
    from .claude import catalog_display

    now = datetime.now(timezone.utc)
    for path in (seat.home / "cache" / "model-catalog").glob("*.json"):
        raw = seat_files.read_text(path)
        if raw is None:
            continue
        catalog = json.loads(raw)
        try:
            rows = catalog["catalog"]["config"]["models"]
        except (KeyError, TypeError):
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            ident, name = row.get("id"), row.get("name")
            if (ident, name) == (target, label) or (ident != target and name != label):
                continue
            # A fresh conflicting pair is evidence against the measured
            # default name. Check freshness at adoption time.
            if catalog_display(catalog, ident, launched=now, now=now):
                return True
    return False


def _claude_model(seat, record, value):
    from .claude import SYNTHETIC_MODEL, _synthetic_model, inspect_entries, set_model_match, user_notice

    if value == SYNTHETIC_MODEL:
        return None

    latest, command = None, None
    for row in inspect_entries(record):
        if row.get("isSidechain") or row.get("isCompactSummary") is True:
            continue
        message = row.get("message")
        if not isinstance(message, dict):
            continue
        if row.get("type") == "assistant" and message.get("model"):
            if not _synthetic_model(row, message):
                latest, command = message["model"], None
        elif row.get("type") == "user" and not user_notice(row):
            content = message.get("content")
            row_version = row.get("version")
            if (isinstance(content, str) and content.startswith("<local-command-stdout>")
                    and (match := set_model_match(content, row_version))):
                latest = match.group(1)
                another = (set_model_match(content[:match.start()], row_version)
                           or set_model_match(content[match.end():], row_version))
                command = row_version, bool(another)
    if latest != value:
        return None
    if command is None:
        return _literal(value)
    version, ambiguous = command
    target = seat.harness.model_identity(_CLAUDE_LABELS.get(value), version=version)
    if ambiguous or target is None or Path("/etc/claude-code").exists():
        return None
    settings = seat_files.read_text(seat.home / "settings.json")
    if settings is not None:
        native = json.loads(settings)
        if not isinstance(native, dict) or "modelPicker" in native:
            return None
    return None if _claude_catalog_conflicts(seat, target, value) else target


def _antigravity_model(value, screen):
    from .antigravity_models import MODEL_IDENTITIES

    label = MODEL_IDENTITIES.get(value, value)
    if screen is not None:
        # The footer puts family and tier in separate fields. Both must come
        # from this frame, since the family alone does not identify the tier.
        effort = screen.shows.get("effort")
        if " (" not in label and effort:
            label = f"{label} ({effort.capitalize()})"
        elif not effort or not label.endswith(f" ({effort.capitalize()})"):
            return None
    candidates = [native for native, shown in MODEL_IDENTITIES.items() if shown == label and native != shown]
    if candidates:
        return candidates[0] if len(candidates) == 1 else None
    return value if screen is None and LOWERCASE_ID.fullmatch(value) else None


def _pi_footer_model(value, screen):
    from .pi import CLIPPED_OFF_FOOTER, FOOTER

    if screen.box is None or screen.interruption is not None or value == "no-model":
        return None
    footer = screen.text.rstrip().splitlines()[-1]
    if CLIPPED_OFF_FOOTER.fullmatch(footer):
        return _literal(value)  # the screen reader proved the model survived this clipping
    match = FOOTER.fullmatch(footer)
    if match is not None and match["model"] == value:
        # Pi can clip the right-hand field without a mark. A complete effort
        # or provider prefix, or more padding than clipping leaves, proves the ID fit.
        before_model = footer[match.end("stats"):match.start("model")]
        if match["level"] or before_model.strip() or len(before_model) >= 4:
            return _literal(value)
    return None


def launch_model(seat, record, state, screen):
    """Resolve the record-first model or refuse before any settings are written."""
    recorded = state.model if state else None
    value = recorded or (screen.shows.get("model") if screen else None)
    if not value:
        return None
    name = seat.harness.name
    try:
        if not isinstance(value, str):
            native = None
        elif name == "claude":
            native = _claude_model(seat, record, value) if recorded else None
        elif name == "antigravity":
            native = _antigravity_model(value, None if recorded else screen)
        elif name == "codex" and not recorded:
            native = (seat.harness.model_identity(value) if CODEX_FOOTER_ID.fullmatch(value)
                      else _literal(value) if LOWERCASE_ID.fullmatch(value) else None)
        elif name == "pi" and not recorded:
            native = _pi_footer_model(value, screen)
        else:
            # This also refuses Grok's spaced subscription labels. Its keyed
            # footer and record IDs pass through with their literal spelling.
            native = _literal(value)
    except (OSError, ValueError):
        native = None
    if native is None:
        raise ShipError(f"{seat.name}: cannot adopt model {value!r}. Use qd set {seat.name} model=<launch-id>")
    return native
