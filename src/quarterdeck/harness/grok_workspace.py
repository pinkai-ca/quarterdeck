"""Native repository-wide folder trust; project contents never gate readiness."""

from __future__ import annotations

import os
import stat
import subprocess
import tomllib
from pathlib import Path

from ..ship import ShipError

GIT_REDIRECTS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_CONFIG", "GIT_CONFIG_COUNT",
                 "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM")


def launch_args(cwd: Path | None) -> list[str]:
    """Keep the native trust choice local while project-tool policy is decided.

    Native --trust grants the repository root, including for a nested cwd.
    This candidate awaits a decision on its project hooks and MCP imports.
    """
    if cwd is None:
        raise ShipError("Grok launch needs the seat's explicit declared workspace")
    return ["--trust", "--cwd", str(cwd)]


def has_repository_marker(cwd: Path) -> bool:
    for folder in (cwd, *cwd.parents):
        try:
            (folder / ".git").lstat()
        except FileNotFoundError:
            continue
        return True
    return False


def trust_root(cwd: Path, environment: dict[str, str]) -> Path:
    """Resolve only the native trust identity, never inspect project sources."""
    for name in GIT_REDIRECTS:
        if os.environ.get(name):
            raise ShipError(f"Grok trust-root redirect {name} is set; clear it before launch")
    try:
        cwd = cwd.resolve(strict=True)
        done = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--path-format=absolute", "--show-toplevel"],
            env={**os.environ, **environment, "LC_ALL": "C"},
            capture_output=True, text=True, timeout=5,
        )
        if (done.returncode == 128 and done.stderr.strip()
                == "fatal: not a git repository (or any of the parent directories): .git"):
            # A rejected existing repository marker is not proof of no repository.
            if has_repository_marker(cwd):
                raise ValueError("existing repository marker")
            return cwd
        roots = done.stdout.splitlines()
        if done.returncode != 0 or done.stderr.strip() or len(roots) != 1 or not Path(roots[0]).is_absolute():
            raise ValueError("ambiguous repository root")
        root = Path(roots[0]).resolve(strict=True)
        if not cwd.is_relative_to(root):
            raise ValueError("repository root is not an ancestor")
        return root
    except (OSError, RuntimeError, ValueError, UnicodeError, subprocess.TimeoutExpired):
        raise ShipError(f"{cwd}: Grok exact trust root is unreadable or unknown; retained trust state") from None


def read_trust(path: Path) -> dict | None:
    """Read one owned native store without altering its atomic lock/rename files."""
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise ShipError(f"{path}: folder-trust metadata unreadable; retained for inspection") from None
    descriptor = None
    try:
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid():
            raise ValueError("unexpected native store")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid():
            raise ValueError("unexpected native store")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            content = stream.read(1024 * 1024 + 1)
        after = path.lstat()
        def identity(info):
            return (info.st_dev, info.st_ino, info.st_mode, info.st_uid,
                    info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if (len(content) > 1024 * 1024 or identity(before) != identity(opened)
                or identity(before) != identity(after)):
            raise ValueError("native store changed or exceeded the read bound")
        return tomllib.loads(content.decode("utf-8"))
    except (OSError, ValueError, UnicodeError):
        raise ShipError(f"{path}: folder-trust state unreadable or unexpected; retained for inspection") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def check(home: Path, cwd: Path | None, environment: dict[str, str]) -> None:
    """Accept only the single measured native grant for this seat's trust root.

    Native 1.0.77 stores [folders."/absolute/root"] with trusted=true and a
    decided_at Unix epoch. --trust writes the grant; Quarterdeck never rewrites it.
    """
    path = home / "trusted_folders.toml"
    data = read_trust(path)
    if not data or data == {"folders": {}}:
        return  # no grant yet; the native launch flag creates it
    if cwd is not None:
        root = str(trust_root(cwd, environment))
        folders = data.get("folders")
        if set(data) == {"folders"} and isinstance(folders, dict) and set(folders) == {root}:
            grant = folders[root]
            if (isinstance(grant, dict) and set(grant) == {"trusted", "decided_at"}
                    and grant["trusted"] is True and type(grant["decided_at"]) is int
                    and grant["decided_at"] >= 0):
                return
    raise ShipError(f"{path}: expected exactly the seat's repository-root or non-repository workspace trust grant; retained")
