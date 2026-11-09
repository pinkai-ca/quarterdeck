"""Shared fixtures: ship folders in temporary directories and a private tmux server."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope='session', autouse=True)
def private_socket_root():
    """Even legacy named no-server probes must stay outside the user's socket directory."""
    previous = os.environ.get('TMUX_TMPDIR')
    parent = Path(previous).expanduser().resolve() if previous else ROOT / 'work'
    parent.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix='qd-suite-', dir=parent))
    os.environ['TMUX_TMPDIR'] = str(folder)
    try:
        yield folder
    finally:
        if previous is None:
            os.environ.pop('TMUX_TMPDIR', None)
        else:
            os.environ['TMUX_TMPDIR'] = previous
        native = folder / f'tmux-{os.getuid()}'
        try:
            native.rmdir()  # only an empty directory in this fresh owned test root
        except FileNotFoundError:
            pass
        try:
            folder.rmdir()
        except OSError as error:
            pytest.fail(f'test socket namespace retained: {folder}: {error}')


@pytest.fixture
def drydock(tmp_path: Path) -> Path:
    """A copy of ships/drydock in a temporary folder, not a repository."""
    folder = tmp_path / "ships" / "drydock"
    shutil.copytree(ROOT / "ships" / "drydock", folder)
    return folder


@pytest.fixture
def enterprise_ship(tmp_path: Path) -> Path:
    """The design's example ship, as its own repository with one commit."""
    folder = tmp_path / "ships" / "enterprise"
    folder.mkdir(parents=True)
    shutil.copy(FIXTURES / "ships" / "enterprise.toml", folder / "ship.toml")
    (folder / "orders.md").write_text("# Ship's orders: enterprise\n")
    git(folder, "init", "--quiet")
    git(folder, "add", ".")
    git(folder, "commit", "--quiet", "-m", "declare the ship")
    return folder


@pytest.fixture
def tmux():
    """A private tmux server for one test, never the user's."""
    from quarterdeck.tmux import Tmux

    server = Tmux.private(ROOT / 'work')
    try:
        yield server
    finally:
        if not server.close():
            pytest.fail(f'private tmux cleanup held: {server.held}')


def git(folder: Path, *args: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="test", GIT_AUTHOR_EMAIL="t@example.org",
               GIT_COMMITTER_NAME="test", GIT_COMMITTER_EMAIL="t@example.org")
    done = subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True, env=env, check=True)
    return done.stdout.strip()


@pytest.fixture(autouse=True)
def no_real_antigravity_usage(monkeypatch):
    """Service tests may read synthetic Google usage; never spend the operator's sign-in."""
    from datetime import datetime, timezone
    from quarterdeck.harness import antigravity
    from quarterdeck.harness.antigravity_quota import unknown

    monkeypatch.setattr(antigravity, "fetch_usage", lambda: unknown("synthetic usage unavailable", datetime.now(timezone.utc)))
