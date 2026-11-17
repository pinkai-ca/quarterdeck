"""A yes raises a ship only from a person's own terminal, and every raise is seen until the desk reports.

A throwaway project with its office under tmp_path. The yes is given by a child process: from a pipe,
from a pseudo-terminal the test opens with no controlling terminal (as under setsid), from such a
terminal made the child's controlling terminal, and from a pane of a private tmux server named by
QD_TMUX_SOCKET. The ship's crew is never started: the child stands in for that one step.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import pty
import subprocess
import sys
import termios
import time

import pytest

from quarterdeck import post, raise_notice, setup, ship

SRC = Path(__file__).resolve().parents[2] / "src"
CHILD = '''
import json, sys
from quarterdeck import cli, raise_ship, setup
from quarterdeck.ship import ShipError
office, out = sys.argv[1], sys.argv[2]
started = []
setup.Operations.up = lambda self, current: started.append(str(current.folder)) or ["synthetic crew up"]
args = cli.build_parser().parse_args(["--ship", office, "raise", "default", "mission", "--accept", "yes"])
try:
    result = dict(ok=True, lines=raise_ship.run(args, interactive=False), started=started)
except ShipError as error:
    result = dict(ok=False, error=str(error), started=started)
open(out, "w").write(json.dumps(result))
'''


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(setup.Operations, "project_folder", lambda _: tmp_path)
    for key in ("QD_SHIP", "QD_SEAT", "GIT_DIR", "GIT_WORK_TREE"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {"GIT_AUTHOR_NAME": "Raise Example", "GIT_AUTHOR_EMAIL": "raise@example.invalid",
                       "GIT_COMMITTER_NAME": "Raise Example", "GIT_COMMITTER_EMAIL": "raise@example.invalid",
                       "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}.items():
        monkeypatch.setenv(key, value)
    setup.init_office(argparse.Namespace(ship=None), start=False)
    with (tmp_path / "office" / "ship.toml").open("a") as stream:  # the pools the default template names
        stream.write('\n[pools.codex]\nprovider = "openai"\nconfig_dir = "~/.codex"\n')
    return tmp_path


def environment(**extra) -> dict:
    env = {key: value for key, value in os.environ.items() if not key.startswith(("QD_", "TMUX"))}
    env.update(PYTHONPATH=str(SRC), PYTHONNOUSERSITE="1", **extra)
    return env


def answers(project: Path) -> list[dict]:
    return [json.loads(path.read_text())
            for path in sorted((project / "office/runtime/raise/mission").glob("*.answer-*.json"))]


def child(project: Path, tmp_path: Path, *, stdin, controlling: bool, **popen) -> dict:
    out = tmp_path / "child.json"
    script = tmp_path / "child.py"
    script.write_text(CHILD)
    command = [sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(SRC)!r}); "
               f"exec(open({str(script)!r}).read())", str(project / "office"), str(out)]
    preexec = (lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0)) if controlling else None
    done = subprocess.run(command, stdin=stdin, capture_output=True, text=True, timeout=120,
                          env=environment(), start_new_session=True, preexec_fn=preexec, **popen)
    assert out.is_file(), done.stderr
    return json.loads(out.read_text())


def refused(result: dict, project: Path, reason: str) -> None:
    assert result["ok"] is False and reason in result["error"], result
    assert result["started"] == [] and not (project / "ships" / "mission").exists()
    assert answers(project) == []


def test_a_yes_from_a_pipe_is_refused(project, tmp_path):
    result = child(project, tmp_path, stdin=subprocess.PIPE, controlling=False)
    refused(result, project, "stdin is not a terminal")


def test_a_yes_from_a_terminal_with_no_controlling_terminal_is_refused(project, tmp_path):
    master, slave = pty.openpty()
    try:
        result = child(project, tmp_path, stdin=slave, controlling=False)
    finally:
        os.close(slave)
        os.close(master)
    refused(result, project, "no controlling terminal (it is detached)")


def test_a_yes_from_a_pane_of_a_ship_server_is_refused(project, tmp_path, tmux):
    out, script = tmp_path / "child.json", tmp_path / "child.py"
    script.write_text(CHILD)
    tmux.new_session("raise-panes")
    command = [sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(SRC)!r}); "
               f"exec(open({str(script)!r}).read())", str(project / "office"), str(out)]
    tmux.new_window("raise-panes", "approve", tmp_path, command,
                    env={"PYTHONNOUSERSITE": "1", "QD_TMUX_SOCKET": tmux.socket,
                         **{key: os.environ[key] for key in os.environ if key.startswith("GIT_")}})
    deadline = time.monotonic() + 60
    while not out.is_file():
        assert time.monotonic() < deadline, "the yes in the pane never finished"
        time.sleep(0.05)
    time.sleep(0.1)
    refused(json.loads(out.read_text()), project, "is a pane of ship")


def status(office, tmux) -> str:
    from quarterdeck import watch
    facts, _ = watch.collect(office, tmux)
    return facts.line()


def test_a_yes_from_a_persons_terminal_is_recorded_told_to_the_desk_and_shown_until_its_report(project, tmp_path, tmux):
    master, slave = pty.openpty()
    terminal = os.ttyname(slave)
    try:
        result = child(project, tmp_path, stdin=slave, controlling=True)
    finally:
        os.close(slave)
        os.close(master)
    assert result["ok"] is True, result
    assert result["started"] == [str(project / "ships" / "mission")]
    saved, = answers(project)
    assert saved["answer"] == "yes" and saved["by"] == "person" and saved["terminal"] == terminal
    process = saved["process"]
    assert saved["ancestry"][0][:2] == [process["pid"], process["start"]]
    assert all(len(entry) == 3 and isinstance(entry[0], int) for entry in saved["ancestry"])
    office = ship.load(project / "office")
    store = post.Store(office.runtime)
    told = [m for m in store.all() if m.subject == raise_notice.SUBJECT]
    assert len(told) == 1 and told[0].kind == "order" and told[0].sender == post.SHIP
    assert office.seat(told[0].to).role == "desk"
    assert terminal in told[0].body and "mission" in told[0].body
    assert any(told[0].id in line for line in result["lines"])
    shown = f"ship mission raised from {terminal}: the desk's report on {told[0].short} clears this"
    assert raise_notice.unseen(office.runtime, store) == [shown]
    assert shown in status(office, tmux)
    store.new("report", told[0].to, post.SHIP, "seen", re=told[0].id)
    assert raise_notice.unseen(office.runtime, post.Store(office.runtime)) == []
    assert "raised from" not in status(office, tmux)
