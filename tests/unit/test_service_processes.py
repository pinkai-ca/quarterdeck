"""Private service scope readings after a lock owner has exited."""

import pytest

from quarterdeck import service_processes as scopes, servicectl
from tests.unit.test_stop_owner_lifetime import stand_in, stopping_clock


def proc(root, pid, *, session=301, group=301, start="700", state="S", unit=None):
    folder = root / str(pid)
    folder.mkdir(parents=True, exist_ok=True)
    fields = [state, "1", str(group), str(session), *(["0"] * 15), start]
    (folder / "stat").write_text(f"{pid} (synthetic service) " + " ".join(fields) + "\n")
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
        assert f"processes re-read: session {child.pid} (service cgroup unavailable)" in proof
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
    assert "session 301" in bound.proof and "cgroup unavailable" in bound.proof


def test_the_owned_process_group_is_available_when_the_session_is_not_owned(tmp_path, monkeypatch):
    root = tmp_path / "proc"
    proc(root, 301, session=300)
    proc(root, 302, session=300, start="701")
    monkeypatch.setattr(scopes, "PROC", root)
    bound = scopes.capture(301, "700")
    assert bound.read() == {301: "700", 302: "701"}
    assert "process group 301" in bound.proof and "cgroup unavailable" in bound.proof
