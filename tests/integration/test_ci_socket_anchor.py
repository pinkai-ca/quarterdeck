"""Claude-owned proof of the CI socket hazard at a hosted-length checkout, inside this run's own scratch.

A synthetic Git checkout is made under tmp_path, at least as long as GitHub's default workspace. It
holds byte copies of this checkout's tests/__init__.py, tests/conftest.py, tests/testrunner and
pyproject.toml, plus one synthetic test, so the real session guard runs while collection is limited by
what the checkout contains: pytest's own argv is never changed. Its .venv/bin/activate is a stated shim
that puts a `python` running this run's interpreter first on PATH. The workflow's test step is taken verbatim from
.github/workflows/ci.yml and run as GitHub runs a step that sets no shell: `bash -e <script>`, with BASH_ENV
unset as it is on the runner. The stricter `--noprofile --norc -eo pipefail` is what GitHub uses only when a
step names `shell: bash`; this one does not.

With TMUX_TMPDIR unset, the checkout's sockets live in its project's short root under /tmp, whose length does not
depend on the checkout's: a hosted-length checkout runs there and leaves no container. A RUNNER_TEMP too long for
the guard is still refused. A short RUNNER_TEMP would need a folder outside the run's own scratch, which is not
allowed; that success path is left to the first private GitHub run.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

from tests import conftest
from tests.testrunner import socket_roots

HOSTED = "/home/runner/work/quarterdeck/quarterdeck"  # GitHub's default workspace for this repository
STEP = "- name: Unit, contract and integration tests"
SYNTHETIC_TEST = "def test_synthetic_session_reaches_a_test():\n    pass\n"
DROPPED = ("TMUX_TMPDIR", "TMUX", "TMUX_PANE", "QD_SHIP", "QD_SEAT", "QD_TMUX_SOCKET", "RUNNER_TEMP",
           "BASH_ENV")  # bash -e reads BASH_ENV when it is set; the runner sets none
# A hosted runner has none of the test tool's variables: no run, no session records, no jail.
DROPPED_PREFIXES = ("TESTRUN",)


def deepest(anchor: Path) -> int:
    return len(os.fsencode(anchor / (conftest.SESSION_PREFIX + "12345678") / "qs-12345678/qd-pt-12345678/s-replacement"))


def checkout(tmp_path: Path) -> Path:
    folder = tmp_path / "synthetic-hosted-length-checkout"
    folder.mkdir()
    conftest.git(folder, "init", "--quiet")
    (folder / "tests").mkdir()
    for name in ("__init__.py", "conftest.py"):
        shutil.copyfile(conftest.ROOT / "tests" / name, folder / "tests" / name)
    shutil.copytree(conftest.ROOT / "tests/testrunner", folder / "tests/testrunner",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copyfile(conftest.ROOT / "pyproject.toml", folder / "pyproject.toml")
    (folder / "tests/test_synthetic_probe.py").write_text(SYNTHETIC_TEST)
    shim = folder / ".venv/bin"
    shim.mkdir(parents=True)
    (shim / "python").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    (shim / "python").chmod(0o700)
    (shim / "activate").write_text(f'PATH="{shim}:$PATH"\nexport PATH\n')
    assert (folder / "tests/conftest.py").read_bytes() == (conftest.ROOT / "tests/conftest.py").read_bytes()
    assert len(os.fsencode(folder)) >= len(HOSTED)
    assert deepest(folder / ".testruns/s") + conftest.SOCKET_MARGIN > conftest.SOCKET_LIMIT, \
        "the synthetic checkout must be long enough that an anchor inside it could not fit"
    return folder


def environment(**extra: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if key not in DROPPED and not key.startswith(DROPPED_PREFIXES)}
    env.update(extra)
    return env


def step_body() -> str:
    """The test step's run block, verbatim, from the candidate workflow."""
    lines = (conftest.ROOT / ".github/workflows/ci.yml").read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == STEP)
    run = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "run: |")
    indent = len(lines[run]) - len(lines[run].lstrip()) + 2
    body = []
    for line in lines[run + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) < indent:
            break
        body.append(line[indent:])
    assert not any(line.strip().startswith("shell:") for line in lines[start:run]), "GitHub's default shell is assumed"
    text = "\n".join(body).rstrip() + "\n"
    assert "python -m pytest" in text and "RUNNER_TEMP" in text and "TMUX_TMPDIR" in text
    return text


def short_root(folder: Path) -> Path:
    """The checkout's own short socket root under /tmp, named for its project."""
    return socket_roots.path(folder.resolve())


def remove_short_root(folder: Path) -> None:
    """Remove the short root this test's checkout caused, and only when it names that checkout."""
    root = short_root(folder)
    if root.is_symlink() or not root.is_dir():
        return
    try:
        record = json.loads((root / socket_roots.PROJECT).read_text())
    except (OSError, ValueError):
        return
    if record.get("project") == str(folder.resolve()):
        shutil.rmtree(root)


def test_a_hosted_length_checkout_runs_on_its_short_root_without_a_supplied_root(tmp_path):
    """With TMUX_TMPDIR unset, the session's sockets live in the project's short root, whatever the checkout's
    length; the session passes, and leaves the root private and its anchor empty."""
    folder = checkout(tmp_path)
    root = short_root(folder)
    assert not root.exists() and not root.is_symlink(), f"{root} is already there; this checkout is new"
    try:
        done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-rs", "-p", "no:xdist"], cwd=folder,
                              env=environment(), capture_output=True, text=True, timeout=120)
        output = done.stdout + done.stderr
        assert done.returncode == 0 and "1 passed" in output, output[-2000:]
        assert len(os.fsencode(root)) == len(os.fsencode(short_root(Path("/p")))), "the root's length is fixed"
        assert deepest(root / "s") + conftest.SOCKET_MARGIN <= conftest.SOCKET_LIMIT
        info = root.lstat()
        assert stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700 and info.st_uid == os.getuid()
        assert json.loads((root / socket_roots.PROJECT).read_text())["project"] == str(folder.resolve())
        anchor = root / "s"
        assert anchor.is_dir() and not anchor.is_symlink() and not any(anchor.iterdir()), \
            "the session leaves no socket container"
        assert not (folder / ".testruns/s").exists(), "no socket anchor in the long checkout itself"
    finally:
        remove_short_root(folder)


def test_the_workflow_step_on_a_too_long_runner_temp_fails_and_leaves_it_empty(tmp_path):
    """The candidate step itself, on a RUNNER_TEMP too long for the unchanged guard: nonzero, nothing retained.
    The refusal must name the step's own mktemp anchor under RUNNER_TEMP, the root the step supplies; the
    checkout's own short root, used only when none is supplied, is never the cause."""
    folder = checkout(tmp_path)
    runner_temp = tmp_path / ("synthetic-runner-temp-" + "w" * 40)
    runner_temp.mkdir()
    assert deepest(runner_temp / "qs.XXXXXXXX") + conftest.SOCKET_MARGIN > conftest.SOCKET_LIMIT
    script = tmp_path / "synthetic-step.sh"
    script.write_text(step_body())
    done = subprocess.run(["bash", "-e", str(script)], cwd=folder,
                          env=environment(RUNNER_TEMP=str(runner_temp)), capture_output=True, text=True, timeout=180)
    output = done.stdout + done.stderr
    assert done.returncode != 0 and "too long" in output, output[-2000:]
    named = set(re.findall(r"test socket anchor (/\S*) gives sockets", output))  # not the traceback's {parent}
    owned = re.compile(re.escape(str(runner_temp.resolve())) + r"/qs\.[A-Za-z0-9]{8}")
    assert len(named) == 1 and owned.fullmatch(next(iter(named))), \
        f"the refusal must name the step's own mktemp anchor under RUNNER_TEMP, not {sorted(named)}"
    assert "/.testruns/s gives sockets" not in output, "the checkout's own anchor must not be the refusal's cause"
    assert "Private CI socket anchor retained" not in output
    assert list(runner_temp.iterdir()) == [], "the step removes its own empty anchor and nothing else is left"
