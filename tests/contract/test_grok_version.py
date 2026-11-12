"""Synthetic version-dispatch stubs; no Grok, doctor CLI or provider invocation."""

from pathlib import Path
from types import SimpleNamespace
from functools import wraps
import json
import os
import subprocess
import sys

import pytest

from quarterdeck import check as live_check, doctor, seat as seat_module
from quarterdeck.drill import Check
from quarterdeck.harness import get
from quarterdeck.harness import base, grok
from quarterdeck.harness.grok import GrokHarness
from quarterdeck.ship import Pool, SeatSpec, Session, Ship


@pytest.fixture
def selected(tmp_path, monkeypatch):
    directory = tmp_path / "bin"
    directory.mkdir()
    program = directory / "grok-8.6.4-linux-x86_64"
    program.write_text("Synthetic selected file; never executed.\n")
    program.chmod(0o700)
    (directory / "grok").symlink_to(program.name)
    monkeypatch.setattr(seat_module, "program_path", lambda: str(directory))
    monkeypatch.setattr(doctor, "program_path", lambda: str(directory))
    executions = []
    monkeypatch.setattr(grok, "program_version", lambda argv: executions.append(argv) or "8.6.4")
    return program, executions


def runtime(tmp_path):
    path = tmp_path / "runtime"
    path.mkdir()
    (path / ".git").mkdir()  # synthetic established-runtime fact; no Git invocation
    return path


def assert_context(argv, parent, selected):
    assert argv[:2] == ("env", "-i") and argv[-2:] == (str(selected.resolve()), "--version")
    environment = dict(value.split("=", 1) for value in argv[2:-2])
    home = Path(environment["HOME"])
    assert environment["GROK_HOME"] == str(home) and home.parent == parent
    assert home.is_dir() and not home.is_symlink() and list(home.iterdir()) == []
    assert environment["GROK_DISABLE_AUTOUPDATER"] == "1"
    assert environment["GROK_STORAGE_MODE"] == "local"
    assert all(environment[key] == value for key, value in grok.ENV.items())
    assert environment["PATH"] == os.environ.get("PATH", "")
    return home


def test_context_replaces_both_inherited_homes_and_retires_empty_directory(tmp_path, monkeypatch, selected):
    program, executions = selected
    parent = runtime(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "synthetic-operator-home"))
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "synthetic-inherited-grok-home"))
    homes = []
    def lookup(argv):
        executions.append(argv)
        homes.append(assert_context(argv, parent, program))
        return "8.6.4"
    monkeypatch.setattr(grok, "program_version", lookup)
    assert base._version_in_runtime(GrokHarness(), parent) == "8.6.4"
    assert len(executions) == 1 and not homes[0].exists()
    assert [p.name for p in parent.iterdir()] == [".git"]


def test_contextual_child_receives_only_caller_path_and_controlled_environment(tmp_path, monkeypatch, selected):
    program, executions = selected
    parent = runtime(tmp_path)
    # Observe the initial exec environment, before Python's locale adjustments.
    # The stand-in's caller contains
    # exclusively synthetic values, so even a regression cannot expose a grant.
    caller = {"PATH": os.environ.get("PATH", ""),
              "HOME": str(tmp_path / "synthetic-caller-home"),
              "GROK_HOME": str(tmp_path / "synthetic-caller-grok-home"),
              "GROK_MANAGED_CONFIG_URL": "https://synthetic.invalid/config",
              "GROK_AGENT": "synthetic-agent",
              "SYNTHETIC_FUTURE_REDIRECT": "synthetic-arbitrary-value"}
    for name, value in caller.items():
        monkeypatch.setenv(name, value)
    program.write_text(f"#!{sys.executable} -IBS\n"
        "import json, sys\nfrom pathlib import Path\n"
        "items = Path('/proc/self/environ').read_bytes().split(b'\\0')\n"
        "environment = dict(item.decode(errors='surrogateescape').split('=', 1) for item in items if item)\n"
        "print(json.dumps({'environment': environment, 'argv': sys.argv[1:]}))\n")
    observed = []
    def lookup(argv):
        executions.append(argv)
        home = assert_context(argv, parent, program)
        result = subprocess.run(argv, env=caller, capture_output=True, text=True, timeout=5, check=True)
        child = json.loads(result.stdout)
        assert child["argv"] == ["--version"]
        assert child["environment"] == {**grok.ENV, "PATH": caller["PATH"], "HOME": str(home), "GROK_HOME": str(home)}
        for name in ("GROK_MANAGED_CONFIG_URL", "GROK_AGENT", "SYNTHETIC_FUTURE_REDIRECT"):
            assert name not in child["environment"]
            assert not any(value.startswith(name + "=") for value in argv)
        observed.append(child)
        return "8.6.4"
    monkeypatch.setattr(grok, "program_version", lookup)
    assert base._version_in_runtime(GrokHarness(), parent) == "8.6.4"
    assert len(executions) == len(observed) == 1


def test_no_context_reads_only_the_selected_resolved_basename(selected):
    program, executions = selected
    assert GrokHarness().version() == "8.6.4"
    assert executions == []
    assert program.name == "grok-8.6.4-linux-x86_64"


def test_a_newer_unselected_filename_does_not_redirect_version(selected):
    program, executions = selected
    newer = program.with_name("grok-9.9.9-linux-x86_64")
    newer.write_text("Synthetic unselected file.\n")
    newer.chmod(0o700)
    assert GrokHarness().version() == "8.6.4" and executions == []


def test_unknown_selected_filename_executes_nothing(selected):
    program, executions = selected
    program.rename(program.with_name("unrecognized-program"))
    link = program.parent / "grok"
    link.unlink()
    link.symlink_to("unrecognized-program")
    assert GrokHarness().version() is None and executions == []


def test_missing_runtime_uses_filename_without_creating_context(tmp_path, selected):
    _, executions = selected
    missing = tmp_path / "absent-runtime"
    assert base._version_in_runtime(GrokHarness(), missing) == "8.6.4"
    assert not missing.exists() and executions == []


@pytest.mark.parametrize("name", ["claude", "codex", "fake"])
def test_default_and_synthetic_adapters_keep_no_context_behavior(tmp_path, monkeypatch, name):
    calls = []
    monkeypatch.setattr(base, "program_version", lambda argv: calls.append(argv) or "synthetic-version")
    parent = runtime(tmp_path)
    adapter = get(name)
    actual = base._version_in_runtime(adapter, parent)
    assert actual == ("fake" if name == "fake" else "synthetic-version")
    assert calls == ([] if name == "fake" else [(adapter.program, "--version")])
    assert [p.name for p in parent.iterdir()] == [".git"]


def test_legacy_no_argument_double_is_called_once_without_a_directory(tmp_path):
    calls = []
    adapter = SimpleNamespace(version=lambda: calls.append("no arguments") or "legacy")
    missing = tmp_path / "absent-runtime"
    assert base._version_in_runtime(adapter, missing) == "legacy"
    assert calls == ["no arguments"] and not missing.exists()


def test_accepting_home_without_opt_in_still_gets_one_no_argument_call(tmp_path):
    parent = runtime(tmp_path)
    calls = []
    def version(home=None):
        assert home is None
        assert [path.name for path in parent.iterdir()] == [".git"]
        return "synthetic-default"
    @wraps(version)
    def callback(*args, **kwargs):
        calls.append((args, kwargs))
        return version(*args, **kwargs)
    adapter = SimpleNamespace(version=callback)
    assert base._version_in_runtime(adapter, parent) == "synthetic-default"
    assert calls == [((), {})]
    assert [path.name for path in parent.iterdir()] == [".git"]


def test_context_callback_error_is_not_retried_without_context(tmp_path):
    parent = runtime(tmp_path)
    calls = []
    def version(home=None):
        calls.append(home)
        assert home.parent == parent and list(home.iterdir()) == []
        raise TypeError("synthetic callback error")
    with pytest.raises(TypeError, match="synthetic callback error"):
        base._version_in_runtime(SimpleNamespace(version=version, private_version_home=True), parent)
    assert len(calls) == 1 and not calls[0].exists()


def synthetic_ship(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pool = Pool("synthetic-plan", "xai")
    spec = SeatSpec("synthetic-seat", "synthetic-session", "worker", "grok", (pool.name,), cwd=workspace)
    return Ship(tmp_path / "ship", "synthetic-ship", "operator", workspace,
                {pool.name: pool}, {"synthetic-session": Session("synthetic-session", "synthetic-session")}, {spec.name: spec})


def stub_doctor_dependencies(monkeypatch):
    monkeypatch.setattr(doctor, "program_version", lambda argv: "synthetic-system-version")
    monkeypatch.setattr(doctor, "framework_check", lambda ship: Check("framework", True, "synthetic framework"))
    monkeypatch.setattr(doctor, "pool_check", lambda pool: Check("pool", True, "synthetic pool"))
    monkeypatch.setattr(doctor, "seat_readiness", lambda ship: [])
    monkeypatch.setattr(doctor.watch, "disks", lambda paths: [])
    monkeypatch.setattr(doctor.servicectl, "alive", lambda runtime: False)


def test_doctor_missing_runtime_remains_missing_without_version_execution(tmp_path, monkeypatch, selected):
    _, executions = selected
    ship = synthetic_ship(tmp_path)
    stub_doctor_dependencies(monkeypatch)
    rows = doctor.checks(ship)
    assert any(row.name == "program grok" and "8.6.4" in row.detail for row in rows)
    assert not any(row.name in ("runtime fence", "service") for row in rows)
    assert not ship.runtime.exists() and executions == []


def test_doctor_passes_its_established_runtime_to_version(tmp_path, monkeypatch, selected):
    program, executions = selected
    ship = synthetic_ship(tmp_path)
    ship.runtime.mkdir(parents=True)
    (ship.runtime / ".git").mkdir()
    stub_doctor_dependencies(monkeypatch)
    homes = []
    def lookup(argv):
        executions.append(argv)
        homes.append(assert_context(argv, ship.runtime, program))
        return "8.6.4"
    monkeypatch.setattr(grok, "program_version", lookup)
    assert any(row.name == "program grok" for row in doctor.checks(ship))
    assert len(executions) == 1 and not homes[0].exists()


def test_check_passes_runtime_context_before_any_probe(tmp_path, monkeypatch, selected):
    program, executions = selected
    ship = synthetic_ship(tmp_path)
    ship.runtime.mkdir(parents=True)
    (ship.runtime / ".git").mkdir()
    homes = []
    def lookup(argv):
        executions.append(argv)
        homes.append(assert_context(argv, ship.runtime, program))
        return None  # ordinary installed-version reading ends before a live probe
    monkeypatch.setattr(grok, "program_version", lookup)
    rows = live_check.check(ship, "grok", None)
    assert len(rows) == 1 and not rows[0].passed
    assert len(executions) == 1 and not homes[0].exists()
