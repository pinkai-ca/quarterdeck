"""Setup's CLI and installed-copy launch path, using synthetic operations only."""
import json
import os
from types import SimpleNamespace

import pytest

from quarterdeck import cli, harness, install, setup, ship
from quarterdeck.drill import Check
from quarterdeck.output import Reading


@pytest.mark.parametrize("check,start,install_fails", [
    ("yes", "yes", False), ("no", "yes", False),
    ("yes", "no", False), ("yes", "yes", True),
])
def test_setup_cli_prepares_copy_only_after_check_and_start_consent(
        tmp_path, monkeypatch, capsys, check, start, install_fails):
    calls = []
    for name, value in {"GIT_AUTHOR_NAME": "Setup Example", "GIT_AUTHOR_EMAIL": "setup@example.invalid",
                        "GIT_COMMITTER_NAME": "Setup Example", "GIT_COMMITTER_EMAIL": "setup@example.invalid",
                        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("GIT_DIR", raising=False)
    monkeypatch.delenv("GIT_WORK_TREE", raising=False)

    class SyntheticOperations(setup.Operations):
        def project_folder(self):
            return tmp_path

        def discover(self, defaults):
            return {name: (Check(name, name == "codex", "synthetic PATH"), "sign-in unknown")
                    for name in harness.NAMES if not harness.get(name).synthetic}

        def credential_present(self, home, filename):
            return False

        def readiness(self, current, selected):
            calls.append("readiness")
            return [Check("readiness", True, "synthetic")]

        def check(self, current, program):
            calls.append("check")
            return [Check("check", True, "synthetic")]

        def status(self, current):
            calls.append("status")
            return Reading({"service": "ok", "seats": [
                SimpleNamespace(name=name, activity=SimpleNamespace(state="idle"))
                for name in current.seats]}, lambda _: ["synthetic status"])

        def access(self, current):
            return "synthetic desk access"

    def prepare(current):
        calls.append("prepare-copy")
        if install_fails:
            raise ship.ShipError("synthetic install failure; no seats started")
        return ["synthetic copy proved"], True

    def delegate(current, *, seat, fresh, reload_required):
        calls.append("installed-up")
        assert (seat, fresh, reload_required) == (None, False, True)
        return ["synthetic installed crew up"]

    monkeypatch.setattr(setup, "Operations", SyntheticOperations)
    monkeypatch.setattr(install, "prepare_up", prepare)
    monkeypatch.setattr(install, "delegate_up", delegate)
    monkeypatch.setattr(cli, "handoff", lambda *_: pytest.fail("setup must keep its explicit target"))
    folder = tmp_path / "crew"
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps(dict(
        folder=str(folder), workspace=str(tmp_path), name="example", programs="codex",
        payment={"codex": "subscription"}, subscription_home={"codex": str(tmp_path / "native-home")},
        use_defaults="yes", accept="yes", check=check, start=start)))
    result = cli.main(["setup", "--answers", str(answers)])
    output = capsys.readouterr()
    assert result == (2 if install_fails else 0)
    assert ship.load(folder).seat("desk").harness == "codex"
    assert ship._git(folder, "status", "--porcelain") == ""
    expected = ["readiness"]
    if check == "yes":
        expected.append("check")
        if start == "yes":
            expected.append("prepare-copy")
            if not install_fails:
                expected += ["installed-up", "status"]
    assert calls == expected
    if install_fails:
        assert "synthetic install failure; no seats started" in output.err
    elif start == "yes" and check == "yes":
        assert "Desk access: synthetic desk access" in output.out
        assert "synthetic copy proved" in output.out
