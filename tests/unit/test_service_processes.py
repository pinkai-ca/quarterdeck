"""Private service scope readings after a lock owner has exited."""

import os
import select
import signal

import pytest

from quarterdeck import service_processes as scopes, servicectl, shutdown
from tests.unit.test_stop_owner_lifetime import stand_in, stopping_clock


def proc(root, pid, *, session=301, group=301, start="700", state="S", unit=None, parent=1, name="synthetic"):
    folder = root / str(pid)
    folder.mkdir(parents=True, exist_ok=True)
    fields = [state, str(parent), str(group), str(session), *(["0"] * 15), start]
    (folder / "stat").write_text(f"{pid} (synthetic service) " + " ".join(fields) + "\n")
    (folder / "comm").write_text(name + "\n")
    if unit is not None:
        (folder / "cgroup").write_text(f"0::/user.slice/{unit}\n")


def test_cgroup_reread_finds_a_member_born_after_the_owner_ended(tmp_path, monkeypatch):
    root, groups = tmp_path / "proc", tmp_path / "groups"
    unit = "qd-service-synthetic-00000001.scope"
    folder = groups / "user.slice" / unit
    folder.mkdir(parents=True)
    (folder / "cgroup.procs").write_text("301\n")
    (folder / "cgroup.events").write_text("populated 1\n")
    proc(root, 301, unit=unit)
    monkeypatch.setattr(scopes, "PROC", root)
    monkeypatch.setattr(scopes, "CGROUPS", groups)
    bound = scopes.capture(301, "700")
    assert bound.read() == {301: "700"}
    proc(root, 301, state="Z", unit=unit)
    # A descendant's own session no longer matches: the cgroup is stronger.
    proc(root, 302, session=302, group=302, start="701", unit=unit + "/child")
    subgroup = folder / "child"
    subgroup.mkdir()
    (subgroup / "cgroup.procs").write_text("302\n")
    (folder / "cgroup.procs").write_text("")
    assert bound.read() == {302: "701"}
    assert bound.proof == f"processes re-read: cgroup {unit}"
    (subgroup / "cgroup.procs").write_text("")
    (folder / "cgroup.events").write_text("populated 0\n")
    assert bound.read() == {}


def test_a_populated_cgroup_without_readable_identities_cannot_prove_an_exit(tmp_path, monkeypatch):
    root, groups = tmp_path / "proc", tmp_path / "groups"
    unit = "qd-service-synthetic-00000002.scope"
    folder = groups / "user.slice" / unit
    folder.mkdir(parents=True)
    (folder / "cgroup.procs").write_text("")
    (folder / "cgroup.events").write_text("populated 1\n")
    proc(root, 301, unit=unit)
    monkeypatch.setattr(scopes, "PROC", root)
    monkeypatch.setattr(scopes, "CGROUPS", groups)
    with pytest.raises(scopes.Uncertain, match="populated"):
        scopes.capture(301, "700").read()


def test_an_unreadable_cgroup_uses_the_owned_session_and_labels_the_stop(tmp_path, monkeypatch):
    def unreadable(_):
        raise PermissionError("synthetic unavailable cgroup")
    monkeypatch.setattr(scopes, "cgroup", unreadable)
    with stand_in(tmp_path, response="exit") as (runtime, child):
        stopping_clock(monkeypatch, child, response="exit")
        proof = servicectl.stop(runtime)
        assert proof.startswith("service stopped")
        assert f"processes re-read: session {child.pid} (service cgroup unavailable: PermissionError)" in proof
        assert child.poll() == 0


def test_a_cgroup_that_becomes_unreadable_falls_back_to_the_captured_session(tmp_path, monkeypatch):
    root, groups = tmp_path / "proc", tmp_path / "groups"
    unit = "qd-service-synthetic-00000003.scope"
    folder = groups / "user.slice" / unit
    folder.mkdir(parents=True)
    proc(root, 301, unit=unit)
    proc(root, 302, start="701")
    proc(root, 303, session=303, group=303)
    monkeypatch.setattr(scopes, "PROC", root)
    monkeypatch.setattr(scopes, "CGROUPS", groups)
    bound = scopes.capture(301, "700")
    assert bound.read() == {301: "700", 302: "701"}
    assert bound.proof == "processes re-read: session 301 (service cgroup unavailable: FileNotFoundError)"


def test_the_owned_process_group_is_available_when_the_session_is_not_owned(tmp_path, monkeypatch):
    root = tmp_path / "proc"
    proc(root, 301, session=300)
    proc(root, 302, session=300, start="701")
    monkeypatch.setattr(scopes, "PROC", root)
    bound = scopes.capture(301, "700")
    assert bound.read() == {301: "700", 302: "701"}
    assert bound.proof == "processes re-read: process group 301 (service cgroup unavailable: FileNotFoundError)"


def test_a_tmux_server_in_the_cgroup_is_left_with_everything_under_it(tmp_path, monkeypatch):
    root, groups = tmp_path / "proc", tmp_path / "groups"
    unit = "qd-service-synthetic-00000004.scope"
    folder = groups / "user.slice" / unit
    folder.mkdir(parents=True)
    (folder / "cgroup.procs").write_text("301\n302\n303\n304\n305\n")
    (folder / "cgroup.events").write_text("populated 1\n")
    proc(root, 301, unit=unit)
    proc(root, 302, start="702", unit=unit, name="tmux: server")
    proc(root, 303, start="703", unit=unit, parent=302)  # a pane
    proc(root, 304, start="704", unit=unit, parent=303)  # the pane's program
    proc(root, 305, start="705", unit=unit)  # a plain detached member
    monkeypatch.setattr(scopes, "PROC", root)
    monkeypatch.setattr(scopes, "CGROUPS", groups)
    bound = scopes.capture(301, "700")
    assert bound.read() == {301: "700", 305: "705"}
    assert bound.servers == {302: "702"}
    assert bound.spared() == "a tmux server is in the service's scope (pid 302); left running with its panes"


def _end(pid, start):
    """Teardown of a stand-in this test started: by its exit event, the timeout only a guard."""
    if shutdown.process_gone(dict(pid=pid, start=start)):
        return
    try:
        descriptor = os.pidfd_open(pid)
    except ProcessLookupError:
        return
    try:
        if not shutdown.process_gone(dict(pid=pid, start=start)):
            os.kill(pid, signal.SIGKILL)
            assert select.select([descriptor], [], [], 20)[0]
    finally:
        os.close(descriptor)


def test_the_stop_leaves_a_tmux_server_in_its_scope_and_ends_a_plain_detached_member(tmp_path, monkeypatch):
    with stand_in(tmp_path, response="exit", detach=("plain", "server")) as (runtime, child):
        plain = int(child.stdout.readline())
        server, pane = map(int, child.stdout.readline().split())
        members = {pid: shutdown.process_start(pid) for pid in (plain, server, pane)}
        send = os.kill
        try:
            assert (scopes.PROC / str(server) / "comm").read_text() == "tmux: server\n"
            assert all(os.getsid(pid) == child.pid for pid in members), "each is in the owner's session"
            parent = {pid: int((scopes.PROC / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[1])
                      for pid in members}
            assert parent[plain] != child.pid and parent[server] != child.pid, "detached, not in its tree"
            assert parent[pane] == server
            child.descendants[plain] = members[plain]  # the one the stop may signal besides its owner
            stopping_clock(monkeypatch, child, response="exit")
            result = servicectl.stop(runtime)
            assert result.startswith("service stopped")
            assert "its remaining children were killed" in result
            assert (f"a tmux server is in the service's scope (pid {server}); left running with its panes"
                    in result)
            assert shutdown.process_gone(dict(pid=plain, start=members[plain]))
            assert not shutdown.process_gone(dict(pid=server, start=members[server]))
            assert not shutdown.process_gone(dict(pid=pane, start=members[pane]))
            assert child.poll() == 0 and not servicectl.locked(runtime)
        finally:
            monkeypatch.setattr(os, "kill", send)
            for pid in (server, pane, plain):
                _end(pid, members[pid])


def test_an_owner_gone_before_capture_is_an_empty_scope_only_once_the_lock_is_free(tmp_path, monkeypatch):
    monkeypatch.setattr(scopes, "PROC", tmp_path / "proc")  # no reading of the owner: it has ended
    gone = 999999999  # above pid_max: the real /proc proves it gone too
    held = [True]
    monkeypatch.setattr(servicectl, "alive", lambda runtime: held[0])
    with pytest.raises(FileNotFoundError):
        scopes.capture(gone, "700")  # without the runtime nothing proves it was a service that ended
    with pytest.raises(FileNotFoundError):
        scopes.capture(gone, "700", runtime=tmp_path)  # a lock still held refuses
    held[0] = False
    bound = scopes.capture(gone, "700", runtime=tmp_path)
    assert bound.read() == {} and bound.read() == {}
    assert bound.proof == "its owner ended before its scope was read" and not bound.servers


def test_an_owner_that_ends_before_its_scope_is_bound_is_stopped_not_refused(tmp_path, monkeypatch):
    with stand_in(tmp_path, response="exit") as (runtime, child):
        native_kill, bind = os.kill, scopes.capture
        signals = []

        def ended_before_capture(pid, start, **kwargs):
            assert pid == child.pid and servicectl.locked(runtime)
            native_kill(child.pid, signal.SIGTERM)
            child.wait(timeout=20)  # its exit is the event; the timeout only guards the test
            return bind(pid, start, **kwargs)

        monkeypatch.setattr(scopes, "capture", ended_before_capture)
        monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append((pid, sig)))
        result = servicectl.stop(runtime)
        assert result.startswith(f"service stopped · pid {child.pid} · its owner ended before its scope was read")
        assert signals == [] and child.poll() == 0 and not servicectl.locked(runtime)


def vanishing(monkeypatch, pid):
    """``pid`` is listed, then reaped between its /proc file's open and read: the kernel answers ESRCH there."""
    real = scopes.process

    def process(asked):
        if asked == pid:
            raise ProcessLookupError(3, "No such process")
        return real(asked)
    monkeypatch.setattr(scopes, "process", process)


def cgroup_scope(tmp_path, monkeypatch, unit, members):
    root, groups = tmp_path / "proc", tmp_path / "groups"
    folder = groups / "user.slice" / unit
    folder.mkdir(parents=True)
    (folder / "cgroup.procs").write_text("".join(f"{pid}\n" for pid in members))
    (folder / "cgroup.events").write_text("populated 1\n")
    for pid, start in members.items():
        proc(root, pid, start=start, unit=unit)
    monkeypatch.setattr(scopes, "PROC", root)
    monkeypatch.setattr(scopes, "CGROUPS", groups)
    return scopes.capture(301, "700")


def test_a_member_that_vanishes_between_the_listing_and_its_read_is_skipped_as_ended(tmp_path, monkeypatch):
    unit = "qd-service-synthetic-00000005.scope"
    bound = cgroup_scope(tmp_path, monkeypatch, unit, {301: "700", 302: "701"})
    vanishing(monkeypatch, 302)
    assert bound.read() == {301: "700"}
    assert bound.proof == f"processes re-read: cgroup {unit}", "the cgroup itself was read: no fallback"


def test_a_member_that_vanishes_during_the_fallback_read_is_skipped_as_ended(tmp_path, monkeypatch):
    root = tmp_path / "proc"
    proc(root, 301)
    proc(root, 302, start="701")
    proc(root, 303, start="702")
    monkeypatch.setattr(scopes, "PROC", root)
    bound = scopes.capture(301, "700")
    vanishing(monkeypatch, 302)
    assert bound.read() == {301: "700", 303: "702"}
    assert bound.proof == "processes re-read: session 301 (service cgroup unavailable: FileNotFoundError)"


def test_a_membership_that_changes_mid_read_in_any_other_way_stays_uncertain(tmp_path, monkeypatch):
    bound = cgroup_scope(tmp_path, monkeypatch, "qd-service-synthetic-00000006.scope", {301: "700", 302: "701"})
    real, seen = scopes.process, []

    def reused(asked):
        found = real(asked)
        if asked == 302:
            seen.append(asked)
            if len(seen) == 2:
                return found[0], "799", *found[2:]  # the pid read again names another process
        return found
    monkeypatch.setattr(scopes, "process", reused)
    with pytest.raises(scopes.Uncertain, match="membership changed during reading"):
        bound.read()


def test_a_pid_reaped_mid_read_is_gone_for_the_stop(monkeypatch):
    class Reaped:
        def __init__(self, *_):
            pass

        def read_text(self):
            raise ProcessLookupError(3, "No such process")
    monkeypatch.setattr(shutdown, "Path", Reaped)
    assert shutdown.process_gone(dict(pid=302, start="701"))


def collected_world(tmp_path, monkeypatch, number):
    """A bound scope whose cgroup folder holds the owner, 301, and a second process of its session, 302, which only
    a fallback to the session would report."""
    root, groups = tmp_path / "proc", tmp_path / "groups"
    unit = f"qd-service-synthetic-0000000{number}.scope"
    folder = groups / "user.slice" / unit
    folder.mkdir(parents=True)
    (folder / "cgroup.procs").write_text("301\n")
    (folder / "cgroup.events").write_text("populated 1\n")
    proc(root, 301, unit=unit)
    proc(root, 302, start="702")  # of the owner's session, outside the cgroup
    monkeypatch.setattr(scopes, "PROC", root)
    monkeypatch.setattr(scopes, "CGROUPS", groups)
    return root, folder, unit


def test_a_scope_folder_read_in_this_stop_and_gone_after_its_owner_reads_as_empty(tmp_path, monkeypatch):
    """systemd removes a --collect scope's folder once it is empty: after the owner's exit the last re-read meets
    ENOENT on the folder itself. Read earlier in this stop and gone with its owner, it is an empty scope, not an
    unavailable one: no fallback to the session."""
    root, folder, unit = collected_world(tmp_path, monkeypatch, 5)
    bound = scopes.capture(301, "700")
    assert bound.read() == {301: "700"} and bound.proof == f"processes re-read: cgroup {unit}"
    folder.joinpath("cgroup.procs").unlink()
    folder.joinpath("cgroup.events").unlink()
    folder.rmdir()  # removed while its owner still lives: proves nothing, and falls back as before
    assert bound.read() == {301: "700", 302: "702"}
    assert bound.proof == "processes re-read: session 301 (service cgroup unavailable: FileNotFoundError)"
    proc(root, 301, state="Z", unit=unit)  # the owner has exited
    assert bound.read() == {}
    assert bound.proof == f"processes re-read: cgroup {unit} (service cgroup ended with it)"
    (root / "301").joinpath("stat").unlink()
    (root / "301").joinpath("comm").unlink()
    (root / "301").joinpath("cgroup").unlink()
    (root / "301").rmdir()  # reaped
    assert bound.read() == {} and bound.proof.endswith("(service cgroup ended with it)")


def test_a_scope_folder_never_read_in_this_stop_still_falls_back(tmp_path, monkeypatch):
    root, folder, unit = collected_world(tmp_path, monkeypatch, 6)
    bound = scopes.capture(301, "700")
    for name in ("cgroup.procs", "cgroup.events"):
        folder.joinpath(name).unlink()
    folder.rmdir()
    proc(root, 301, state="Z", unit=unit)
    assert bound.read() == {302: "702"}, 'never read here: gone proves nothing, the session is read instead'
    assert bound.proof == "processes re-read: session 301 (service cgroup unavailable: FileNotFoundError)"
