"""Harness adapters, one per agent program, found by name."""

from __future__ import annotations

from ..ship import ShipError
from .base import Harness

NAMES = ("fake", "claude", "codex", "antigravity")


def get(name: str) -> Harness:
    if name == "fake":
        from .fake import FakeHarness

        return FakeHarness()
    if name == "claude":
        from .claude import ClaudeHarness

        return ClaudeHarness()
    if name == "codex":
        from .codex import CodexHarness

        return CodexHarness()
    if name == "antigravity":
        from .antigravity import AntigravityHarness

        return AntigravityHarness()
    raise ShipError(f"no harness {name!r}; known: {', '.join(NAMES)}")
