"""qd doctor's checks on the drydock: what passes on a machine that runs the tests, and what a failure names."""

from __future__ import annotations

import sys

from pathlib import Path

import pytest

from quarterdeck import cli, doctor
from quarterdeck.ship import Pool, ShipError, load


pytestmark = pytest.mark.usefixtures('synthetic_disk_room')


def run(capsys, *argv: str) -> tuple[int, str]:
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out


def test_disk_pressure_is_still_a_named_failure(drydock: Path, monkeypatch):
    monkeypatch.setattr(doctor.watch, 'disks', lambda paths: [('synthetic-root', 95)])
    check = next(c for c in doctor.checks(load(drydock)) if c.name == 'disk')
    assert not check.passed and check.detail == 'nearly full: synthetic-root 95%'


def test_the_drydock_passes_every_check_on_a_machine_that_runs_the_tests(drydock: Path, capsys):
    found = doctor.checks(load(drydock))
    assert all(c.passed for c in found), [str(c) for c in found if not c.passed]
    assert [c.name for c in found][:3] == ["ship file", "tmux", "git"]
    assert next(c.detail for c in found if c.name == "program fake").startswith(sys.executable)
    assert next(c.detail for c in found if c.name == "pool fake").endswith("the program holds its own sign-in")
    assert "service" not in [c.name for c in found], "no runtime yet, so nothing to say about its service"
    code, out = run(capsys, "--ship", str(drydock), "doctor")
    assert code == 0 and out.startswith("PASS  ship file · ship.toml: 3 seats, 1 pools, 3 sessions") and "FAIL" not in out


def test_a_missing_working_directory_and_a_missing_program_fail_by_name(drydock: Path, tmp_path: Path, monkeypatch, capsys):
    nowhere = tmp_path / "nowhere"
    text = (drydock / "ship.toml").read_text().replace("[seats.captain]\n", f'[seats.captain]\ncwd = "{nowhere}"\n')
    (drydock / "ship.toml").write_text(text)
    failed = [c for c in doctor.checks(load(drydock)) if not c.passed]
    assert [c.name for c in failed] == ["seat captain"] and "does not exist; qd creates nothing outside its folder" in failed[0].detail
    code, out = run(capsys, "--ship", str(drydock), "doctor")
    assert code == 1 and f"FAIL  seat captain · working directory {nowhere} does not exist" in out
    monkeypatch.setattr(doctor.shutil, "which", lambda *args, **kwargs: None)
    failed = {c.name: c.detail for c in doctor.checks(load(drydock)) if not c.passed}
    assert failed["program fake"] == f"{sys.executable} is not on the PATH qd runs with; install it, or put it on that PATH"


def test_a_pool_is_ready_when_its_credential_is_there(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("SYNTHETIC_KEY", raising=False)
    absent = doctor.pool_check(Pool("claude-secondary", "claude", config_dir=tmp_path / "nowhere"))
    assert not absent.passed and absent.detail.startswith("no credential at") and "sign in to the program there" in absent.detail
    key = doctor.pool_check(Pool("api", "claude", api_key="env:SYNTHETIC_KEY"))
    assert not key.passed and "SYNTHETIC_KEY is not set" in key.detail
    monkeypatch.setenv("SYNTHETIC_KEY", "x")
    assert doctor.pool_check(Pool("api", "claude", api_key="env:SYNTHETIC_KEY")).detail == "key present in env:SYNTHETIC_KEY"
    signed = tmp_path / "signed"
    signed.mkdir()
    (signed / ".credentials.json").write_text('{"claudeAiOauth": {"refreshTokenExpiresAt": 4102444800000}}')
    present = doctor.pool_check(Pool("claude-secondary", "claude", config_dir=signed))
    assert present.passed and present.detail.startswith(f"signed in at {signed}; its grant expires ")
    assert doctor.pool_check(Pool("google", "antigravity")).detail == "the program holds its own sign-in"


def test_a_ship_file_that_does_not_load_is_the_first_failing_check(tmp_path: Path, capsys):
    code, out = run(capsys, "--ship", str(tmp_path), "doctor")
    assert code == 1 and out.startswith("FAIL  ship file · ") and "holds no ship.toml" in out


def test_an_endpoint_pool_is_ready_when_it_answers_and_names_the_server_when_it_does_not(monkeypatch):
    monkeypatch.setattr(doctor.endpoint, "models", lambda url, timeout=0, **kwargs: [doctor.endpoint.Served("a", 4096)])
    ready = doctor.pool_check(Pool("local", "local", endpoint="http://127.0.0.1:8000/v1"))
    assert ready.passed and ready.detail == "http://127.0.0.1:8000/v1 answers; serves a (4,096 tokens)"

    def down(url, timeout=0, **kwargs):
        raise ShipError(f"{url} does not answer: connection refused")

    monkeypatch.setattr(doctor.endpoint, "models", down)
    check = doctor.pool_check(Pool("local", "local", endpoint="http://127.0.0.1:1/v1"))
    assert not check.passed
    assert check.detail == "http://127.0.0.1:1/v1 does not answer: connection refused; start the server, or point the pool at one that answers"


def test_a_jailed_seat_adds_a_bubblewrap_check(drydock: Path, monkeypatch):
    from quarterdeck import ship as shipmod

    (drydock / "ship.toml").write_text(shipmod.edit((drydock / "ship.toml").read_text(), "seats.worker-1", "sandbox", True))
    ship = shipmod.load(drydock)
    monkeypatch.setattr(doctor, "program_version", lambda argv: "0.11.1" if argv[0] == "bwrap" else "1.0")
    check = next(c for c in doctor.checks(ship) if c.name == "sandbox")
    assert check.passed and check.detail == "bubblewrap 0.11.1: jailed seats run in it"
    monkeypatch.setattr(doctor, "program_version", lambda argv: None if argv[0] == "bwrap" else "1.0")
    check = next(c for c in doctor.checks(ship) if c.name == "sandbox")
    assert not check.passed and "install bubblewrap, or set sandbox = false" in check.detail
