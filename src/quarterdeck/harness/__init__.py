"""Harness adapters, one per agent program, found by name."""

from __future__ import annotations

from ..ship import ShipError
from .base import Harness

NAMES = ("fake", "claude", "codex", "antigravity")


def get(name: str) -> Harness:
    if name not in NAMES:
        raise ShipError(f"no harness {name!r}; known: {', '.join(NAMES)}")
    try:
        if name == "fake":
            from .fake import FakeHarness as cls
        elif name == "claude":
            from .claude import ClaudeHarness as cls
        elif name == "codex":
            from .codex import CodexHarness as cls
        else:
            from .antigravity import AntigravityHarness as cls
    except ImportError as error:
        raise ShipError(f"harness {name!r} is not built yet: {error}") from None
    return cls()
