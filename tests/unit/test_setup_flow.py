"""Ordinary guided setup flows. All machine/account/live operations are replaced.

No inherited protection tests are read or changed by this file. Fake PATH executables
print only a version; full flows use the same production conversation with a fake boundary.
"""
import argparse
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import cli, doctor, harness, setup, ship
from quarterdeck.drill import Check
from quarterdeck.output import Reading


class ScriptedOperations(setup.Operations):
    def __init__(self):
        self.calls = []
        self.refuse = None
        self.check_passes = True

    def discover(self, defaults):
        self.calls.append("discover")
        return {name: (Check(f"program {name}", True, "scripted version 1.2.3"), "sign-in unknown")
                for name in harness.NAMES if not harness.get(name).synthetic}

    def credential_present(self, home, filename):
        return False

    def local_models(self, url):
        return []

    def offered_models(self, url, provider):
        return []

    def readiness(self, current, selected):
        self.calls.append(("readiness", tuple(selected)))
        return [self.refuse] if self.refuse else [Check("chosen programs and accounts", True, "scripted readiness")]

    def check(self, current, name):
        self.calls.append(("check", name))
        return [Check("scripted proof", self.check_passes, "resolve scripted failure" if not self.check_passes else "passed")]

    def change_seat(self, current, name, changes):
        self.calls.append(("set", name, tuple(changes)))
        # The boundary alone is faked: ordinary declaration validation still runs.
        edits = [(f"seats.{name}", key, cli.parse_value(value))
                 for change in changes for key, _, value in [change.partition("=")]]
        return ship.set_values(current, edits)

    def up(self, current):
        self.calls.append("up")
        return ["scripted crew started"]

    def status(self, current):
        self.calls.append("status")
        return Reading({"service": "ok", "seats": [SimpleNamespace(name=name, activity=SimpleNamespace(state="idle"))
                                                  for name in current.seats]}, lambda _: ["scripted status"])

    def access(self, current):
        self.calls.append("access")
        return "scripted desk access"


@pytest.fixture
def answers(tmp_path, monkeypatch):
    monkeypatch.setattr(setup.Operations, "project_folder", lambda _: tmp_path)
    (tmp_path / "ships").mkdir()
    # A synthetic git identity for a disposable repository, never the operator's config.
    for name, value in {"GIT_AUTHOR_NAME": "Setup Example", "GIT_AUTHOR_EMAIL": "setup@example.invalid",
                        "GIT_COMMITTER_NAME": "Setup Example", "GIT_COMMITTER_EMAIL": "setup@example.invalid",
                        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("GIT_DIR", raising=False)
    monkeypatch.delenv("GIT_WORK_TREE", raising=False)
    return dict(folder=str(tmp_path / "ships" / "crew"), name="example", workspace=str(tmp_path), programs="codex",
                payment={"codex": "subscription"}, subscription_home={"codex": str(tmp_path / "sign-in")},
                desk="codex", captain="codex", engineer="codex", check="yes", start="yes",
                use_defaults="yes", accept="yes")


def execute(answers, operations=None, *, interactive=False, read=None):
    lines = []
    operations = operations or ScriptedOperations()
    conversation = setup.Conversation(answers, interactive=interactive, read=read, write=lines.append)
    result = setup.run(argparse.Namespace(json=False), operations=operations, conversation=conversation)
    return result, lines, operations


def test_scripted_subscription_creates_committed_ship_then_checks_in_order(answers):
    result, lines, operations = execute(answers)
    folder = Path(answers["folder"])
    current = ship.load(folder)
    assert current.seat("desk").harness == "codex"
    assert current.workspace == Path(answers["workspace"])
    assert (folder / ".git").is_dir()
    assert ship._git(folder, "status", "--porcelain") == ""
    assert ship._git(folder, "log", "--format=%s", "-1") == "qd setup: declare crew"
    assert (folder / ".gitignore").read_text() == "/runtime/\n"
    assert "Ship's orders" in (folder / "orders.md").read_text()
    assert operations.calls == ["discover", ("readiness", ("codex",)), ("check", "codex"), "up", "status", "access"]
    assert any("sign-in unknown" in line for line in lines)
    assert any("First order" in line for line in result)
    assert (folder.parent / ship.MARK).read_text() == "crew\n"
    assert any("marked as your default ship: qd acts on it when no ship is named" in line for line in lines)


@pytest.mark.parametrize("missing", ["folder", "operator", "office", "payment.codex", "subscription_home.codex",
                                   "workspace", "name", "seats.desk.name", "seats.desk.harness",
                                   "seats.desk.model", "seats.desk.effort", "seats.desk.pools",
                                   "seats.captain.harness", "seats.engineering.harness",
                                   "accept", "check", "start"])
def test_non_tty_names_exact_missing_answer(answers, missing):
    # Defaults and legacy aliases are optional. This case explicitly supplies every
    # current required answer and declines defaults, then removes exactly one.
    for alias in ("desk", "captain", "engineer"):
        answers.pop(alias)
    answers.update(operator="Operator", office="Office", use_defaults="no",
                   seats={role: dict(name=role.capitalize(), harness="codex", model="gpt-6-astra",
                                     effort="medium", pools=["codex"])
                          for role in ("desk", "captain", "engineering")})
    parts = missing.split(".")
    parent = answers
    for part in parts[:-1]:
        parent = parent[part]
    del parent[parts[-1]]
    operations = ScriptedOperations()
    with pytest.raises(ship.ShipError, match=f"missing answer {missing}"):
        execute(answers, operations)
    assert "up" not in operations.calls
    if missing != "start":
        assert not any(isinstance(call, tuple) and call[0] == "check" for call in operations.calls)


def test_program_and_role_flags_may_be_omitted_when_seat_answers_supply_them(answers):
    for key in ("programs", "desk", "captain", "engineer"):
        answers.pop(key)
    answers["seats"] = {role: {"harness": "codex"} for role in ("desk", "captain", "engineering")}
    execute(answers)
    current = ship.load(Path(answers["folder"]))
    assert set(current.seats) == {"desk", "captain", "engineering"}
    assert all(seat.harness == "codex" for seat in current.seats.values())


def test_key_payment_stores_only_variable_reference(answers):
    answers["payment"] = {"codex": "key"}
    answers["provider"] = {"codex": "openai"}
    answers["key_env"] = {"codex": "example-setup-key"}
    execute(answers)
    current = ship.load(Path(answers["folder"]))
    assert current.pool("codex").api_key == "keys:example-setup-key"
    assert current.pool("codex").config_dir is None


def test_a_subscription_can_name_a_key_to_fall_back_to(answers):
    answers["key_fallback"] = {"codex": "openai"}
    execute(answers)
    current = ship.load(Path(answers["folder"]))
    assert current.pool("codex-key").api_key == "keys:openai" and current.pool("codex-key").provider == "openai"
    assert current.pool("codex").kind == "subscription"
    assert all(seat.pools[-1] == "codex-key" and seat.pools[0] == "codex" for seat in current.seats.values()
               if seat.harness == "codex")


@pytest.mark.parametrize("program", ["pi", "opencode"])
@pytest.mark.parametrize("payment", ["local", "endpoint"])
def test_explicit_endpoint_operator_model_and_no_key(answers, program, payment):
    answers.update(programs=program, desk=program, captain=program, engineer=program,
                   payment={program: payment}, provider={program: "example-operator"},
                   endpoint={program: "http://127.0.0.1:8181/v1"}, model={program: "example-model"},
                   key_env={program: "none"})
    execute(answers)
    current = ship.load(Path(answers["folder"]))
    assert current.pool(program).provider == "example-operator"
    assert current.pool(program).kind == "endpoint"
    assert current.pool(program).api_key is None
    assert current.seat("desk").model == "example-model"


def test_endpoint_key_is_explicit_reference(answers):
    answers.update(programs="pi", desk="pi", captain="pi", engineer="pi", payment={"pi": "endpoint"},
                   provider={"pi": "example-operator"}, endpoint={"pi": "https://example.invalid/v1"},
                   model={"pi": "example-model"}, key_env={"pi": "example-endpoint-key"})
    execute(answers)
    assert ship.load(Path(answers["folder"])).pool("pi").api_key == "keys:example-endpoint-key"


def test_adapter_rejects_unsupported_subscription(answers):
    answers.update(programs="pi", desk="pi", captain="pi", engineer="pi",
                   model={"pi": "example-model"}, payment={"pi": "subscription"}, provider={"pi": "openai"})
    with pytest.raises(ship.ShipError, match="subscription"):
        execute(answers)
    assert not Path(answers["folder"]).exists()


def test_unsigned_readiness_stops_before_live_check_and_names_rerun(answers):
    operations = ScriptedOperations()
    operations.refuse = Check("pool codex", False, "no credential; complete native sign-in")
    with pytest.raises(ship.ShipError, match="readiness refused.*no credential.*--existing keep.*--accept yes --check yes --start yes"):
        execute(answers, operations)
    assert operations.calls == ["discover", ("readiness", ("codex",))]


def test_native_check_refusal_does_not_launch(answers):
    operations = ScriptedOperations()
    operations.check_passes = False
    with pytest.raises(ship.ShipError, match="qd check codex failed"):
        execute(answers, operations)
    assert "up" not in operations.calls


def test_declining_checks_saves_ship_without_spending(answers):
    answers["check"] = "no"
    result, _, operations = execute(answers)
    assert Path(answers["folder"]).is_dir()
    assert "declined" in result[0]
    assert not any(isinstance(call, tuple) and call[0] == "check" for call in operations.calls)
    assert "up" not in operations.calls


def test_declining_start_does_not_launch(answers):
    answers["start"] = "no"
    result, _, operations = execute(answers)
    assert "crew not started" in result[0]
    assert "up" not in operations.calls


def test_existing_keep_does_not_rewrite_declaration(answers):
    execute(answers)
    folder = Path(answers["folder"])
    before = (folder / "ship.toml").read_bytes(), (folder / "orders.md").read_bytes()
    result, lines, operations = execute(dict(folder=str(folder), existing="keep", check="yes", start="yes", use_defaults="yes", accept="yes"))
    assert before == ((folder / "ship.toml").read_bytes(), (folder / "orders.md").read_bytes())
    assert any("Ship name:" in line and "example" in line for line in lines)
    assert "up" in operations.calls


def test_existing_update_reuses_set_boundary_and_reports_relaunch(answers):
    execute(answers)
    original_orders = (Path(answers["folder"]) / "orders.md").read_bytes()
    answers.update(existing="update", payment={"codex": "key"}, provider={"codex": "openai"}, key_env={"codex": "example-setup-key"})
    result, _, operations = execute(answers)
    assert any(isinstance(call, tuple) and call[0] == "set" for call in operations.calls)
    assert "up" not in operations.calls
    assert any("restart desk" in line for line in result)
    current = ship.load(Path(answers["folder"]))
    assert current.pool(current.seat("desk").pools[0]).kind == "key"
    assert (current.folder / "orders.md").read_bytes() == original_orders


@pytest.mark.parametrize("mode", ["keep", "update"])
def test_existing_rerun_leaves_default_mark_alone(answers, mode):
    execute(answers)
    mark = Path(answers["folder"]).parent / ship.MARK
    mark.write_text("another-ship\n")
    before = mark.read_bytes(), mark.stat().st_mtime_ns
    _, lines, _ = execute({**answers, "existing": mode})
    assert (mark.read_bytes(), mark.stat().st_mtime_ns) == before
    assert not any("marked as your default ship" in line for line in lines)


def test_existing_rerun_needs_explicit_answer_without_tty(answers):
    execute(answers)
    rerun = dict(folder=answers["folder"], operator="Operator", office="Office", name="example",
                 workspace=answers["workspace"], use_defaults="no", accept="yes", check="yes", start="yes")
    with pytest.raises(ship.ShipError, match="missing answer existing"):
        execute(rerun)


def test_existing_non_ship_folder_is_not_overwritten(answers):
    folder = Path(answers["folder"])
    folder.mkdir()
    (folder / "note.txt").write_text("keep this")
    with pytest.raises(ship.ShipError, match="holds no ship.toml"):
        execute(answers)
    assert list(folder.iterdir()) == [folder / "note.txt"]


def test_selected_programs_beyond_three_roles_get_worker_seat(answers):
    answers.update(programs="codex,claude", payment={"codex": "subscription", "claude": "subscription"},
                   subscription_home={"codex": "/example/codex", "claude": "/example/claude"})
    execute(answers)
    current = ship.load(Path(answers["folder"]))
    assert current.seat("worker-claude").harness == "claude"
    assert current.seat("worker-claude").role == "worker"


def test_scripted_interactive_questions_use_same_flow(answers):
    scripted = iter(["", "example", "", answers["workspace"], answers["folder"],
                     "", "codex", "", "", "", "codex", "", "", "", "codex", "", "",
                     "subscription", answers["subscription_home"]["codex"],
                     "", "", "", "accept", "", ""])
    result, _, operations = execute({}, interactive=True, read=lambda _: next(scripted))
    assert "up" in operations.calls
    assert "Desk access" in result[0]
    assert list(scripted) == []


def test_enter_only_checks_starts_and_reaches_desk(answers, tmp_path, monkeypatch):
    monkeypatch.setattr(setup.Operations, "workspace_default", lambda _: str(tmp_path))
    operations = ScriptedOperations()
    monkeypatch.setattr(operations, "discover", lambda _: {
        "codex": (Check("program codex", True, "synthetic program"), "sign-in unknown")})
    prompts = []

    def enter(prompt):
        prompts.append(prompt)
        return ""

    result, lines, operations = execute({}, operations, interactive=True, read=enter)
    assert operations.calls == [("readiness", ("codex",)), ("check", "codex"), "up", "status", "access"]
    assert "Desk access" in result[0]
    assert any("Run qd check" in prompt and "subscription" in prompt and "[yes]" in prompt for prompt in prompts)
    assert any("Bring up the crew?" in prompt and "subscription" in prompt and "[yes]" in prompt for prompt in prompts)
    assert (tmp_path / "ships" / "aurora" / "ship.toml").is_file()


def test_flags_override_answers_file(answers, tmp_path):
    file = tmp_path / "answers.json"
    file.write_text(json.dumps(answers))
    args = cli.build_parser().parse_args(["setup", "--answers", str(file), "--name", "renamed", "--payment", "codex=key"])
    merged = setup.read_answers(args)
    assert merged["name"] == "renamed"
    assert merged["payment"] == {"codex": "key"}
    execute(merged | {"provider": {"codex": "openai"}, "key_env": {"codex": "example-setup-key"}})


def test_cli_flags_bind_setup_answers():
    parser = cli.build_parser()
    args = parser.parse_args(["setup", "--folder", "/example/crew", "--programs", "codex", "--payment", "codex=key",
                              "--key-env", "codex=example-setup-key", "--provider", "codex=openai", "--desk", "codex", "--captain", "codex",
                              "--engineer", "codex", "--workspace", "/example/work", "--name", "example",
                              "--check", "yes", "--start", "no"])
    assert setup.read_answers(args)["key_env"] == {"codex": "example-setup-key"}
    assert args.run is cli.cmd_setup


def test_existing_operation_delegates_to_qd_set_path(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "set_seat", lambda *args: calls.append(args) or ["same path"])
    marker = object()
    assert setup.Operations().change_seat(marker, "desk", ["model=example"]) == ["same path"]
    assert calls == [(marker, "desk", ["model=example"])]


def test_doctor_discovery_uses_registered_adapters_and_fake_path_only(tmp_path, monkeypatch):
    # Never search the interpreter's directory for a real installed program in this test.
    from quarterdeck import seat
    monkeypatch.setattr(seat, "qd_bin", lambda: tmp_path)
    monkeypatch.setattr(setup.Operations, "credential_present", lambda *_: False)
    monkeypatch.setenv("PATH", str(tmp_path))
    for name in harness.NAMES:
        reader = harness.get(name)
        if not reader.synthetic:
            file = tmp_path / reader.program
            file.write_text("#!/bin/sh\nexit 99\n")
            file.chmod(0o755)
    found = setup.Operations().discover({})
    assert set(found) == {name for name in harness.NAMES if not harness.get(name).synthetic}
    assert all(row.passed for row, _ in found.values())
    assert "version unchecked" in found["codex"][0].detail
    assert "unknown" in found["codex"][1]


def test_missing_program_doctor_row(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "program_path", lambda: str(tmp_path))
    row = doctor.program_check("codex")
    assert not row.passed
    assert "PATH" in row.detail


@pytest.mark.parametrize("value", ["not a name", "env:EXAMPLE_KEY", "EXAMPLE_KEY", "sk-synthetic-" + "a" * 40])
def test_key_answer_is_a_name(value):
    with pytest.raises(ship.ShipError, match="the NAME of a key") as refused:
        setup.key_reference(value)
    assert value not in str(refused.value), "what was answered may be a pasted key"


@pytest.mark.parametrize("value", ["https://name:password@example.invalid/v1", "https://example.invalid/v1?key=example", "file:///example"])
def test_endpoint_answers_do_not_embed_credentials(value):
    with pytest.raises(ship.ShipError, match="URL without credentials"):
        setup.safe_endpoint(value)


def test_openrouter_is_not_an_automatic_payment_route(answers):
    answers.update(programs="pi", desk="pi", captain="pi", engineer="pi",
                   payment={"pi": "endpoint"}, model={"pi": "example-model"})
    with pytest.raises(ship.ShipError, match="missing answer provider.pi"):
        execute(answers)
    assert not Path(answers["folder"]).exists()


def test_explicit_openrouter_key_uses_declared_endpoint_and_variable(answers):
    answers.update(programs="pi", desk="pi", captain="pi", engineer="pi", payment={"pi": "key"},
                   provider={"pi": "openrouter"}, model={"pi": "example-model"},
                   key_env={"pi": "example-router-key"})
    execute(answers)
    current = ship.load(Path(answers["folder"]))
    assert current.pool("pi").provider == "openrouter"
    assert current.pool("pi").endpoint == "https://openrouter.ai/api/v1"
    assert current.pool("pi").api_key == "keys:example-router-key"


def test_interactive_readiness_wait_is_person_driven_and_rereads(answers):
    operations = ScriptedOperations()
    operations.refuse = Check("pool codex", False, "complete native sign-in yourself")
    answers.update(wait_for_sign_in="yes", operator="Operator", office="Office",
                   seats={role: dict(name=role.capitalize(), harness="codex", model="gpt-6-astra",
                                     effort="medium", pools=["codex"])
                          for role in ("desk", "captain", "engineering")})
    def person_finished(prompt):
        assert prompt.startswith("Complete the native steps yourself")
        operations.refuse = None
        return ""
    result, _, operations = execute(answers, operations, interactive=True, read=person_finished)
    assert operations.calls.count(("readiness", ("codex",))) == 2
    assert "up" in operations.calls


def test_status_does_not_claim_success_when_crew_is_down(answers):
    operations = ScriptedOperations()
    operations.status = lambda current: Reading({"service": "down", "seats": []}, lambda _: ["service down"])
    with pytest.raises(ship.ShipError, match="not confirmed by status"):
        execute(answers, operations)
    assert "access" not in operations.calls


def test_missing_endpoint_model_is_named_before_writing(answers):
    answers.update(programs="pi", desk="pi", captain="pi", engineer="pi",
                   payment={"pi": "endpoint"}, provider={"pi": "example-operator"},
                   endpoint={"pi": "http://127.0.0.1:8181/v1"}, key_env={"pi": "none"})
    with pytest.raises(ship.ShipError, match="missing answer seats.desk.model"):
        execute(answers)
    assert not Path(answers["folder"]).exists()


def test_existing_addition_preserves_other_seats_and_orders(answers):
    execute(answers)
    folder = Path(answers["folder"])
    orders = (folder / "orders.md").read_bytes()
    answers.update(existing="update", programs="codex,pi", payment={"codex": "subscription", "pi": "endpoint"},
                   provider={"pi": "example-operator"}, endpoint={"pi": "http://127.0.0.1:8181/v1"},
                   model={"pi": "example-model"}, key_env={"pi": "none"})
    execute(answers)
    current = ship.load(folder)
    assert current.seat("desk").harness == "codex"
    assert current.seat("worker-pi").harness == "pi"
    assert (folder / "orders.md").read_bytes() == orders


@pytest.mark.parametrize("document", ["[]", '{"unknown_answer":"value"}', '{"payment":"subscription"}'])
def test_answers_file_validation_is_an_ordinary_refusal(tmp_path, document):
    file = tmp_path / "answers.json"
    file.write_text(document)
    args = cli.build_parser().parse_args(["setup", "--answers", str(file)])
    with pytest.raises(ship.ShipError, match="answer"):
        setup.read_answers(args)


def test_non_tty_cli_uses_same_setup_without_inherited_ship_handoff(answers, tmp_path, monkeypatch, capsys):
    text, orders, _ = setup.template("office")
    ship.create(tmp_path / "office", text, orders)
    file = tmp_path / "answers.json"
    file.write_text(json.dumps(answers))
    operations = ScriptedOperations()
    monkeypatch.setattr(setup, "Operations", lambda: operations)
    monkeypatch.setattr(cli, "handoff", lambda *_: pytest.fail("setup must use its explicit target, not an inherited ship"))
    monkeypatch.setenv("QD_SHIP", str(tmp_path / "unrelated"))
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: False))
    assert cli.main(["setup", "--answers", str(file)]) == 0
    assert "Desk access" in capsys.readouterr().out
    assert operations.calls[-3:] == ["up", "status", "access"]


def test_unfinished_first_commit_requires_person_to_resolve_git(answers, monkeypatch):
    def refuse_commit(*_, **__):
        raise ship.ShipError("example git identity is unavailable")
    monkeypatch.setattr(ship, "commit", refuse_commit)
    with pytest.raises(ship.ShipError, match="first commit is unfinished.*resolve the git reason yourself"):
        execute(answers)
    assert (Path(answers["folder"]) / "ship.toml").exists()
    with pytest.raises(ship.ShipError, match="has no first commit"):
        execute(dict(folder=answers["folder"], existing="keep", check="yes", start="yes", use_defaults="yes", accept="yes"))


@pytest.mark.parametrize("relative", ["crew", "ships/group/crew"])
def test_new_ship_folder_must_be_directly_under_ships_before_acceptance(answers, tmp_path, relative):
    answers["folder"] = str(tmp_path / relative)
    operations = ScriptedOperations()
    lines = []
    conversation = setup.Conversation(answers, interactive=False, write=lines.append)
    with pytest.raises(ship.ShipError, match="a new ship lives directly under .*; choose a folder name there"):
        setup.run(argparse.Namespace(json=False), operations=operations, conversation=conversation)
    assert not Path(answers["folder"]).exists()
    assert not (tmp_path / "ships" / ship.MARK).exists()
    assert not any(line.startswith("Ship name:") for line in lines)
    assert operations.calls == ["discover"]


@pytest.mark.parametrize("mode", ["keep", "update"])
def test_existing_ship_at_legacy_path_stays_there_and_preserves_mark(answers, tmp_path, mode):
    execute(answers)
    original = Path(answers["folder"])
    legacy = tmp_path / "legacy-crew"
    original.rename(legacy)
    mark = tmp_path / "ships" / ship.MARK
    mark.write_text("another-ship\n")
    before = mark.read_bytes(), mark.stat().st_mtime_ns
    result, lines, operations = execute({**answers, "folder": str(legacy), "existing": mode})
    assert ship.load(legacy).name == "example"
    assert not original.exists()
    assert (mark.read_bytes(), mark.stat().st_mtime_ns) == before
    assert not any("marked as your default ship" in line for line in lines)
    assert "up" in operations.calls
    assert "Desk access" in result[0]


def test_default_ship_folder_is_in_quarterdeck_not_the_workspace(answers, tmp_path):
    workspace = tmp_path / "person-project"
    workspace.mkdir()
    answers.pop("folder")
    answers["workspace"] = str(workspace)
    answers["check"] = "no"
    execute(answers)
    current = ship.load(tmp_path / "ships" / answers["name"])
    assert current.workspace == workspace
    assert list(workspace.iterdir()) == []


# -- the project folder: always a suggestion where one is known, never an exit on Enter --------------------------


def test_enter_at_a_project_folder_with_no_suggestion_asks_again_and_never_exits(answers, tmp_path, monkeypatch):
    monkeypatch.setattr(setup.Operations, "workspace_default", lambda _: None)
    operations = ScriptedOperations()
    monkeypatch.setattr(operations, "discover", lambda _: {
        "codex": (Check("program codex", True, "synthetic program"), "sign-in unknown")})
    asked = []

    def enter(prompt):
        if prompt.startswith("Project folder"):
            asked.append(prompt)
            return str(tmp_path) if len(asked) == 3 else ""
        return ""

    result, lines, operations = execute({}, operations, interactive=True, read=enter)
    assert asked == ["Project folder: "] * 3, "no suggestion is shown where none is known"
    assert lines.count("the crew needs a folder to work in: type its path") == 2
    assert "Desk access" in result[0]
    assert ship.load(tmp_path / "ships" / "aurora").workspace == tmp_path


def test_enter_at_a_suggested_project_folder_takes_it(answers, tmp_path, monkeypatch):
    monkeypatch.setattr(setup.Operations, "workspace_default", lambda _: str(tmp_path))
    operations = ScriptedOperations()
    monkeypatch.setattr(operations, "discover", lambda _: {
        "codex": (Check("program codex", True, "synthetic program"), "sign-in unknown")})
    prompts = []
    result, lines, _ = execute({}, operations, interactive=True, read=lambda prompt: prompts.append(prompt) or "")
    assert f"Project folder [{tmp_path}]: " in prompts
    assert "the crew needs a folder to work in: type its path" not in lines


def test_an_answers_file_without_a_workspace_still_refuses_with_its_remedy(answers):
    del answers["workspace"]
    with pytest.raises(setup.ShipError, match=r"missing answer workspace; pass --workspace ANSWER or --answers FILE"):
        execute(answers)


@pytest.fixture
def places(tmp_path, monkeypatch):
    """A synthetic Quarterdeck checkout, a plain folder, another repository and a second Quarterdeck clone."""
    from quarterdeck import setup_proposal, standing
    from tests.conftest import git

    def repository(path, quarterdeck=False):
        path.mkdir(parents=True)
        git(path, "init", "-q")
        if quarterdeck:
            (path / "pyproject.toml").write_text('[project]\nname = "quarterdeck"\n')
            (path / "src" / "quarterdeck").mkdir(parents=True)
        return path

    # git looks no higher than this test's folder, which itself lies inside the checkout that runs the tests
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    framework = repository(tmp_path / "quarterdeck", quarterdeck=True)
    monkeypatch.setattr(standing, "project_dir", lambda: framework)
    plain = tmp_path / "plain"
    plain.mkdir()
    (framework / "work").mkdir()
    return SimpleNamespace(framework=framework, inside=framework / "work", plain=plain,
                           other=repository(tmp_path / "other"), clone=repository(tmp_path / "clone", quarterdeck=True),
                           default=setup_proposal.workspace_default)


@pytest.mark.parametrize("where,suggested", [("plain", True), ("other", True), ("framework", False),
                                             ("inside", False), ("clone", False)])
def test_the_suggestion_is_the_start_folder_unless_it_is_the_quarterdeck_checkout_or_inside_it(
        places, monkeypatch, where, suggested):
    folder = getattr(places, where)
    monkeypatch.chdir(folder)
    assert places.default() == (str(folder.resolve()) if suggested else None)


@pytest.mark.parametrize("interactive", [False, True])
def test_first_setup_creates_and_starts_office_before_crew_from_one_conversation(
        answers, tmp_path, monkeypatch, capsys, interactive):
    operations = ScriptedOperations()
    def up_desk(office):
        assert office.kind == "office"
        assert not Path(answers["folder"]).exists()
        operations.calls.append("office up")
        return ["scripted office started"]
    monkeypatch.setattr(operations, "up_desk", up_desk)
    monkeypatch.setattr(setup, "Operations", lambda: operations)
    if interactive:
        data = {key: value for key, value in answers.items() if key not in ("accept", "start")}
        prompts = []
        def respond(prompt):
            prompts.append(prompt)
            if prompt.startswith("Change an item"):
                return "accept"
            return "yes" if prompt.startswith(("Continue with", "Start the office")) else ""
        lines = []
        conversation = setup.Conversation(data, interactive=True, read=respond, write=lines.append)
        args = cli.build_parser().parse_args(["setup"])
        setup.first_step(args, operations=operations, conversation=conversation)
        assert sum(prompt.startswith("What should the crew call you?") for prompt in prompts) == 1
        assert sum(prompt.startswith("Office name") for prompt in prompts) == 1
        assert prompts[0].startswith("Continue with a ship and crew")
        assert sum(prompt.startswith("Change an item") for prompt in prompts) == 1
        assert sum(prompt.startswith("Start the office and crew") for prompt in prompts) == 1
    else:
        file = tmp_path / "answers.json"
        file.write_text(json.dumps(answers))
        assert cli.main(["setup", "--answers", str(file)]) == 0
        assert "scripted office started" in capsys.readouterr().out
    assert ship.load(tmp_path / "office").kind == "office"
    crew = ship.load(Path(answers["folder"]))
    assert {seat.role for seat in crew.seats.values()} == {"captain", "engineering"}
    assert operations.calls.index("office up") < operations.calls.index(("check", "codex")) < operations.calls.index("up")


def test_first_setup_file_without_accept_writes_neither_office_nor_crew(answers, tmp_path):
    answers.pop("accept")
    conversation = setup.Conversation(answers, interactive=False, write=lambda _: None)
    with pytest.raises(setup.ShipError, match="missing answer accept"):
        setup.first_step(argparse.Namespace(json=False), operations=ScriptedOperations(), conversation=conversation)
    assert not (tmp_path / "office").exists()
    assert not Path(answers["folder"]).exists()


def test_terminal_can_stop_at_office_without_crew_questions(answers, tmp_path, monkeypatch):
    operations = ScriptedOperations()
    calls = []
    monkeypatch.setattr(operations, "up_desk", lambda office: calls.append(office) or ["office up"])
    prompts = []
    def respond(prompt):
        prompts.append(prompt)
        return "no" if prompt.startswith("Continue") else "yes"
    conversation = setup.Conversation({}, interactive=True, read=respond, write=lambda _: None)
    result = setup.first_step(argparse.Namespace(json=False), operations=operations, conversation=conversation)
    assert "Office ready" in result[-1]
    assert len(calls) == 1 and operations.calls == ["access"]
    assert len(prompts) == 3
    assert prompts[1].startswith("Create this project's office")
    assert prompts[2].startswith("Start the office's desk")
    assert not Path(answers["folder"]).exists()


@pytest.mark.parametrize("program", ["codex", "claude"])
def test_combined_setup_writes_the_chosen_desk_only_to_the_office(answers, tmp_path, program):
    answers.update(desk=program, programs="codex,claude",
                   payment={"codex": "subscription", "claude": "subscription"},
                   subscription_home={"codex": str(tmp_path / "codex"), "claude": str(tmp_path / "claude")},
                   office="My Office", start="no", check="no",
                   seats={"desk": {"name": "Chosen desk", "model": "chosen-model", "effort": "high"}})
    lines = []
    operations = ScriptedOperations()
    setup.first_step(argparse.Namespace(json=False), operations=operations,
                     conversation=setup.Conversation(answers, interactive=False, write=lines.append))
    office = ship.load(tmp_path / "office")
    desk = office.seat("desk")
    assert (desk.display, desk.harness, desk.model, desk.effort, desk.pools) == (
        "Chosen desk", program, "chosen-model", "high", (program,))
    assert office.pool(program).config_dir == tmp_path / program
    assert set(office.pools) == {program}
    assert office.session_of(desk).human
    assert office.session_of(desk).title == "My Office"
    crew = ship.load(Path(answers["folder"]))
    assert "desk" not in crew.seats and all(seat.role != "desk" for seat in crew.seats.values())
    assert all(not session.human for session in crew.sessions.values())
    assert "desk@office" in (crew.folder / "orders.md").read_text()
    assert any("Office" in line and "Desk" in line and "Chosen desk" in line for line in lines)
    assert any("Ship" in line and "Captain" in line for line in lines)
    assert any("Ship" in line and "Engineer" in line for line in lines)


def test_combined_setup_preserves_the_desk_fallback_choices(answers, tmp_path):
    answers.update(programs="codex,claude", check="no", start="no",
                   payment={"codex": "subscription", "claude": "subscription"},
                   subscription_home={"codex": str(tmp_path / "codex"), "claude": str(tmp_path / "claude")},
                   seats={"desk": {"model": "gpt-6-astra", "effort": "medium", "pools": ["codex", {
                       "pool": "claude", "harness": "claude", "model": "claude-opus-5-5", "effort": "high"}]}})
    setup.first_step(argparse.Namespace(json=False), operations=ScriptedOperations(),
                     conversation=setup.Conversation(answers, interactive=False, write=lambda _: None))
    office = ship.load(tmp_path / "office")
    assert office.seat("desk").pools == ("codex", "claude")
    fallback = office.seat("desk").for_pool("claude")
    assert (fallback.harness, fallback.model, fallback.effort) == ("claude", "claude-opus-5-5", "high")
    assert "desk" not in ship.load(Path(answers["folder"])).seats


@pytest.mark.parametrize("mode", ["new", "keep", "update"])
def test_existing_office_crew_wizard_never_adds_a_desk(answers, tmp_path, mode):
    text, orders, _ = setup.template("office")
    text = ship.edit(text, "sessions.office", "title", "Custom Office")
    answers["office"] = "Custom Office"
    office = ship.create(tmp_path / "office", text, orders)
    before = office.file.read_bytes()
    answers.update(check="no", start="no")
    args = argparse.Namespace(json=False)
    def run(data):
        return setup.first_step(args, operations=ScriptedOperations(),
                                conversation=setup.Conversation(data, interactive=False, write=lambda _: None))
    run(answers)
    if mode != "new":
        run({**answers, "existing": mode})
    crew = ship.load(Path(answers["folder"]))
    assert set(crew.seats) == {"captain", "engineering"}
    assert not any(seat.role == "desk" for seat in crew.seats.values())
    assert office.file.read_bytes() == before


def test_combined_setup_access_points_to_the_office(answers, tmp_path, monkeypatch):
    operations = ScriptedOperations()
    monkeypatch.setattr(operations, "up_desk", lambda _: ["office started"])
    seen = []
    monkeypatch.setattr(operations, "access", lambda current: seen.append(current.folder) or str(current.folder))
    result = setup.first_step(argparse.Namespace(json=False), operations=operations,
                             conversation=setup.Conversation(answers, interactive=False, write=lambda _: None))
    assert seen == [tmp_path / "office", tmp_path / "office"]
    assert f"Desk access: {tmp_path / 'office'}" in result[0]
