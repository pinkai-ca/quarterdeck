"""Harness adapters, one per agent program, found by name."""

from __future__ import annotations

from ..ship import ShipError
from .base import Harness

NAMES = ("fake", "claude", "codex", "antigravity", "opencode", "pi")
MODULES = {
    "fake": ("fake", "FakeHarness"),
    "claude": ("claude", "ClaudeHarness"),
    "codex": ("codex", "CodexHarness"),
    "antigravity": ("antigravity", "AntigravityHarness"),
    "opencode": ("opencode", "OpenCodeHarness"),
    "pi": ("pi", "PiHarness"),
}


def get(name: str) -> Harness:
    if name not in NAMES:
        raise ShipError(f"no harness {name!r}; known: {', '.join(NAMES)}")
    module_name, class_name = MODULES[name]
    try:
        import importlib

        module = importlib.import_module(f".{module_name}", __package__)
    except ImportError as error:
        raise ShipError(f"harness {name!r} is not built yet: {error}") from None
    return getattr(module, class_name)()
