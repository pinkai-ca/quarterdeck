"""Read-only Git scope and the project sources a Grok seat cannot import."""

from __future__ import annotations

import os
import stat
import subprocess
import tomllib
from pathlib import Path

from ..ship import ShipError

GIT_REDIRECTS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_CONFIG", "GIT_CONFIG_COUNT",
                 "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM")

PROJECT_SOURCES = (
    "Agents.md", "Claude.md", "CLAUDE.md", "CLAUDE.local.md", "AGENT.md", "AGENTS.md",
    ".claude/CLAUDE.md", ".claude/CLAUDE.local.md",
    ".grok/rules", ".claude/rules", ".cursor/rules",
    ".mcp.json", ".grok/hooks", ".claude/settings.json", ".claude/settings.local.json", ".cursor/hooks.json",
    ".grok/workflows", ".grok/skills", ".grok/commands", ".agents/skills", ".agents/commands",
    ".claude/skills", ".claude/commands", ".cursor/skills",
    ".grok/agents", ".grok/personas", ".grok/roles", ".grok/plugins", ".grok/lsp.json",
)


def metadata(path: Path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise ShipError(f"{path}: Grok project source metadata is unreadable") from None


def discover(cwd: Path, environment: dict[str, str]) -> list[Path]:
    """One bounded local Git metadata query; no Grok/provider process or write."""
    for name in GIT_REDIRECTS:
        if os.environ.get(name):
            raise ShipError(f"Grok Git discovery redirect {name} is set; scope is unknown")
    try:
        cwd = cwd.resolve(strict=True)
        done = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--path-format=absolute", "--show-toplevel"],
            env={**os.environ, **environment, "LC_ALL": "C"},
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, RuntimeError, UnicodeError, subprocess.TimeoutExpired):
        raise ShipError(f"{cwd}: Grok Git discovery scope is unreadable or unavailable") from None
    if done.returncode == 128 and done.stderr.strip() == "fatal: not a git repository (or any of the parent directories): .git":
        if any(metadata(parent / ".git") is not None for parent in (cwd, *cwd.parents)):
            raise ShipError(f"{cwd}: Git refused an existing repository marker; Grok scope is unknown")
        return [cwd]  # positive ordinary non-repository result, not a failed-query default
    roots = done.stdout.splitlines()
    if done.returncode != 0 or done.stderr.strip() or len(roots) != 1 or not Path(roots[0]).is_absolute():
        raise ShipError(f"{cwd}: Grok Git discovery scope is unknown or ambiguous")
    try:
        root = Path(roots[0]).resolve(strict=True)
    except (OSError, RuntimeError):
        raise ShipError(f"{cwd}: Grok Git root is unreadable") from None
    if not cwd.is_relative_to(root):
        raise ShipError(f"{cwd}: Grok Git root is not an ancestor; scope is ambiguous")
    scopes = [cwd]
    while scopes[-1] != root:
        scopes.append(scopes[-1].parent)
    return list(reversed(scopes))


def read_configuration(path: Path, before) -> dict:
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("project configuration is not a regular file")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            content = stream.read(1024 * 1024 + 1)
        after = path.lstat()
        def identity(info):
            return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if (not stat.S_ISREG(opened.st_mode) or len(content) > 1024 * 1024
                or identity(before) != identity(opened) or identity(before) != identity(after)):
            raise ValueError("project configuration changed or exceeded the read bound")
        return tomllib.loads(content.decode("utf-8"))
    finally:
        if descriptor is not None:
            os.close(descriptor)


def check(cwd: Path, environment: dict[str, str]) -> None:
    problems = []
    for folder in discover(cwd, environment):
        for relative in PROJECT_SOURCES:
            path = folder / relative
            try:
                if metadata(path) is not None:
                    problems.append(f"{path}: project source exists")
            except ShipError as error:
                problems.append(str(error))
        for name in (".grok", ".claude", ".cursor", ".agents"):
            path = folder / name
            try:
                info = metadata(path)
                if info is not None and not stat.S_ISDIR(info.st_mode):
                    problems.append(f"{path}: project source root is linked or not a directory")
            except ShipError as error:
                problems.append(str(error))
        path = folder / ".grok" / "config.toml"
        try:
            parent = metadata(path.parent)
            if parent is not None and not stat.S_ISDIR(parent.st_mode):
                continue  # the source-root obstruction above already reports this unknown scope
            info = metadata(path)
            if info is None:
                continue
            if not stat.S_ISREG(info.st_mode):
                problems.append(f"{path}: project configuration is linked or not a regular file")
                continue
            config = read_configuration(path, info)
            if "mcp_servers" in config or "plugins" in config:
                problems.append(f"{path}: project MCP or plugin configuration exists")
        except (ShipError, OSError, ValueError, UnicodeError):
            problems.append(f"{path}: project configuration is unreadable or unknown")
    if problems:
        raise ShipError("Grok project sources refused: " + "; ".join(sorted(set(problems))))
