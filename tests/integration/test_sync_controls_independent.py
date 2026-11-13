"""CLI dispatch/output integration with a synthetic installer API boundary.

No installation, service, provider, record or resource machinery is exercised.
Forward/back coverage means exact ref forwarding, not a proved code transition.
"""

import json
from unittest.mock import Mock

import pytest

from quarterdeck import cli, install
from quarterdeck.ship import ShipError


ALIAS_NOTE = " · `upgrade` is now `qd sync`"


@pytest.mark.parametrize("verb", ["sync", "upgrade"])
@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("ship_mode", ["shipless", "explicit", "environment"])
def test_main_sync_dispatches_without_installed_copy_handoff(
    verb, json_output, ship_mode, tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv("QD_SHIP", raising=False)
    monkeypatch.delenv("QD_SEAT", raising=False)
    folder = tmp_path / "synthetic-ship"
    synthetic_ship = object()
    synthetic_tmux = object()
    find = Mock(return_value=folder)
    load = Mock(return_value=synthetic_ship)
    handoff = Mock()
    api = Mock(return_value=["synthetic first proof", "synthetic final proof"])
    monkeypatch.setattr(cli.shipmod, "find", find)
    monkeypatch.setattr(cli.shipmod, "load", load)
    monkeypatch.setattr(cli.Tmux, "for_ship", Mock(return_value=synthetic_tmux))
    monkeypatch.setattr(install, "handoff", handoff)
    monkeypatch.setattr(install, "upgrade", api)
    argv = ["--json"] if json_output else []
    if ship_mode == "explicit":
        argv += ["--ship", str(folder)]
    elif ship_mode == "environment":
        monkeypatch.setenv("QD_SHIP", str(folder))
    # Each mode supplies a distinct ref case for both spellings and output forms.
    ref = {"shipless": "HEAD", "explicit": "HEAD~2", "environment": "synthetic-forward-ref"}[ship_mode]
    argv += [verb] + ([] if ship_mode == "shipless" else [ref])

    assert cli.main(argv) == 0

    handoff.assert_not_called()
    find.assert_called_once_with(str(folder) if ship_mode == "explicit" else None)
    load.assert_called_once_with(folder)
    api.assert_called_once_with(synthetic_ship, ref, synthetic_tmux, back=False, force=False)
    captured = capsys.readouterr()
    expected = ["synthetic first proof", "synthetic final proof" + (ALIAS_NOTE if verb == "upgrade" else "")]
    assert captured.err == ""
    if json_output:
        assert json.loads(captured.out) == expected
    else:
        assert captured.out == "\n".join(expected) + "\n"


@pytest.mark.parametrize("verb", ["sync", "upgrade"])
@pytest.mark.parametrize("json_output", [False, True])
def test_main_preserves_install_refusal_exit_without_alias_success(
    verb, json_output, monkeypatch, capsys
):
    synthetic_ship = object()
    synthetic_tmux = object()
    monkeypatch.setattr(cli, "load_ship", Mock(return_value=synthetic_ship))
    monkeypatch.setattr(cli.Tmux, "for_ship", Mock(return_value=synthetic_tmux))
    api = Mock(side_effect=ShipError("synthetic refusal; run qd sync from the checkout"))
    monkeypatch.setattr(install, "upgrade", api)
    argv = (["--json"] if json_output else []) + [verb, "synthetic-target-ref"]

    assert cli.main(argv) == 2

    api.assert_called_once_with(synthetic_ship, "synthetic-target-ref", synthetic_tmux, back=False, force=False)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "qd: synthetic refusal; run qd sync from the checkout\n"
    assert ALIAS_NOTE not in captured.err


@pytest.mark.parametrize("ship_mode", ["explicit", "environment"])
def test_other_commands_still_take_installed_handoff_path(
    ship_mode, tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv("QD_SHIP", raising=False)
    folder = tmp_path / "synthetic-ship"
    monkeypatch.setattr(cli.shipmod, "find", Mock(return_value=folder))
    handoff = Mock()
    monkeypatch.setattr(install, "handoff", handoff)
    monkeypatch.setattr(cli, "cmd_status", Mock(return_value=["synthetic status"]))
    if ship_mode == "explicit":
        argv = ["--ship", str(folder), "status"]
    else:
        monkeypatch.setenv("QD_SHIP", str(folder))
        argv = ["status"]

    assert cli.main(argv) == 0

    handoff.assert_called_once_with(folder, argv)
    assert capsys.readouterr().out == "synthetic status\n"


def test_top_level_help_lists_canonical_sync_and_temporary_alias(capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main(["--help"])

    assert stopped.value.code == 0
    text = " ".join(capsys.readouterr().out.split())
    assert "sync install the framework at a local commit" in text
    assert "upgrade alias for qd sync:" in text
