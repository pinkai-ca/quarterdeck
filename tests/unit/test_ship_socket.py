"""The ship's own tmux server: its socket, the refusals around it, qd attach, and a seat's launch environment."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import cli, seat as seatmod, tmux as module
from quarterdeck.tmux import Tmux, TmuxError, ship_socket


@pytest.fixture(autouse=True)
def roomy_sockets(monkeypatch):
    """Test folders are deep; only the length test holds paths to the native limit."""
    monkeypatch.setattr(module, "SOCKET_LIMIT", 4096)


def a_ship(folder: Path, sessions=None):
    sessions = sessions or {"office": SimpleNamespace(title="Office", human=True),
                            "crew": SimpleNamespace(title="Crew", human=False)}
    return SimpleNamespace(folder=folder, runtime=folder / "runtime", sessions=sessions)


def recorded(monkeypatch):
    seen = []

    def run(command, **kwargs):
        seen.append(command)
        return SimpleNamespace(returncode=1, stdout="", stderr="no server running")
    monkeypatch.setattr(module.subprocess, "run", run)
    return seen


# -- choosing the server ------------------------------------------------------------------


def test_a_ship_runs_on_a_fixed_socket_under_its_runtime(tmp_path, monkeypatch):
    monkeypatch.delenv("QD_TMUX_SOCKET", raising=False)
    ship = a_ship(tmp_path)
    server = Tmux.for_ship(ship)
    assert server.socket == str(tmp_path / "runtime" / "tmux" / "server") == str(ship_socket(ship))
    assert server.ship and not server.legacy and not server._private
    assert not (tmp_path / "runtime").exists()  # choosing a server makes nothing


def test_the_ships_own_socket_named_in_the_environment_is_the_ship_server(tmp_path, monkeypatch):
    ship = a_ship(tmp_path)
    monkeypatch.setenv("QD_TMUX_SOCKET", str(ship_socket(ship)))
    assert Tmux.for_ship(ship).ship


def test_a_private_test_namespace_named_in_the_environment_keeps_its_meaning(tmp_path, monkeypatch):
    private = Tmux.private(Path(os.environ["TMUX_TMPDIR"]))  # a creator's namespace; no server is started
    try:
        monkeypatch.setenv("QD_TMUX_SOCKET", private.socket)
        server = Tmux.for_ship(a_ship(tmp_path))
        assert server.socket == private.socket and not server.ship and server._private
    finally:
        assert private.close(), private.held


@pytest.mark.parametrize("named", ["/elsewhere/ships/other/runtime/tmux/server", "qd-some-named-server"])
def test_any_other_socket_named_in_the_environment_is_refused_by_name(tmp_path, monkeypatch, named):
    monkeypatch.setenv("QD_TMUX_SOCKET", named)
    seen = recorded(monkeypatch)
    with pytest.raises(TmuxError, match="will not run this ship there") as refused:
        Tmux.for_ship(a_ship(tmp_path))
    assert named in str(refused.value) and seen == []


def test_a_folder_that_only_looks_like_a_private_namespace_is_refused(tmp_path, monkeypatch):
    from quarterdeck.tmux_socket import HEADER, LOCK
    folder = tmp_path / "ns"
    folder.mkdir(mode=0o700)
    (folder / LOCK).write_bytes(HEADER)
    (folder / LOCK).chmod(0o644)  # not the creator's mode-600 lock
    monkeypatch.setenv("QD_TMUX_SOCKET", str(folder / "s"))
    with pytest.raises(TmuxError, match="will not run this ship there"):
        Tmux.for_ship(a_ship(tmp_path))


def test_TMUX_and_TMUX_TMPDIR_never_choose_the_ships_server(tmp_path, monkeypatch):
    monkeypatch.delenv("QD_TMUX_SOCKET", raising=False)
    monkeypatch.setenv("TMUX", "/elsewhere/default,1,0")
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path / "other"))
    seen = recorded(monkeypatch)
    server = Tmux.for_ship(a_ship(tmp_path))
    server.running()
    assert seen == [["tmux", "-u", "-S", str(tmp_path / "runtime" / "tmux" / "server"), "list-sessions"]]


def test_every_ship_command_names_the_socket_and_keeps_the_persons_configuration(tmp_path, monkeypatch):
    seen = recorded(monkeypatch)
    server = Tmux(str(tmp_path / "server"), ship=True)
    server.running()
    server.has_session("Crew")
    assert all(command[:4] == ["tmux", "-u", "-S", str(tmp_path / "server")] for command in seen)
    assert not any("-f" in command or "-N" in command for command in seen)


def test_qd_never_stops_a_ship_server_nor_the_default_one(tmp_path, monkeypatch):
    seen = recorded(monkeypatch)
    for server in (Tmux(str(tmp_path / "server"), ship=True), Tmux(str(tmp_path / "default"), legacy=True)):
        with pytest.raises(TmuxError, match="never stops"):
            server.kill_server()
    assert seen == []


def test_the_default_server_is_read_with_its_path_and_never_started(tmp_path, monkeypatch):
    seen = recorded(monkeypatch)
    Tmux(str(tmp_path / "default"), legacy=True).running()
    assert seen == [["tmux", "-u", "-S", str(tmp_path / "default"), "-N", "list-sessions"]]


def test_the_default_server_is_looked_for_by_TMUX_and_the_default_socket_only(monkeypatch):
    import shutil
    import socket as sockets
    import tempfile
    monkeypatch.undo()  # the suite's stand-in for this search is the subject here
    from quarterdeck.tmux import _default_sockets
    short = Path(tempfile.mkdtemp(prefix="d", dir=os.environ["TMUX_TMPDIR"]))  # the session's short private root
    try:
        folder = short / f"tmux-{os.getuid()}"
        folder.mkdir(mode=0o700)
        named, plain = short / "n", folder / "default"
        for path in (named, plain):
            with sockets.socket(sockets.AF_UNIX) as made:
                made.bind(str(path))  # a bound socket file and no server: nothing listens on it
        (short / "f").write_text("")
        monkeypatch.setenv("TMUX", f"{named},1,0")
        monkeypatch.setenv("TMUX_TMPDIR", str(short))
        assert _default_sockets() == [named, plain]  # never /tmp's default while TMUX_TMPDIR names another
        monkeypatch.setenv("TMUX", f"{short / 'f'},1,0")  # not a socket: never a candidate
        assert short / "f" not in _default_sockets()
    finally:
        shutil.rmtree(short)


# -- the socket's place ---------------------------------------------------------------------


def test_a_socket_path_too_long_is_refused_before_launch_and_never_started(tmp_path, monkeypatch):
    monkeypatch.delenv("QD_TMUX_SOCKET", raising=False)
    monkeypatch.setattr(module, "SOCKET_LIMIT", 108)
    seen = recorded(monkeypatch)
    server = Tmux.for_ship(a_ship(tmp_path / ("d" * 108)))  # reading is no launch: no server can be there
    with pytest.raises(TmuxError, match="shorter folder"):
        server.launchable()
    with pytest.raises(TmuxError, match="shorter folder"):
        server.new_session("Crew")
    assert not any("new-session" in command for command in seen)


def test_socket_length_counts_bytes_and_the_terminating_nul(monkeypatch):
    monkeypatch.setattr(module, "SOCKET_LIMIT", 108)
    exact = "/" + "a" * 106  # 107 bytes and its NUL: exactly the limit
    module.check_socket_length(exact, "probe")
    with pytest.raises(TmuxError, match="109 bytes"):
        module.check_socket_length(exact + "a", "probe")  # one byte over
    wide = "/" + "é" * 60  # 61 characters, 121 bytes in UTF-8
    with pytest.raises(TmuxError, match="122 bytes"):
        module.check_socket_length(wide, "probe")


def test_the_socket_folder_is_made_private_when_the_server_starts(tmp_path, monkeypatch):
    seen = recorded(monkeypatch)
    path = tmp_path / "runtime" / "tmux" / "server"
    with pytest.raises(TmuxError):
        Tmux(str(path), ship=True).new_session("Crew")
    assert (path.parent.stat().st_mode & 0o777) == 0o700
    # The server is first asked whether it runs; the one that starts it is the new-session.
    assert seen[0] == ["tmux", "-u", "-S", str(path), "-N", "list-sessions"]
    [started] = [command for command in seen if "new-session" in command]
    assert started[started.index("tmux"):][:4] == ["tmux", "-u", "-S", str(path)]


def test_a_symlinked_socket_folder_is_refused_and_left_alone(tmp_path, monkeypatch):
    monkeypatch.delenv("QD_TMUX_SOCKET", raising=False)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "tmux").symlink_to(elsewhere)
    with pytest.raises(TmuxError, match="not a plain folder"):
        Tmux.for_ship(a_ship(tmp_path))
    assert (tmp_path / "runtime" / "tmux").is_symlink()


def test_a_socket_folder_open_to_others_is_refused_and_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv("QD_TMUX_SOCKET", raising=False)
    folder = tmp_path / "runtime" / "tmux"
    folder.mkdir(parents=True)
    folder.chmod(0o755)
    with pytest.raises(TmuxError, match="mode 755"):
        Tmux.for_ship(a_ship(tmp_path))
    assert (folder.stat().st_mode & 0o777) == 0o755


def test_something_else_at_the_socket_path_is_refused_and_kept(tmp_path, monkeypatch):
    monkeypatch.delenv("QD_TMUX_SOCKET", raising=False)
    folder = tmp_path / "runtime" / "tmux"
    folder.mkdir(parents=True, mode=0o700)
    (folder / "server").write_text("not a socket")
    with pytest.raises(TmuxError, match="not this user's socket"):
        Tmux.for_ship(a_ship(tmp_path))
    assert (folder / "server").read_text() == "not a socket"


def test_a_symlink_at_the_socket_path_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("QD_TMUX_SOCKET", raising=False)
    folder = tmp_path / "runtime" / "tmux"
    folder.mkdir(parents=True, mode=0o700)
    (folder / "server").symlink_to(tmp_path / "anything")
    with pytest.raises(TmuxError, match="not this user's socket"):
        Tmux.for_ship(a_ship(tmp_path))


# -- attach -----------------------------------------------------------------------------------


def test_attach_words_name_the_ships_socket():
    assert Tmux("/r/tmux/server", ship=True).attach_argv("Office") == [
        "tmux", "-S", "/r/tmux/server", "attach-session", "-t", "=Office:"]
    assert Tmux("/tmp/tmux-1/default", legacy=True).attach_argv("Office") == [
        "tmux", "-S", "/tmp/tmux-1/default", "-N", "attach-session", "-t", "=Office:"]


class Attached(Exception):
    pass


@pytest.fixture
def attach(drydock, monkeypatch):
    """qd attach with the exec caught: the words and the environment it would hand to tmux."""
    from quarterdeck import install
    monkeypatch.setattr(install, "handoff", lambda folder, argv: None)
    monkeypatch.setenv("QD_SHIP", str(drydock))
    monkeypatch.delenv("QD_TMUX_SOCKET", raising=False)
    monkeypatch.setattr(Tmux, "has_session", lambda self, name: True)
    caught = {}

    def execvpe(file, argv, env):
        caught.update(file=file, argv=argv, env=env)
        raise Attached
    monkeypatch.setattr(os, "execvpe", execvpe)

    def run(*argv):
        with pytest.raises(Attached):
            cli.main(["attach", *argv])
        return caught
    return run


def test_attach_opens_the_persons_own_session_by_default(attach, drydock):
    socket = str(drydock.resolve() / "runtime" / "tmux" / "server")
    assert attach()["argv"] == ["tmux", "-S", socket, "attach-session", "-t", "=Office:"]


def test_attach_takes_a_session_by_name_or_title(attach):
    assert attach("bridge")["argv"][-1] == "=Bridge:"
    assert attach("Crew")["argv"][-1] == "=Crew:"


def test_attach_from_inside_another_tmux_opens_a_client_of_its_own(attach, monkeypatch):
    monkeypatch.setenv("TMUX", "/elsewhere/default,1,0")
    monkeypatch.setenv("TMUX_PANE", "%3")
    found = attach()
    assert "attach-session" in found["argv"] and "TMUX" not in found["env"] and "TMUX_PANE" not in found["env"]


def test_attach_inside_the_ships_own_server_switches_the_client(attach, drydock, monkeypatch):
    socket = str(drydock.resolve() / "runtime" / "tmux" / "server")
    monkeypatch.setenv("TMUX", f"{socket},1,0")
    assert attach("crew")["argv"] == ["tmux", "-S", socket, "switch-client", "-t", "=Crew:"]


def test_attach_to_an_unknown_session_names_the_ships_sessions(drydock, monkeypatch, capsys):
    from quarterdeck import install
    monkeypatch.setattr(install, "handoff", lambda folder, argv: None)
    monkeypatch.setenv("QD_SHIP", str(drydock))
    monkeypatch.delenv("QD_TMUX_SOCKET", raising=False)
    assert cli.main(["attach", "nowhere"]) == 2
    assert "office (Office)" in capsys.readouterr().err


# -- a seat's launch environment ---------------------------------------------------------------


def test_launch_words_cut_the_seat_off_from_the_server_it_runs_in(tmp_path):
    words = seatmod.launch_words("/bin", ["prog", "a b"], socket="/r/tmux/server", tmux_tmpdir=tmp_path / "t",
                                 markers=["CLAUDECODE"])
    assert words == ["env", "-u", "TMUX", "-u", "TMUX_PANE", "-u", "CLAUDECODE", "-u", "SUDO_ASKPASS",
                     "QD_TMUX_SOCKET=/r/tmux/server", f"TMUX_TMPDIR={tmp_path / 't'}", "PATH=/bin", "prog", "a b"]
    unnamed = seatmod.launch_words("/bin", ["prog"], socket=None, tmux_tmpdir=tmp_path)
    assert unnamed[5:9] == ["-u", "SUDO_ASKPASS", "-u", "QD_TMUX_SOCKET"]


def test_the_markers_removed_are_the_adapters_named_session_marks_only():
    from quarterdeck import harness
    assert harness.inherited_markers() == (
        "CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_SESSION_ATTENDED",
        "CLAUDE_PID", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN")


def test_the_launch_script_leaves_the_program_no_tmux_server_and_no_inherited_session_marks(tmp_path):
    from quarterdeck import harness
    markers = [name for name in harness.inherited_markers() if name not in {"CLAUDE_CODE_SESSION_ID": "adapter's"}]
    words = seatmod.launch_words(os.environ["PATH"], ["env"], socket="/r/tmux/server", tmux_tmpdir=tmp_path / "t",
                                 markers=markers)
    script = tmp_path / "launch.sh"
    script.write_text(seatmod.launch_script("synthetic", words))
    inherited = {"PATH": os.environ["PATH"], "TMUX": "/r/tmux/server,1,0", "TMUX_PANE": "%1",
                 "TMUX_TMPDIR": "/shared", "QD_TMUX_SOCKET": "/elsewhere",
                 "CLAUDECODE": "1", "CLAUDE_PID": "7", "CLAUDE_CODE_CHILD_SESSION": "1",
                 "CLAUDE_CODE_SESSION_ATTENDED": "1", "CLAUDE_CODE_MESSAGING_SOCKET": "/m", "CLAUDE_CODE_MESSAGING_TOKEN": "t",
                 "CLAUDE_CODE_SESSION_ID": "kept-by-the-adapter", "CLAUDE_CODE_ENTRYPOINT": "cli",
                 "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "CLAUDE_CONFIG_DIR": "/home"}
    done = subprocess.run(["sh", str(script)], env=inherited, capture_output=True, text=True, timeout=10, check=True)
    seen = dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)
    for gone in ("TMUX", "TMUX_PANE", "CLAUDECODE", "CLAUDE_PID", "CLAUDE_CODE_CHILD_SESSION",
                 "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN"):
        assert gone not in seen
    assert seen["TMUX_TMPDIR"] == str(tmp_path / "t") and seen["QD_TMUX_SOCKET"] == "/r/tmux/server"
    for kept in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
                 "CLAUDE_CONFIG_DIR"):
        assert kept in seen  # an adapter's own setting, and every preference, stay


@pytest.mark.parametrize("program,removed", [
    ("grok", {"VTE_VERSION"}), ("muse", {"COLORTERM"}), ("claude", set()), ("codex", set()), ("opencode", set()),
])
def test_launch_uses_the_adapters_measured_terminal_environment(tmp_path, program, removed):
    """Grok's hint row depends on VTE_VERSION. Muse's colour detector prefers COLORTERM to TERM."""
    from types import SimpleNamespace
    from quarterdeck import harness
    launching = SimpleNamespace(harness=harness.get(program), _launch_tmpdir=tmp_path / "t",
                                _socket_for_seat=lambda: "/r/tmux/server", ship=SimpleNamespace(runtime=tmp_path))
    words = seatmod.Seat._launch_words(launching, ["env"], {}, os.environ["PATH"])
    script = tmp_path / "launch.sh"
    script.write_text(seatmod.launch_script("synthetic", words))
    terminal = {"TERM": "tmux-256color", "VTE_VERSION": "8400", "COLORTERM": "truecolor"}
    inherited = {"PATH": os.environ["PATH"], **terminal}
    done = subprocess.run(["sh", str(script)], env=inherited, capture_output=True, text=True, timeout=10, check=True)
    seen = dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)
    assert {name: seen[name] for name in terminal if name in seen} == {
        name: value for name, value in terminal.items() if name not in removed
    }


def test_a_ship_still_on_the_default_server_keeps_its_alert_when_the_status_line_overflows():
    from quarterdeck import watch
    cutover = "tmux: still on the default server /tmp/tmux-1/default; qd down, then qd up, moves it"
    assert watch.Facts("ok", 0, 0, 0, cutover=cutover).line() == f"service ok · 0 waiting · {cutover} · qd status"
    crowded = watch.Facts("ok", 0, 0, 0, cutover=cutover, pending=["x" * 400]).line()
    assert cutover in crowded and crowded.endswith(" · qd status")
