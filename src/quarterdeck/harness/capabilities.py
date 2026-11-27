"""Declared controls and defaults, without a program, account or model lookup.

Historical CLI vocabularies are not installed-version or model/account evidence.
The simulator's unrestricted text is an explicit capability, not a finite enum.
"""

from dataclasses import dataclass, replace

from ..ship import ShipError


def persistent_screen_queue(harness: str):
    """Optional control-side policy for a queue evidenced by the native screen."""
    if harness == "grok":
        from . import grok_screen
        return grok_screen
    return None


def complete_box_wrap_slack(harness: str, default: int) -> int:
    """The adapter's word-break margin when checking a complete draft."""
    if harness == "claude":
        from .claude_held_box import COMPLETE_BOX_WRAP_SLACK
        return COMPLETE_BOX_WRAP_SLACK
    return default


def held_box_wrap_rule(harness: str):
    """Optional rule for a program's measured held composer wraps."""
    if harness == "claude":
        from .claude_held_box import held_edge_wraps
        return held_edge_wraps
    return None


def screen_forms(harness: str):
    """Optional control-side reader for launch-bound screen forms."""
    if harness == "claude":
        from . import claude_screen_forms
        return claude_screen_forms
    return None


def follow_minimum_size(harness: str | None) -> tuple[int, int] | None:
    """Optional minimum native pane dimensions for follow's temporary resize."""
    if harness == "claude":
        return 80, 34
    return None


@dataclass(frozen=True)
class SuggestedValue:
    value: str | None
    mode: str


@dataclass(frozen=True)
class DefaultMetadata:
    model_default: bool = False
    model: SuggestedValue | None = None
    effort: SuggestedValue | None = None


@dataclass(frozen=True)
class EffortCapability:
    kind: str = "unknown"
    levels: tuple[str, ...] = ()
    measured_versions: tuple[str, ...] = ()
    complete: bool = False
    version: str | None = None
    measured: bool = False


_CLAUDE_CLI_EFFORTS = EffortCapability(
    "levels", ("low", "medium", "high", "xhigh", "max"), ("2.1.331",)
)


def effort_facts(harness, version: str | None = None) -> EffortCapability:
    """Use the measured Claude CLI vocabulary only for the release it describes."""
    if harness.name == "claude" and version in _CLAUDE_CLI_EFFORTS.measured_versions:
        return replace(_CLAUDE_CLI_EFFORTS, version=version, measured=True)
    return harness.effort_capability(version)


def default_label(*, unchecked: bool = True) -> str:
    return "program's default" + (" (not checked)" if unchecked else "")


def selection_label(value: str | None, mode: str | None, *, unchecked: bool = True) -> str | None:
    return default_label(unchecked=unchecked) if mode == "program-default" else value


def validate_mode(value, mode, key: str, context: str) -> None:
    if mode is None:  # every legacy string, including 'default', keeps its meaning
        return
    if mode not in ("named", "program-default"):
        raise ShipError(f"{context}: {key}_mode must be named or program-default")
    if mode == "named" and (not isinstance(value, str) or not value.strip()):
        raise ShipError(f"{context}: {key}_mode=named requires a nonempty {key} string")
    if mode == "program-default" and value is not None:
        raise ShipError(f"{context}: {key}_mode=program-default requires {key} absent; "
                        f"edit the declaration to remove {key} explicitly")


def validate_effort(harness, effort, mode=None, *, version=None, installed=False, context=None) -> str | None:
    """Refuse proved incompatibility; return, never hide, installed uncertainty."""
    context = context or harness.name
    validate_mode(effort, mode, "effort", context)
    if mode == "program-default" or effort is None or effort == "":
        return None
    if not isinstance(effort, str):
        raise ShipError(f"{context}: effort must be text")
    facts = effort_facts(harness, version)
    if facts.kind == "none":
        raise ShipError(f"{context}: {harness.name} takes no effort; effort {effort!r} is not supported")
    if facts.kind == "unrestricted":
        return None
    if facts.kind == "levels" and (facts.complete or installed and facts.measured):
        if effort not in facts.levels:
            raise ShipError(f"{context}: effort {effort!r} is not supported by {harness.name}; "
                            f"the levels are {', '.join(facts.levels)}")
    if installed and not facts.measured:
        return f"levels of {harness.program} {version or 'version unknown'} not measured; {effort!r} unchecked"
    return None


def validate_selection(spec, harness, *, version=None, installed=False) -> str | None:
    context = f"seat {spec.name}"
    validate_mode(spec.model, spec.model_mode, "model", context)
    if spec.model_mode == "program-default" and not harness.default_metadata().model_default:
        raise ShipError(f"{context}: {harness.name} requires an explicit endpoint model; "
                        "model_mode=program-default is not supported")
    reject_default_overrides(spec, spec.settings, scope="seat settings",
                             model_keys=harness.model_selectors, effort_keys=harness.effort_selectors)
    if harness.unclassified_default_overrides:
        reject_unclassified_overrides(spec, spec.settings, scope="seat settings")
    return validate_effort(harness, spec.effort, spec.effort_mode, version=version,
                           installed=installed, context=context)


def reject_default_overrides(spec, config, *, scope, model_keys=("model",), effort_keys=()) -> None:
    """Report selector fields and their scopes, never their possibly private values.

    Adapters supply selector names; generic code does not infer native aliases or
    remove template/settings values. Lists and profiles retain their exact scopes.
    """
    forbidden = set(model_keys) if spec.model_mode == "program-default" else set()
    if spec.effort_mode == "program-default":
        forbidden.update(effort_keys)
    if not forbidden:
        return

    def visit(value, path):
        if isinstance(value, dict):
            for key, item in value.items():
                field = f"{path}.{key}"
                if key in forbidden:
                    raise ShipError(f"seat {spec.name}: program-default conflicts with {field}; "
                                    "remove the named selector explicitly")
                visit(item, field)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f"{path}[{index}]")

    visit(config, scope)


def reject_unclassified_overrides(spec, config, *, scope) -> None:
    """A user's unclassified override cannot promise a native default.

    Only declaration-provided scopes enter here, never the operator environment
    or native account data. Adapter-generated baselines do not enter here.
    """
    if spec.model_mode != "program-default" and spec.effort_mode != "program-default":
        return
    if config:
        field = f"{scope}.{next(iter(config))}" if isinstance(config, dict) else scope
        raise ShipError(f"seat {spec.name}: {field} is unclassified for program-default; "
                        "remove that override or choose an explicit named value")


def named_record_holds(harness: str | None) -> tuple[str, ...]:
    """The exact record-hold reasons a program's adapter establishes from measured rows, which a status summary may
    say by name. Presentation only: no gate reads this, and every other unknown stays unnamed."""
    if harness == 'claude':
        from .claude import MIXED_REMOTE_INPUT
        from .claude_background import STOPPED_RESUME
        return (MIXED_REMOTE_INPUT, STOPPED_RESUME)
    return ()
