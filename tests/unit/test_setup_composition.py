"""Ordinary setup composition checks with synthetic operations and temporary ships."""
import os
import tomllib

import pytest

from quarterdeck import doctor, harness, setup, setup_proposal, ship
from quarterdeck.drill import Check


class ProposalOperations(setup.Operations):
    def discover(self, defaults):
        return {name: (Check(name, True, "synthetic PATH"), "sign-in unknown")
                for name in harness.NAMES if not harness.get(name).synthetic}

    def credential_present(self, home, filename):
        return False

    def local_models(self, url):
        return []

    def offered_models(self, url, provider):
        return []

    def subscription_home(self, program, defaults):
        return "/synthetic/sign-in"


def proposal(tmp_path, programs, **extra):
    answers = dict(name="aurora", workspace=str(tmp_path), folder=str(tmp_path / "crew"),
                   programs=programs, use_defaults="yes", **extra)
    operations = ProposalOperations()
    operations.project_folder = lambda: tmp_path
    text, orders, defaults = setup.template()
    conversation = setup.Conversation(answers, interactive=False,
                                      read=lambda _: pytest.fail("scripted setup must not prompt"),
                                      write=lambda _: None)
    return setup_proposal.build(conversation, operations, text, orders, defaults,
                                operations.discover(defaults))


@pytest.mark.parametrize("program", ["claude", "codex", "grok", "opencode", "pi"])
def test_generated_thresholds_follow_selected_harness_not_template(tmp_path, program):
    endpoint = program in ("opencode", "pi")
    fields = (dict(payment={program: "endpoint"}, provider={program: "synthetic-operator"},
                   endpoint={program: "http://127.0.0.1:8181/v1"},
                   model={program: "synthetic-model"}, key_env={program: "none"}) if endpoint else {})
    proposed = proposal(tmp_path, program, **fields)
    current = proposed.ship
    assert set(current.seats) == {"desk", "captain", "engineering"}
    raw = tomllib.loads(proposed.text)
    for name, seat in current.seats.items():
        assert seat.harness == program
        if program in ("claude", "codex"):
            assert seat.fresh_after_compactions == 2
            assert raw["seats"][name]["fresh_after_compactions"] == 2
        else:
            assert seat.fresh_after_compactions is None
            assert "fresh_after_compactions" not in raw["seats"][name]


def test_mixed_crew_and_unused_program_worker_have_matching_defaults(tmp_path):
    mixed = proposal(tmp_path, "claude,codex,grok", desk="claude", captain="grok", engineer="codex")
    assert {name: seat.fresh_after_compactions for name, seat in mixed.ship.seats.items()} == {
        "desk": 2, "captain": None, "engineering": 2}
    included = proposal(tmp_path, "claude,codex", desk="codex", captain="codex", engineer="codex")
    assert included.ship.seat("worker-claude").role == "worker"
    assert all(seat.fresh_after_compactions == 2 for seat in included.ship.seats.values())


def test_default_template_keeps_geordi_and_handoff_line(tmp_path):
    text, orders, _ = setup.template()
    current = ship._from_text(text, tmp_path)
    assert current.seat("engineering").display == "Geordi"
    assert all(seat.fresh_after_compactions == 2 for seat in current.seats.values())
    previous = "- What must survive a fresh start goes in a post, never only in your context.\n"
    line = ("- A seat that starts fresh first posts a handoff of what it has in hand, "
            "addressed to itself, so that the fresh seat reads it among its open messages.\n")
    assert previous + line in orders
    assert orders.count(line) == 1


def test_summary_keeps_five_columns_without_new_seat_annotations(tmp_path):
    current = proposal(tmp_path, "codex").ship
    data = setup_proposal.summary(current, setup_proposal.display_control)
    assert all(row["fresh_after_compactions"] == 2 for row in data["seats"])
    assert all(list(row) == ["seat", "name", "harness", "model", "effort",
                             "fresh_after_compactions"] for row in data["seats"])
    assert data["seats"][2]["name"] == "Geordi"
    rendered = setup_proposal.render_summary(data)
    assert any("Geordi" in line for line in rendered)
    assert not any("fresh after compactions:" in line for line in rendered)
    other = setup_proposal.summary(proposal(tmp_path, "grok").ship, setup_proposal.display_control)
    assert all("fresh_after_compactions" not in row for row in other["seats"])


def test_readiness_reuses_each_program_version_once(tmp_path, monkeypatch):
    current = proposal(tmp_path, "claude,codex", desk="claude", captain="codex", engineer="codex").ship
    calls, received = [], []
    monkeypatch.setattr(doctor, "program_path", lambda: str(tmp_path))
    monkeypatch.setattr(doctor.shutil, "which", lambda program, **_: "/synthetic/" + program)

    def version(reader, runtime):
        calls.append((reader.name, runtime))
        return "synthetic-version-" + reader.name

    monkeypatch.setattr(doctor, "_version_in_runtime", version)
    monkeypatch.setattr(doctor, "pool_check", lambda pool: Check(pool.name, True, "synthetic pool"))

    def readiness(chosen, *, versions):
        received.append((set(chosen.seats), versions))
        return [Check("selection", True, "synthetic readiness")]

    monkeypatch.setattr(doctor, "seat_readiness", readiness)
    rows = setup.Operations().readiness(current, ["claude", "codex"])
    assert calls == [("claude", current.runtime), ("codex", current.runtime)]
    assert received == [(set(current.seats), {"claude": "synthetic-version-claude",
                                             "codex": "synthetic-version-codex"})]
    assert all(row.passed for row in rows)
    assert rows[-1].detail == "synthetic readiness"


def test_program_check_retains_row_only_interface(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "program_path", lambda: str(tmp_path))
    row = doctor.program_check("codex", tmp_path)
    assert isinstance(row, Check)
    assert not row.passed
    assert "PATH" in row.detail


def existing_ship(tmp_path, monkeypatch, program, threshold):
    for key, value in {"GIT_AUTHOR_NAME": "Setup Example", "GIT_AUTHOR_EMAIL": "setup@example.invalid",
                       "GIT_COMMITTER_NAME": "Setup Example", "GIT_COMMITTER_EMAIL": "setup@example.invalid",
                       "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("GIT_DIR", raising=False)
    monkeypatch.delenv("GIT_WORK_TREE", raising=False)
    original = proposal(tmp_path, program)
    text = "\n".join(line for line in original.text.splitlines()
                     if not line.startswith("fresh_after_compactions =")) + "\n"
    if threshold is not None:
        for role in ("desk", "captain", "engineering"):
            text = ship.edit(text, "seats." + role, "fresh_after_compactions", threshold)
    return ship.create(original.ship.folder, text, original.orders)


@pytest.mark.parametrize("threshold", [None, 0, 7])
@pytest.mark.parametrize("mode", ["keep", "update"])
def test_retained_harness_preserves_explicit_threshold_or_absence(tmp_path, monkeypatch, threshold, mode):
    before = existing_ship(tmp_path, monkeypatch, "codex", threshold)
    text = before.file.read_text()
    result = proposal(tmp_path, "codex", existing=mode)
    assert all(seat.fresh_after_compactions == threshold for seat in result.ship.seats.values())
    assert text == result.existing.file.read_text()
    assert not any(key == "fresh_after_compactions" for _, key, _ in result.edits)
    raw = tomllib.loads(result.text)
    assert all(("fresh_after_compactions" in fields) == (threshold is not None)
               for fields in raw["seats"].values())


def test_switch_away_discloses_retained_key_with_no_effect(tmp_path, monkeypatch):
    existing_ship(tmp_path, monkeypatch, "codex", 7)
    result = proposal(tmp_path, "grok", existing="update", desk="grok", captain="grok", engineer="grok",
                      seats={role: {"model": "synthetic-grok", "effort": "medium"}
                             for role in ("desk", "captain", "engineering")})
    assert all(seat.harness == "grok" and seat.fresh_after_compactions == 7
               for seat in result.ship.seats.values())
    assert not any(key == "fresh_after_compactions" for _, key, _ in result.edits)
    data = setup_proposal.summary(result.ship, setup_proposal.display_control, existing=result.existing)
    note = "fresh after compactions: no effect, grok does not count them (retained value: 7)"
    assert all(row["fresh_start_note"] == note for row in data["seats"])
    rendered = setup_proposal.render_summary(data)
    assert all(any(row["name"] in line and note in line for line in rendered) for row in data["seats"])


def test_switch_to_counting_harness_adds_default_with_harness_edit(tmp_path, monkeypatch):
    existing_ship(tmp_path, monkeypatch, "grok", None)
    result = proposal(tmp_path, "codex", existing="update", desk="codex", captain="codex", engineer="codex")
    assert all(seat.harness == "codex" and seat.fresh_after_compactions == 2
               for seat in result.ship.seats.values())
    data = setup_proposal.summary(result.ship, setup_proposal.display_control, existing=result.existing)
    assert all(row["fresh_start_note"] == "fresh after compactions: 2 (default added for codex)"
               for row in data["seats"])

    class RecordUpdates(ProposalOperations):
        def __init__(self):
            self.calls = []

        def change_seat(self, current, name, changes):
            self.calls.append((name, list(changes)))
            from quarterdeck.cli import parse_value
            edits = [("seats." + name, key, parse_value(value))
                     for change in changes for key, _, value in [change.partition("=")]]
            return ship.set_values(current, edits)

    operations = RecordUpdates()
    conversation = setup.Conversation({}, interactive=False, write=lambda _: None)
    current, pending = setup.update(result.existing, result.edits, operations, conversation)
    assert {name for name, _ in operations.calls} == set(current.seats)
    assert len(operations.calls) == 3
    assert all("harness=codex" in changes and "fresh_after_compactions=2" in changes
               for _, changes in operations.calls)
    assert all(seat.fresh_after_compactions == 2 for seat in current.seats.values())
    assert len(pending) == 3


def test_project_display_abbreviates_home_without_changing_json(tmp_path, monkeypatch):
    current = proposal(tmp_path, "codex").ship
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path.parent)
    data = setup_proposal.summary(current, setup_proposal.display_control)
    assert data["project_path"] == str(tmp_path)
    assert "Project path:   ~/" + tmp_path.name in setup_proposal.render_summary(data)
