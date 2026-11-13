"""Declared controls and defaults, without a program, account or model lookup.

Historical CLI vocabularies are not installed-version or model/account evidence.
The simulator's unrestricted text is an explicit capability, not a finite enum.
"""

from dataclasses import dataclass

from ..ship import ShipError


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
    facts = harness.effort_capability(version)
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
