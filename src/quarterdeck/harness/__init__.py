"""Harness adapters, one per agent program, found by name."""

from __future__ import annotations

from functools import lru_cache

from ..ship import ShipError
from .base import Harness

NAMES = ("fake", "claude", "codex", "antigravity", "opencode", "pi", "grok", "muse")
MODULES = {
    "fake": ("fake", "FakeHarness"),
    "claude": ("claude", "ClaudeHarness"),
    "codex": ("codex", "CodexHarness"),
    "antigravity": ("antigravity", "AntigravityHarness"),
    "opencode": ("opencode", "OpenCodeHarness"),
    "pi": ("pi", "PiHarness"),
    "grok": ("grok", "GrokHarness"),
    "muse": ("muse", "MuseHarness"),
}


@lru_cache(maxsize=None)
def _adapter_class(name: str):
    if name not in NAMES:
        raise ShipError(f"no harness {name!r}; known: {', '.join(NAMES)}")
    module_name, class_name = MODULES[name]
    try:
        import importlib

        module = importlib.import_module(f".{module_name}", __package__)
    except ImportError as error:
        raise ShipError(f"harness {name!r} is not built yet: {error}") from None
    return getattr(module, class_name)


def get(name: str) -> Harness:
    return _adapter_class(name)()


def inherited_markers() -> tuple[str, ...]:
    """Every adapter's session markers: a seat of any program drops another program's as well."""
    found: list[str] = []
    for name in NAMES:
        try:
            markers = _adapter_class(name).inherited_markers
        except ShipError:
            continue
        found += [marker for marker in markers if marker not in found]
    return tuple(found)


def provider_of(name: str) -> str | None:
    """The adapter, not a generic mapping, declares its native company."""
    return _adapter_class(name).native_provider


def native_name(provider: str) -> str | None:
    from ..providers import canonical
    company = canonical(provider)
    names = [name for name in NAMES if provider_of(name) == company]
    if len(names) > 1:
        raise ShipError(f"provider {company}: native harness is ambiguous ({', '.join(names)})")
    return names[0] if names else None


def native_for(provider: str) -> Harness:
    name = native_name(provider)
    if name is None:
        raise ShipError(f"provider {provider!r} has no installed native harness; declare a compatible endpoint")
    return get(name)
