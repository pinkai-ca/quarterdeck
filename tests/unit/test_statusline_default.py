"""Ordinary synthetic display/home/packaging checks; no native sessions or providers."""

from datetime import datetime, timedelta, timezone
import fcntl
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
import venv

import pytest

pytest_plugins = ("tests.shared_install",)

from quarterdeck.harness import claude_statusline as line
from quarterdeck import resources
from quarterdeck.harness.claude import ClaudeHarness
from quarterdeck.ship import Pool, SeatSpec


def window(pool, used, **extra):
    return dict(pool=pool, window="sample interval", used=used, complete=used is not None,
                resets_at=(datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
                current=True, blocks=True, wall=False, **extra)


def cache(windows):
    return dict(windows=windows, ledger_at=time.time(), launched_pools=sorted({w['pool'] for w in windows}))


def plain(text):
    return line.ANSI.sub("", text)


def test_native_callback_with_optional_prompt_cache_and_cost_uses_context_and_effort():
    callback = {
        'model': {'display_name': 'Haiku 5.5'}, 'effort': {'level': 'medium'},
        'context_window': {'used_percentage': 4},
        'prompt_cache': {'warm': True, 'requests': 2, 'misses': 1, 'hit_ratio': 0.5},
        'cost': {'total_cost_usd': 0.125, 'total_duration_ms': 200, 'total_api_duration_ms': 125,
                 'total_lines_added': 0, 'total_lines_removed': 0},
    }
    assert plain(line.identity({}, 'sample', callback, 2)) == 'Sample · Haiku 5.5 · medium · context 4%'
    del callback['prompt_cache']
    assert plain(line.identity({}, 'sample', callback, 2)).endswith('Haiku 5.5 · medium · context 4%')


def test_declared_subscriptions_use_generic_ledger_windows():
    providers = ("anthropic", "openai", "google", "xai", "new-provider")
    pools = {f"sample-{n}": {"provider": provider} for n, provider in enumerate(providers)}
    pools.update(key={"provider": "openai", "api_key": "keys:synthetic-key"},
                 local={"provider": "synthetic", "endpoint": "http://example.invalid"})
    windows = [window(name, (n + 1) / 10) for n, name in enumerate(list(pools)[:5])]
    rows = line.allowances(cache(windows), {"pools": pools}, "sample-1")
    assert [r["pool"] for r in rows] == list(pools)[:5]
    assert [r["used"] for r in rows] == [10, 20, 30, 40, 50]
    text = plain("\n".join(line.draw(500, {}, "sample-seat", {}, rows)))
    for name in list(pools)[:5]:
        assert line.label(name) in text and name not in text
    assert "● 1" in text and "sample interval" not in text
    assert all(" · 1h" in plain(line.cell(row, 3)) and "reset " not in plain(line.cell(row, 3)) for row in rows)


def test_incomplete_window_is_unknown_and_unread_pool_is_omitted():
    ship = {"pools": {"sample-unread": {}, "sample-missing": {}}}
    rows = line.allowances(cache([window("sample-unread", None)]), ship, None)
    assert all(r["used"] is None for r in rows)
    assert [r['pool'] for r in rows] == ['sample-unread']
    text = plain("\n".join(line.draw(80, {}, "sample-seat", {}, rows)))
    assert text.count("unknown") == 1 and "Missing" not in text and "0%" not in text


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), -0.1, 1.1, "0.0"])
def test_invalid_fraction_is_unknown(value):
    rows = line.allowances(cache([window("sample", value)]), {"pools": {"sample": {}}}, None)
    assert rows[0]["used"] is None


def test_zero_remains_measured_but_other_unknown_windows_stay_visible():
    rows = line.allowances(cache([window("sample", 0), window("sample", None)]),
                           {"pools": {"sample": {}}}, None)
    text = plain(line.cell(rows[0], 3))
    assert "0%" in text and "+ unknown" in text


def test_blocking_window_and_provider_wall_lead_without_name_guessing():
    windows = [window("sample", .9, group="not a model family"), window("sample", .3), window("sample", .99)]
    windows[0].update(blocks=False, window="summary allowance")
    windows[1].update(wall=True, window="native bucket")
    rows = line.allowances(cache(windows), {"pools": {"sample": {}}}, None)
    assert rows[0]["used"] == 30 and rows[0]["span"] == "native bucket"
    assert "back in " in plain(line.cell(rows[0], 3))


def test_stale_reading_is_named_and_expired_reset_is_not_a_new_zero():
    w = window("sample", .6)
    w.update(resets_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
    row = line.allowances(cache([w]), {"pools": {"sample": {}}}, None)[0]
    text = plain(line.cell(row, 3))
    assert "60%?" in text and "reset passed" in text


def test_narrow_screen_keeps_every_attempted_subscription():
    ship = {"pools": {f"sample-{n}": {} for n in range(10)}}
    rows = line.allowances(cache([window(name, None) for name in ship['pools']]), ship, None)
    text = plain("\n".join(line.draw(25, {}, "sample-seat", {}, rows)))
    assert all(line.label(name) + " unknown" in text for name in ship["pools"])


def test_identity_uses_session_facts():
    session = dict(model={"display_name": "Sample Model"}, effort={"level": "high"},
                   context_window={"used_percentage": 31})
    text = plain(line.identity({"persona": "Sample", "role": "builder"}, "sample-seat", session, 2))
    assert all(word in text for word in ("Sample", "builder", "Sample Model", "high", "context 31%"))


def test_missing_ship_prints_unknown_without_creating_cache(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("QD_SHIP", str(tmp_path / "missing"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "outside"))
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    monkeypatch.setattr(sys, "argv", ["statusline.py"])
    line.main()
    text = plain(capsys.readouterr().out)
    assert "ship reading unavailable" in text and "0%" not in text
    assert not list(tmp_path.iterdir())


def test_display_only_writes_inside_ship_runtime_and_never_waits_for_qd(tmp_path, monkeypatch, capsys):
    # law: one-folder
    ship = tmp_path / "sample ship"
    ship.mkdir()
    (ship / "ship.toml").write_text('[ship]\nname="sample"\n[pools.sample]\nprovider="synthetic"\n')
    monkeypatch.setenv("QD_SHIP", str(ship))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "outside"))
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"terminal_width":50}'))
    monkeypatch.setattr(sys, "argv", ["statusline.py"])
    monkeypatch.setattr(line, "qd_reading", lambda *a: pytest.fail("foreground qd reading"))
    renewals = []
    monkeypatch.setattr(line, "renew_in_background", renewals.append)
    line.main()
    text = plain(capsys.readouterr().out)
    assert "ship ledger unavailable" in text and "Sample" not in text
    assert renewals == [ship / "runtime/statusline/readings.json"]
    assert not (tmp_path / "outside").exists()
    assert sorted(p.name for p in ship.iterdir()) == ["runtime", "ship.toml"]


def test_refresh_keeps_only_display_fields_and_retains_failed_reading(tmp_path, monkeypatch):
    path = tmp_path / "readings.json"
    w = window("sample", .4)
    ledger = dict(available=True, pools=[dict(name="sample", kind="subscription")],
                  windows=[dict(pool="sample", current=True, complete=True, wall=False, reading=w)],
                  turns=["should not be cached"])
    answers = iter([ledger, dict(seats=[dict(name="sample-seat", pool="sample", record="not cached")])])
    monkeypatch.setattr(line, "qd_reading", lambda *a: next(answers))
    line.refresh(tmp_path, path)
    first = line.read_json(path)
    assert first["windows"][0]["used"] == .4 and first["seats"] == {"sample-seat": "sample"}
    assert "should not be cached" not in path.read_text() and "not cached" not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    monkeypatch.setattr(line, "qd_reading", lambda *a: None)
    line.refresh(tmp_path, path)
    later = line.read_json(path)
    assert later["windows"] == first["windows"] and later["ledger_at"] == first["ledger_at"]
    assert later["ledger_unavailable"] is True
    row = line.allowances(later, {"pools": {"sample": {}}, 'seats': {'sample-seat': {'pool': 'sample'}}}, None)[0]
    assert "40%?" in plain(line.cell(row, 3))


def test_lock_releases_after_reader_exit_without_deleting_old_lockfile(tmp_path, monkeypatch):
    path = tmp_path / "readings.json"
    lock = path.with_suffix(".lock")
    calls = []
    monkeypatch.setattr(line, "qd_reading", lambda *a: calls.append(a))
    with lock.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        line.refresh(tmp_path, path)
        assert not calls
    line.refresh(tmp_path, path)
    assert calls and lock.exists() and path.exists()


def test_failed_readings_are_throttled_and_status_unknown(tmp_path, monkeypatch, capsys):
    (tmp_path / "ship.toml").write_text('[ship]\nname="sample"\n')
    folder = tmp_path / "runtime/statusline"
    folder.mkdir(parents=True)
    path = folder / "readings.json"
    monkeypatch.setattr(line, "qd_reading", lambda *a: None)
    line.refresh(tmp_path, path)
    monkeypatch.setenv("QD_SHIP", str(tmp_path))
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    monkeypatch.setattr(sys, "argv", ["statusline.py"])
    monkeypatch.setattr(line, "renew_in_background", lambda *a: pytest.fail("unthrottled retry"))
    line.main()
    text = plain(capsys.readouterr().out)
    assert "ship ledger unavailable" in text and "seat subscription unknown" in text


def test_claude_default_home_command_and_ship_override(tmp_path):
    seat = SeatSpec("sample-seat", "sample-session", "sample-role", "claude", ("sample",))
    pool = Pool("sample", "anthropic")
    home = tmp_path / "home"
    ClaudeHarness().build_home(home, seat, pool, None)
    settings = json.loads((home / "settings.json").read_text())
    command = shlex.split(settings["statusLine"]["command"])
    assert command[:3] == [sys.executable, "-I", "-S"]
    assert Path(command[3]).is_file()
    assert Path(command[3]).name == "claude_statusline.py"
    assert Path(command[3]).parent.name == "harness"
    env = {**os.environ, "QD_SHIP": str(tmp_path / "missing")}
    done = subprocess.run(command, input="{}", text=True, capture_output=True, env=env, timeout=10)
    assert done.returncode == 0 and "ship reading unavailable" in done.stdout
    template = tmp_path / "template"
    template.mkdir()
    (template / "settings.json").write_text(json.dumps({"statusLine": {"command": "sample custom footer"}}))
    ClaudeHarness().build_home(home, seat, pool, template)
    assert json.loads((home / "settings.json").read_text())["statusLine"]["command"] == "sample custom footer"


def test_plain_wheel_install_carries_runnable_default_footer(tmp_path, shared_wheel_install):
    built = shared_wheel_install.wheel.proof
    assert built.returncode == 0, built.stdout + built.stderr
    installed = shared_wheel_install.prefix
    python = installed / 'bin/python'
    install = shared_wheel_install.proof
    assert install.returncode == 0, install.stdout + install.stderr
    proof = '''import json, shlex, sys
from pathlib import Path
from quarterdeck.harness.claude import ClaudeHarness
from quarterdeck.ship import Pool, SeatSpec
home = Path(sys.argv[1])
ClaudeHarness().build_home(home, SeatSpec("sample", "sample", "sample", "claude", ("sample",)), Pool("sample", "anthropic"), None)
print(json.dumps(shlex.split(json.loads((home / "settings.json").read_text())["statusLine"]["command"])))
'''
    built_home = subprocess.run([str(python), "-I", "-c", proof, str(tmp_path / "home")],
                                capture_output=True, text=True, timeout=10)
    assert built_home.returncode == 0, built_home.stderr
    command = json.loads(built_home.stdout)
    assert command[0] == str(python)
    assert Path(command[3]).is_relative_to(installed)
    assert Path(command[3]).name == "claude_statusline.py"
    assert Path(command[3]).parent.name == "harness"
    assert (installed / "bin/qd").is_file()
    done = subprocess.run(command, input="{}", capture_output=True, text=True,
                          env={**os.environ, "QD_SHIP": str(tmp_path / "missing")}, timeout=10)
    assert done.returncode == 0 and "ship reading unavailable" in done.stdout
