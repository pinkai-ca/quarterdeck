"""An inherited synthetic parent must be unreachable by a default tmux query."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest

from tests.testrunner import environment

ROOT = Path(__file__).resolve().parents[2]


def tmux(args, env, *, check=True):
    return subprocess.run(["tmux", *args], env=env, capture_output=True, text=True,
                          timeout=10, check=check)


@pytest.mark.parametrize("runner_prepared", [False, True], ids=["direct", "runner"])
def test_pytest_cannot_query_inherited_parent_server(tmp_path, runner_prepared, monkeypatch):
    # Only these socket anchors live outside the project; test data/output stay in tmp_path.
    anchor = Path(tempfile.mkdtemp(prefix="qdi-", dir="/tmp"))
    identity = anchor.stat()
    (tmp_path / "anchor.json").write_text(json.dumps({
        "anchor": str(anchor), "device": identity.st_dev, "inode": identity.st_ino}))
    socket = anchor / "parent.sock"
    child_anchor = anchor / "child"
    child_anchor.mkdir()
    clean = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    clean["TMUX_TMPDIR"] = str(anchor)
    clean["QD_TMUX_SOCKET"] = str(anchor / "unused-qd.sock")
    started = False
    socket_identity = None
    try:
        tmux(["-S", str(socket), "-f", "/dev/null", "new-session", "-d", "-s", "synthetic-parent",
              "sleep 120"], clean)
        started = True
        socket_identity = socket.lstat()
        parent = tmux(["-S", str(socket), "display-message", "-p", "#{socket_path}"], clean).stdout.strip()
        pid = tmux(["-S", str(socket), "display-message", "-p", "#{pid}"], clean).stdout.strip()
        pane = tmux(["-S", str(socket), "display-message", "-p", "#{pane_id}"], clean).stdout.strip()
        inherited = dict(clean, TMUX=f"{parent},{pid},0", TMUX_PANE=pane)
        # The ONLY unqualified control receives our synthetic connection, never the caller's.
        assert tmux(["display-message", "-p", "#{socket_path}"], inherited).stdout.strip() == parent
        if runner_prepared:
            with monkeypatch.context() as patch:
                for key, value in inherited.items():
                    patch.setenv(key, value)
                child, _ = environment.build(tmp_path / "runner", "synthetic-run")
        else:
            child = dict(clean)
        child.update(TMUX_TMPDIR=str(child_anchor), QD_TMUX_SOCKET=str(child_anchor / "qd.sock"),
                     OPENBLAS_NUM_THREADS="1")
        output = tmp_path / "probe.json"
        probe = tmp_path / "test_probe.py"
        probe.write_text("""import json, os, subprocess
from pathlib import Path

def test_default_query_is_isolated():
    assert 'TMUX' not in os.environ
    assert 'TMUX_PANE' not in os.environ
    result = subprocess.run(['tmux', 'display-message', '-p', '#{socket_path}'],
                            capture_output=True, text=True, timeout=10)
    actual = result.stdout.strip()
    if result.returncode == 0:
        assert actual != os.environ['SYNTHETIC_PARENT_SOCKET']
        assert Path(actual).is_relative_to(Path(os.environ['SYNTHETIC_CHILD_ANCHOR']))
    else:
        assert result.returncode == 1
        assert not actual
    Path(os.environ['SYNTHETIC_PROBE_OUTPUT']).write_text(json.dumps(
        {'exit': result.returncode, 'socket': actual, 'TMUX': os.environ.get('TMUX'),
         'TMUX_PANE': os.environ.get('TMUX_PANE')}))
""")
        child.pop("PYTEST_ADDOPTS", None)
        child.update(SYNTHETIC_PARENT_SOCKET=parent, SYNTHETIC_PROBE_OUTPUT=str(output),
                     SYNTHETIC_CHILD_ANCHOR=str(child_anchor))
        # Clear all live values first, then deliberately supply ONLY our synthetic parent.
        # Recontamination also covers conftest's TESTRUNS_RUN early-return branch.
        command = ["env", "-u", "TMUX", "-u", "TMUX_PANE", "env",
                   f"TMUX={inherited['TMUX']}", f"TMUX_PANE={pane}",
                   "timeout", "--kill-after=5s", "60s", "prlimit", "--as=8589934592", "--",
                   sys.executable, "-m", "pytest", "-p", "tests.conftest", "-c", str(ROOT / "pyproject.toml"),
                   "--basetemp=" + str(tmp_path / "child-tmp"), "-q", str(probe)]
        result = subprocess.run(command, cwd=ROOT, env=child, capture_output=True, text=True, timeout=75)
        (tmp_path / "child-output.txt").write_text(result.stdout + result.stderr)
        assert result.returncode == 0, result.stdout + result.stderr
        observed = json.loads(output.read_text())
        assert observed['TMUX'] is None and observed['TMUX_PANE'] is None
        assert observed['exit'] != 0 or observed['socket'] != parent
        assert tmux(["-S", str(socket), "has-session", "-t", "synthetic-parent"], clean).returncode == 0
        (tmp_path / "proof.json").write_text(json.dumps({
            "anchor": str(anchor), "device": identity.st_dev, "inode": identity.st_ino,
            "parent_socket": parent, "parent_pid": pid, "parent_pane": pane,
            "runner_prepared": runner_prepared, "child": observed, "parent_alive": True}))
    finally:
        if started:
            tmux(["-S", str(socket), "kill-server"], clean)
            # tmux leaves its socket pathname: remove only this exact, proven-closed endpoint.
            assert tmux(["-S", str(socket), "display-message", "-p", "#{pid}"], clean,
                        check=False).returncode != 0
            if socket.exists():
                actual = socket.lstat()
                assert (actual.st_dev, actual.st_ino) == (socket_identity.st_dev, socket_identity.st_ino)
                socket.unlink()
        native = child_anchor / f"tmux-{os.getuid()}"
        if native.exists():
            native.rmdir()
        child_anchor.rmdir()
        actual = anchor.stat()
        assert (actual.st_dev, actual.st_ino) == (identity.st_dev, identity.st_ino)
        anchor.rmdir()
