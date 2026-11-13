"""Independent ordinary sync gates; installer orchestration is outside this file.

All identifiers and installation metadata below are synthetic, with nonhex names.
These tests exercise public wording and delegation, never a real installation.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from quarterdeck import cli, doctor, install
from quarterdeck.ship import ShipError


ALIAS_NOTE = " · `upgrade` is now `qd sync`"


@pytest.mark.parametrize("verb", ["sync", "upgrade"])
def test_both_verbs_default_to_head_and_share_handler(verb, monkeypatch):
    args = cli.build_parser().parse_args([verb])

    assert args.run is cli.cmd_upgrade
    assert args.ref is None  # own-copy parser distinguishes omitted refs from --back targets
    assert args.upgrade_alias is (verb == "upgrade")
    synthetic_ship, synthetic_tmux = object(), object()
    monkeypatch.setattr(cli, "load_ship", Mock(return_value=synthetic_ship))
    monkeypatch.setattr(cli.Tmux, "default", Mock(return_value=synthetic_tmux))
    api = Mock(return_value=["synthetic final proof"])
    monkeypatch.setattr(install, "upgrade", api)

    assert args.run(args) == ["synthetic final proof" + (ALIAS_NOTE if verb == "upgrade" else "")]
    api.assert_called_once_with(synthetic_ship, "HEAD", synthetic_tmux, back=False, force=False)


@pytest.mark.parametrize("verb", ["sync", "upgrade"])
@pytest.mark.parametrize("ref", ["HEAD~2", "synthetic-forward-ref"])
def test_parser_preserves_backward_and_forward_local_refs(verb, ref):
    args = cli.build_parser().parse_args([verb, ref])

    assert args.run is cli.cmd_upgrade
    assert args.ref == ref


@pytest.mark.parametrize("verb", ["sync", "upgrade"])
def test_command_help_describes_local_bidirectional_sync(verb, capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.build_parser().parse_args([verb, "--help"])

    assert stopped.value.code == 0
    captured = capsys.readouterr()
    help_text = " ".join(captured.out.split())
    assert captured.err == ""
    assert f"usage: qd {verb}" in help_text
    assert "local commit" in help_text
    assert "fetches nothing" in help_text
    assert "forward or back" in help_text
    assert "default: the checkout's HEAD" in help_text
    assert ("alias for qd sync" in help_text) is (verb == "upgrade")


@pytest.mark.parametrize("verb", ["sync", "upgrade"])
@pytest.mark.parametrize("ref", ["HEAD", "HEAD~2", "synthetic-forward-ref"])
def test_handler_calls_existing_install_api_and_only_alias_marks_final_proof(
    verb, ref, monkeypatch
):
    args = cli.build_parser().parse_args([verb, ref])
    synthetic_ship = object()
    synthetic_tmux = object()
    load = Mock(return_value=synthetic_ship)
    tmux = Mock(return_value=synthetic_tmux)
    original = ["synthetic installation proof", "synthetic final proof"]
    api = Mock(return_value=original.copy())
    monkeypatch.setattr(cli, "load_ship", load)
    monkeypatch.setattr(cli.Tmux, "default", tmux)
    monkeypatch.setattr(install, "upgrade", api)

    result = args.run(args)

    load.assert_called_once_with(args)
    tmux.assert_called_once_with()
    api.assert_called_once_with(synthetic_ship, ref, synthetic_tmux, back=False, force=False)
    assert result == [original[0], original[1] + (ALIAS_NOTE if verb == "upgrade" else "")]
    assert sum(line.count(ALIAS_NOTE) for line in result) == (verb == "upgrade")


def test_source_repository_refusal_names_sync(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "__file__", str(tmp_path / "code" / "src" / "quarterdeck" / "install.py"))
    synthetic_ship = SimpleNamespace(folder=tmp_path / "ships" / "synthetic-ship")

    with pytest.raises(ShipError) as refused:
        install.source_repo(synthetic_ship)

    assert str(refused.value) == (
        "no unambiguous local framework checkout; run qd sync from the checkout"
    )


def test_doctor_installed_copy_recommends_sync(tmp_path, monkeypatch):
    synthetic_ship = SimpleNamespace(code=None, folder=tmp_path / "synthetic-ship",
                                     runtime=tmp_path / "synthetic-runtime")
    metadata = {"commit": "SYNTH-current", "at": "2001-02-03T04:05:06+00:00",
                "safety": install.COPY_SAFETY}
    read = Mock(return_value=doctor.copy_state.Selection(
        qd=synthetic_ship.runtime / "venv/bin/qd", fact=metadata))
    monkeypatch.setattr(doctor.copy_state, "select", read)
    monkeypatch.setattr(doctor.servicectl, "service_identity", Mock(return_value=None))
    monkeypatch.setattr(doctor.servicectl, "alive", Mock(return_value=False))

    check = doctor.framework_check(synthetic_ship)

    read.assert_called_once_with(synthetic_ship.folder, mode=None, mode_known=True)
    assert check.passed
    assert check.name == "framework"
    assert check.detail == (
        "selected copy SYNTH-c since 2001-02-03T04:05 · "
        "no proven previous copy; a successful sync creates one · service down; no running commit observed"
    )


@pytest.mark.parametrize("proof_command", [None, "laws"])
def test_doctor_failed_proof_names_sync_without_success(tmp_path, monkeypatch, proof_command):
    synthetic_ship = SimpleNamespace(code=None, folder=tmp_path / "synthetic-ship",
                                     runtime=tmp_path / "synthetic-runtime")
    failure = {"phase": "proving", "reason": "copy operation did not complete"}
    if proof_command is not None:
        failure.update(reason="laws proof did not complete", step=proof_command)
    warning = "installed copy proof failed; use qd up, qd sync or qd doctor"
    read = Mock(return_value=doctor.copy_state.Selection(
        state={"last_failure": failure}, warning=warning))
    monkeypatch.setattr(doctor.copy_state, "select", read)
    monkeypatch.setattr(doctor.servicectl, "service_identity", Mock(return_value=None))
    monkeypatch.setattr(doctor.servicectl, "alive", Mock(return_value=False))

    check = doctor.framework_check(synthetic_ship)

    read.assert_called_once_with(synthetic_ship.folder, mode=None, mode_known=True)
    assert not check.passed
    assert check.detail == (
        f"current installed commit unknown · {warning} · "
        "no proven previous copy; a successful sync creates one · last copy failure: proving · "
        + ("laws proof did not complete at laws" if proof_command else "copy operation did not complete")
        + " · service down; no running commit observed"
    )


def test_doctor_checkout_reports_declared_checkout(tmp_path, monkeypatch):
    synthetic_ship = SimpleNamespace(code="checkout", runtime=tmp_path / "synthetic-runtime")
    synthetic_repo = tmp_path / "synthetic-repo"
    read = Mock()
    monkeypatch.setattr(doctor.copy_state, "select", read)
    source = Mock(return_value=synthetic_repo)
    git = Mock(return_value=b"SYNTH-checkout\n")
    monkeypatch.setattr(install, "source_repo", source)
    monkeypatch.setattr(install, "git", git)

    check = doctor.framework_check(synthetic_ship)

    source.assert_called_once_with(synthetic_ship)
    git.assert_called_once_with(synthetic_repo, "rev-parse", "--short", "HEAD")
    read.assert_not_called()
    assert check.passed
    assert check.detail == "follows the checkout at SYNTH-checkout"


def test_doctor_without_source_repository_reports_unknown_checkout(tmp_path, monkeypatch):
    synthetic_ship = SimpleNamespace(
        code="checkout", folder=tmp_path / "ships" / "synthetic-ship", runtime=tmp_path / "synthetic-runtime"
    )
    read = Mock()
    monkeypatch.setattr(doctor.copy_state, "select", read)
    monkeypatch.setattr(install, "__file__", str(tmp_path / "code" / "src" / "quarterdeck" / "install.py"))

    check = doctor.framework_check(synthetic_ship)

    read.assert_not_called()
    assert not check.passed
    assert check.detail == "follows the checkout; checkout commit unknown"
