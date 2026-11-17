"""An answers file for the office alone makes the office alone, with every office answer applied, and stops; only an
answer that a crew alone reads asks for a crew. All machine/account/live operations are replaced."""
import argparse
import shutil
from pathlib import Path

import pytest

from quarterdeck import setup, setup_answers, ship

from .test_setup_flow import ScriptedOperations, answers  # noqa: F401 -- the fixture, by name


@pytest.fixture
def office_only(answers, tmp_path, monkeypatch):  # noqa: F811
    """The office's own answers, with no answer a crew alone reads; the crew wizard must not be reached."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # a throwaway home: no default is read from the operator's
    monkeypatch.setattr(setup, "run", lambda *_, **__: pytest.fail("no crew wizard for an office-only file"))
    operations = ScriptedOperations()
    monkeypatch.setattr(operations, "up_desk", lambda office: operations.calls.append("office up") or ["office up"])
    return operations, dict(desk="codex", use_defaults="yes", accept="yes", start="yes")


def first_step(answers, operations=None):  # noqa: F811
    lines = []
    conversation = setup.Conversation(answers, interactive=False, write=lines.append)
    return setup.first_step(argparse.Namespace(json=False), operations=operations or ScriptedOperations(),
                            conversation=conversation), lines


def test_the_desks_program_use_defaults_accept_and_start_make_the_office_alone_with_that_desk(office_only, tmp_path):
    operations, answers = office_only  # noqa: F811
    result, _ = first_step(answers, operations)
    assert "Office ready" in result[-1]
    office = ship.load(tmp_path / "office")
    desk = office.seat("desk")
    assert office.kind == "office" and desk.harness == "codex" and desk.pools == ("codex",), "the answer, not claude"
    assert set(office.pools) == {"codex"} and office.pool("codex").provider == "openai"
    assert not (tmp_path / "ships" / "crew").exists() and list((tmp_path / "ships").iterdir()) == []
    assert operations.calls == ["discover", "office up", "access"], "no readiness, check or crew start"


def test_the_operator_and_the_office_title_are_applied(office_only, tmp_path):
    operations, answers = office_only  # noqa: F811
    answers.update(operator="Synthetic Operator", office="Synthetic Office", start="no",
                   seats={"desk": {"name": "Synthetic desk", "model": "synthetic-model", "effort": "high"}})
    result, _ = first_step(answers, operations)
    assert "desk not started" in result[-1]
    office = ship.load(tmp_path / "office")
    desk = office.seat("desk")
    assert office.operator == "Synthetic Operator" and office.session_of(desk).title == "Synthetic Office"
    assert (desk.display, desk.model, desk.effort) == ("Synthetic desk", "synthetic-model", "high")


def test_the_office_written_alone_equals_the_office_a_run_with_a_crew_writes(answers, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    office_answers = dict(desk="codex", operator="Synthetic Operator", office="Synthetic Office", use_defaults="yes",
                          accept="yes", start="no", payment={"codex": "subscription"},
                          subscription_home={"codex": str(tmp_path / "sign-in")},
                          seats={"desk": {"name": "Synthetic desk", "effort": "high"}})
    first_step({**answers, **office_answers, "check": "no"})
    assert ship.load(Path(answers["folder"])).seats.keys() == {"captain", "engineering"}, "the crew came too"
    combined = (tmp_path / "office" / "ship.toml").read_bytes()
    shutil.rmtree(tmp_path / "office")
    shutil.rmtree(answers["folder"])
    monkeypatch.setattr(setup, "run", lambda *_, **__: pytest.fail("no crew wizard for an office-only file"))
    first_step(office_answers)
    assert (tmp_path / "office" / "ship.toml").read_bytes() == combined
    assert not Path(answers["folder"]).exists()


def test_a_pool_answer_for_a_program_the_desk_does_not_run_is_refused_not_dropped(office_only, tmp_path):
    operations, answers = office_only  # noqa: F811
    answers.update(payment={"codex": "subscription", "claude": "subscription"})
    with pytest.raises(setup.ShipError, match=r"answer payment\.claude names a program the office's desk does not run"):
        first_step(answers, operations)
    assert not (tmp_path / "office").exists()


@pytest.mark.parametrize("given", [
    *({name: "yes"} for name in setup_answers.CREW_SCALARS),
    *({"seats": {role: {"name": "Synthetic"}}} for role in setup_answers.CREW_SEATS),
], ids=lambda given: next(iter(given)) if "seats" not in given else "seats." + next(iter(given["seats"])))
def test_an_answer_only_a_crew_reads_runs_both_office_first(answers, given, monkeypatch):  # noqa: F811
    seen = []
    monkeypatch.setattr(setup, "run", lambda *_, **kwargs: seen.append(kwargs) or ["crew wizard"])
    result, _ = first_step({"desk": "codex", "use_defaults": "yes", "accept": "yes", "start": "yes", **given})
    assert result == ["crew wizard"] and seen[0]["initialize_office"] is True, "the office first, in the same run"


@pytest.mark.parametrize("name", setup_answers.OFFICE_SCALARS)
def test_an_answer_the_office_also_reads_never_asks_for_a_crew(name):
    assert not setup_answers.asks_for_crew({name: "yes", "seats": {"desk": {"name": "Synthetic"}},
                                            **{field: {"codex": "x"} for field in setup_answers.MAPS}})


def test_every_answer_counts_for_the_office_or_the_crew_and_the_crew_set_is_derived():
    office, crew = set(setup_answers.OFFICE_SCALARS), set(setup_answers.CREW_SCALARS)
    assert office <= set(setup_answers.SCALARS), "an office answer the resolver does not know"
    assert office | crew == set(setup_answers.SCALARS) and not office & crew
    assert set(setup_answers.OFFICE_SEATS) | set(setup_answers.CREW_SEATS) == set(setup_answers.ROLES)
    assert {"captain", "engineer", "workspace", "name", "folder", "existing", "check", "programs",
            "wait_for_sign_in"} == crew


def test_the_office_alone_reads_only_answers_counted_for_the_office(office_only, monkeypatch):
    """Recorded from the code as it runs: an answer the office-only path reads that the crew set holds would make an
    office-only file run the crew wizard, so this fails until that answer is counted for the office."""
    operations, answers = office_only  # noqa: F811
    read = set()
    for method in ("supplied", "value", "ask", "choice"):
        original = getattr(setup.Conversation, method)
        def recording(self, field, *rest, _original=original, **options):
            read.add(field)
            return _original(self, field, *rest, **options)
        monkeypatch.setattr(setup.Conversation, method, recording)
    original_control = setup.Conversation.control
    def control(self, role, field, *rest):
        read.add(f"seats.{role}.{field}")
        return original_control(self, role, field, *rest)
    monkeypatch.setattr(setup.Conversation, "control", control)
    answers.update(operator="Synthetic Operator", office="Synthetic Office")
    first_step(answers, operations)
    tops = {field.split(".")[0] if not field.startswith("seats.") else ".".join(field.split(".")[:2]) for field in read}
    counted = (set(setup_answers.OFFICE_SCALARS) | set(setup_answers.MAPS)
               | {f"seats.{role}" for role in setup_answers.OFFICE_SEATS})
    assert tops <= counted, tops - counted
    assert {"accept", "start", "operator", "office", "desk", "seats.desk", "payment"} <= tops
