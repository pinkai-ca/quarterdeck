"""Information-only inventory of declared Grok workspace hooks, MCP and LSP.

No command, URL, header, environment value or hook body is rendered or executed.
Native activation and dynamic extensions are not inferred from a file inventory.
"""

from __future__ import annotations

import json
import os
import stat
import tomllib
from pathlib import Path

from ..ship import ShipError
from .grok_workspace import _trust_scope as trust_scope

MAX_BYTES = 1024 * 1024
MAX_FILES = 512


def label(value: str | Path) -> str:
    return json.dumps(str(value), ensure_ascii=True)


def metadata(path: Path):
    try:
        return path.lstat(), None
    except FileNotFoundError:
        return None, None
    except OSError:
        return None, "metadata unreadable"


def document(path: Path, *, toml: bool = False) -> tuple[dict | None, str | None]:
    info, problem = metadata(path)
    if problem or info is None:
        return None, problem
    descriptor = None
    try:
        if not stat.S_ISREG(info.st_mode):
            return None, "linked or non-regular source"
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            return None, "non-regular source"
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            content = stream.read(MAX_BYTES + 1)
        after = path.lstat()
        def identity(state):
            return (state.st_dev, state.st_ino, state.st_mode, state.st_size,
                    state.st_mtime_ns, state.st_ctime_ns)
        if (len(content) > MAX_BYTES or identity(info) != identity(opened)
                or identity(info) != identity(after)):
            return None, "source changed or exceeds inventory bound"
        data = tomllib.loads(content.decode("utf-8")) if toml else json.loads(content)
        if not isinstance(data, dict):
            return None, "unrecognized source shape"
        return data, None
    except (OSError, ValueError, UnicodeError):
        return None, "source unreadable or unrecognized"
    finally:
        if descriptor is not None:
            os.close(descriptor)


def inventory(cwd: Path, environment: dict[str, str]) -> str:
    """Return a notice even for incomplete inventory; never a workspace refusal."""
    imports = {"hooks": [], "MCP servers": [], "LSP": []}
    unknown = {"hooks": [], "MCP servers": [], "LSP": []}

    def unsure(kinds, path, reason):
        for kind in kinds:
            unknown[kind].append(f"{label(path)} ({reason})")

    def servers(path, data, key):
        entries = data.get(key, {})
        if not isinstance(entries, dict) or any(not isinstance(value, dict) for value in entries.values()):
            unsure(("MCP servers",), path, "unrecognized server declarations")
            return
        if entries:
            imports["MCP servers"].append(f"{label(path)} names [{', '.join(label(name) for name in sorted(entries))}]")

    def hooks(path):
        data, problem = document(path)
        if problem:
            unsure(("hooks",), path, problem)
        elif data is not None:
            events = data.get("hooks")
            if not isinstance(events, dict) or any(not isinstance(value, list) for value in events.values()):
                unsure(("hooks",), path, "unrecognized hook declarations")
            elif events:
                imports["hooks"].append(f"{label(path)} events [{', '.join(label(name) for name in sorted(events))}]")

    def lsp(path, *, manifest=False):
        info, problem = metadata(path)
        if problem or info is None:
            if problem:
                unsure(("LSP",), path, problem)
            return
        descriptor = None
        try:
            if not stat.S_ISREG(info.st_mode):
                unsure(("LSP",), path, "linked or non-regular LSP source")
                return
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            opened = os.fstat(descriptor)
            after = path.lstat()
            def identity(state):
                return (state.st_dev, state.st_ino, state.st_mode, state.st_size,
                        state.st_mtime_ns, state.st_ctime_ns)
            if not stat.S_ISREG(opened.st_mode) or identity(info) != identity(opened) or identity(info) != identity(after):
                unsure(("LSP",), path, "LSP source changed or unrecognized")
            elif manifest:
                unsure(("LSP",), path, "plugin manifest LSP layout/activation unknown")
            else:
                # Only locations are established, not JSON fields or types.
                # Confirm readability without reading any configuration bytes.
                imports["LSP"].append(f"declared, shape unknown {label(path)}")
        except OSError:
            unsure(("LSP",), path, "LSP source unreadable")
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def plugin_lsp(path):
        info, problem = metadata(path)
        if problem or info is None:
            if problem:
                unsure(("LSP",), path, problem)
            return
        if not stat.S_ISDIR(info.st_mode):
            unsure(("LSP",), path, "linked or non-directory plugin source")
            return
        try:
            plugins = []
            for entry in path.iterdir():
                if len(plugins) >= MAX_FILES:
                    unsure(("LSP",), path, "plugin directory exceeds inventory bound")
                    break
                plugins.append(entry)
            for plugin in sorted(plugins):
                info, problem = metadata(plugin)
                if problem or info is None or not stat.S_ISDIR(info.st_mode):
                    unsure(("LSP",), plugin, problem or "unrecognized plugin source")
                    continue
                lsp(plugin / ".lsp.json")
                lsp(plugin / "plugin.json", manifest=True)
        except OSError:
            unsure(("LSP",), path, "plugin directory unreadable")

    try:
        root, repository = trust_scope(cwd, environment)
        resolved = cwd.resolve(strict=True)
        relative = resolved.relative_to(root)
        scopes = [root]
        for part in relative.parts:
            scopes.append(scopes[-1] / part)
        if len(scopes) > 128:
            raise ValueError("inventory scope bound")
    except (ShipError, OSError, RuntimeError, ValueError):
        return (f"Grok trusted workspace {label(cwd)}: project hooks unknown; project MCP servers unknown; LSP: unknown "
                "(repository scope unreadable or unrecognized); inventory only, native activation not inspected")

    for folder in scopes:
        path = folder / ".mcp.json"
        data, problem = document(path)
        if problem:
            unsure(("MCP servers",), path, problem)
        elif data is not None:
            if "mcpServers" not in data:
                unsure(("MCP servers",), path, "unrecognized MCP source shape")
            else:
                servers(path, data, "mcpServers")

        native = folder / ".grok"
        info, problem = metadata(native)
        if problem or (info is not None and not stat.S_ISDIR(info.st_mode)):
            unsure(imports, native, problem or "linked or non-directory source root")
            continue
        if info is None:
            continue

        lsp(native / "lsp.json")
        plugin_lsp(native / "plugins")

        path = native / "config.toml"
        data, problem = document(path, toml=True)
        if problem:
            unsure(imports, path, problem)
        elif data is not None:
            servers(path, data, "mcp_servers")
            if any(key in data for key in ("plugins", "version_overrides", "experiments")):
                unsure(imports, path, "indirect or conditional imports not inspected")

        path = native / "hooks"
        info, problem = metadata(path)
        if problem or (info is not None and not stat.S_ISDIR(info.st_mode)):
            unsure(("hooks",), path, problem or "linked or non-directory hook source")
        elif info is not None:
            try:
                files = []
                for entry in path.iterdir():
                    if len(files) >= MAX_FILES:
                        unsure(("hooks",), path, "hook directory exceeds inventory bound")
                        break
                    files.append(entry)
                for entry in sorted(files):
                    if entry.suffix == ".json":
                        hooks(entry)
            except OSError:
                unsure(("hooks",), path, "hook directory unreadable")

        for name in ("plugins", "agents", "personas", "roles", "workflows"):
            path = native / name
            info, problem = metadata(path)
            if problem or info is not None:
                unsure(imports, path, problem or "indirect imports not inspected")

    parts = []
    for kind in imports:
        known = "; ".join(imports[kind])
        if unknown[kind]:
            detail = (known + "; " if known else "") + "unknown: " + "; ".join(unknown[kind])
        else:
            detail = known or "none declared in inspected sources"
        heading = "LSP:" if kind == "LSP" else f"project {kind}"
        parts.append(f"{heading} {detail}")
    scope = f"whole repository {label(root)}" if repository else f"no repository; grant {label(root)}"
    return (f"Grok trusted workspace {label(cwd)} ({scope}): "
            + "; ".join(parts) + "; inventory only, native activation not inspected")
