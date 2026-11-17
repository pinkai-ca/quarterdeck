"""The daily live test, with a fake clock and stand-in checks. No program is started."""

import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from quarterdeck import cli, daily_live, post
from quarterdeck.ship import ShipError, load

ZONE = timezone(timedelta(hours=-7))
NOW = datetime(2026, 11, 14, 23, 0, tzinfo=ZONE)
TODAY = datetime(2026, 11, 15, 6, 30, tzinfo=timezone.utc).isoformat()  # 23:30 the same civil day
YESTERDAY = datetime(2026, 11, 14, 6, 30, tzinfo=timezone.utc).isoformat()

SHIP = """\
[ship]
name = "daily"
operator = "operator"
code = "checkout"

[pools.fake]
provider = "synthetic"

[pools.check-claude]
provider = "anthropic"

[pools.check-codex]
provider = "openai"

[pools.check-grok]
provider = "xai"

[sessions.office]
title = "Office"
human = true

[seats.desk]
session = "office"
role = "desk"
harness = "fake"
pool = "fake"
model = "fake-1"
"""


def reading(name, *, working=False, ended=None):
    return {"harness": name, "working_now": working, "last_turn_ended": ended}


def ship(folder: Path):
    folder.mkdir()
    (folder / "ship.toml").write_text(SHIP)
    return load(folder)


@pytest.fixture(autouse=True)
def private_daily_slots(tmp_path, monkeypatch):
    """Stand-in checks use private locks, never the running suite's real slots."""
    from quarterdeck import check_capacity
    root = tmp_path / 'private-test-slots'
    root.mkdir(mode=0o700)
    for name in check_capacity.NAMES:
        (root / name).touch(mode=0o600)
    monkeypatch.setattr(check_capacity, 'folder', lambda origin: root)


def test_a_program_used_on_the_clocks_civil_day_is_not_checked():
    choices = daily_live.plan(
        [reading("claude", ended=TODAY), reading("codex", working=True, ended=YESTERDAY),
         reading("grok", ended=YESTERDAY), reading("pi", working=None, ended=YESTERDAY)],
        history_error=None, day=NOW)
    by_name = {choice.program: choice for choice in choices}
    assert by_name["claude"].action == "skip" and by_name["claude"].use == "used"
    assert by_name["codex"].action == "skip" and by_name["codex"].use == "used"
    assert by_name["grok"].action == "check" and by_name["grok"].use == "unused"
    assert by_name["pi"].action == "check" and by_name["pi"].use == "unknown"


def test_unknown_history_is_checked_and_muse_is_skipped_by_name(tmp_path):
    calls = []

    def runner(origin, program, root, qd):
        calls.append(program)
        code = 1 if program == "grok" else 0
        output = "synthetic failure" if code else "stood in"
        return daily_live.CheckRun(program, code, output)

    origin = ship(tmp_path / "daily")
    message, failed = daily_live.run(
        origin, now=NOW, history_error="program history unavailable",
        readings=[reading("claude", working=False, ended=TODAY), reading("grok", working=False, ended=YESTERDAY),
                  reading("Muse", working=True, ended=TODAY), reading("fake", working=False)],
        runner=runner, scratch=tmp_path / "scratch")
    assert calls == ["claude", "grok"]
    assert failed is True
    assert message.kind == "report" and message.sender == post.SHIP and message.to == "desk"
    assert message.subject == "Daily live test"
    assert message.body.startswith("Daily live test for 2026-11-14.\n")
    assert "Readings: program history unavailable" in message.body
    assert daily_live.MUSE_LINE in message.body
    assert "claude: use unknown; check passed" in message.body
    assert "grok: use unknown; check failed (exit 1)" in message.body
    assert "synthetic failure" in message.body
    assert "stood in" not in message.body
    assert "fake: not a program a live check runs; not checked" in message.body
    stored = post.Store(origin.runtime).all()
    assert [item.id for item in stored] == [message.id]


def test_zone_is_read_from_the_machines_localtime_link(monkeypatch):
    def readlink(path):
        if path == "/etc/localtime":
            return "/usr/share/zoneinfo/Pacific/Honolulu"
        raise OSError(path)

    monkeypatch.setattr(os, "readlink", readlink)
    assert daily_live.machine_zone().key == "Pacific/Honolulu"
    monkeypatch.setattr(os, "readlink", lambda path: (_ for _ in ()).throw(OSError("not a link")))
    assert daily_live.machine_zone() is not None
    source = Path(daily_live.__file__).read_text()
    assert "Vancouver" not in source


def test_no_desk_is_refused(tmp_path):
    folder = tmp_path / "daily"
    folder.mkdir()
    (folder / "ship.toml").write_text(SHIP.replace('role = "desk"', 'role = "worker"'))
    try:
        daily_live.run(load(folder), now=NOW, readings=[], runner=lambda *args: None)
    except ShipError as error:
        assert "one seat with role desk" in str(error)
    else:
        raise AssertionError("a ship with no desk was accepted")


def test_units_match_the_morning_timer_and_install_nothing(tmp_path, monkeypatch):
    origin = ship(tmp_path / "daily")
    monkeypatch.setattr(cli, "load_ship", lambda args: origin)
    monkeypatch.setattr(cli.daily_live, "installed_qd", lambda value: "/usr/bin/qd")
    monkeypatch.setattr(cli.daily_live, "run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("ran")))
    args = cli.build_parser().parse_args(["daily-live", "--units"])
    text = "\n".join(cli.cmd_daily_live(args))
    assert cli.needs_ship(cli.build_parser().parse_args(["daily-live"]))
    assert "OnCalendar=*-*-* 23:00:00" in text
    assert "Persistent=true" in text
    assert "Type=oneshot" in text
    assert "TimeoutStartSec=infinity" in text
    assert "UnsetEnvironment=QD_SEAT" in text
    assert f"Environment=QD_SHIP={origin.folder}" in text
    assert "ExecStart=/usr/bin/qd daily-live" in text
    assert "WantedBy=timers.target" in text
    assert not (tmp_path / ".config").exists()


def _stub(path: Path) -> None:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "place = sys.argv[sys.argv.index('--ship') + 1]\n"
        "socket = os.environ.get('QD_TMUX_SOCKET')\n"
        "Path(os.environ['DAILY_LIVE_RECORD']).write_text(json.dumps({\n"
        "    'argv': sys.argv,\n"
        "    'seat': os.environ.get('QD_SEAT'),\n"
        "    'tmux': os.environ.get('TMUX'),\n"
        "    'tmpdir': os.environ.get('TMUX_TMPDIR'),\n"
        "    'socket': socket,\n"
        "    'ship': os.environ.get('QD_SHIP'),\n"
        "    'ship_exists': Path(place, 'ship.toml').is_file(),\n"
        "    'socket_name': Path(socket).name if socket else None,\n"
        "    'socket_parent_exists': Path(socket).parent.is_dir() if socket else False,\n"
        "    'namespace_header': Path(socket).parent.joinpath('.qd-tmux-lock').read_text(errors='replace')[:40] if socket else None,\n"
        "    'namespace_mode': oct(Path(socket).parent.stat().st_mode & 0o777) if socket else None,\n"
        "}))\n"
        "print('minimal check stood in')\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def test_each_check_is_minimal_on_a_private_throwaway_that_is_removed(tmp_path, monkeypatch):
    if daily_live.memory_root() is None:
        import pytest
        pytest.skip("no writable /dev/shm for a throwaway ship")
    origin = ship(tmp_path / "daily")
    stub = tmp_path / "qd-stub"
    record = tmp_path / "record.json"
    _stub(stub)
    monkeypatch.setenv("DAILY_LIVE_RECORD", str(record))
    monkeypatch.setenv("QD_SEAT", "worker2")
    monkeypatch.setenv("TMUX", "/not-the-private-server,1,1")
    before = os.environ.get("TMUX_TMPDIR")
    result = daily_live.invoke(origin, "codex", daily_live.memory_root(), str(stub))
    saved = json.loads(record.read_text())
    assert result.exit_code == 0
    assert result.retained is None
    assert saved["argv"] == [str(stub), "--ship", saved["ship"], "check", "codex", "--minimal"]
    assert saved["ship_exists"] is True
    assert saved["seat"] is None and saved["tmux"] is None and saved["tmpdir"] is None
    assert saved["socket_name"] == "s"
    assert saved["socket_parent_exists"] is True
    assert Path(saved["socket"]).parent.name.startswith("qd-pt-")
    assert saved["namespace_header"].startswith("quarterdeck private tmux namespace")
    assert saved["namespace_mode"] == "0o700"
    assert str(daily_live.memory_root()) in saved["ship"]
    assert not Path(saved["ship"]).exists()
    assert not Path(result.place).exists()
    assert not Path(saved["socket"]).parent.exists()
    assert os.environ.get("TMUX_TMPDIR") == before
    assert (origin.folder / "ship.toml").is_file()


def test_without_memory_the_throwaway_uses_the_scratch(tmp_path, monkeypatch):
    if daily_live.memory_root() is None:
        import pytest
        pytest.skip("private server still needs /dev/shm on a long scratch path")
    origin = ship(tmp_path / "daily")
    stub = tmp_path / "qd-stub"
    record = tmp_path / "record.json"
    scratch = tmp_path / "scratch"
    _stub(stub)
    monkeypatch.setenv("DAILY_LIVE_RECORD", str(record))
    monkeypatch.setattr(daily_live, "ship_root", lambda path: path)
    message, failed = daily_live.run(
        origin, now=NOW, readings=[reading("codex", working=False, ended=YESTERDAY)],
        scratch=scratch, qd=str(stub))
    saved = json.loads(record.read_text())
    assert failed is False
    assert saved["argv"][-3:] == ["check", "codex", "--minimal"]
    assert saved["ship"].startswith(str(scratch))
    assert not Path(saved["ship"]).exists()
    assert not scratch.joinpath(Path(saved["ship"]).relative_to(scratch).parts[0]).exists()
    assert Path(saved["socket"]).parent.name.startswith("qd-pt-")
    assert not Path(saved["socket"]).exists()
    assert "codex: not used that day; check passed" in message.body
    assert daily_live.MUSE_LINE in message.body


def test_the_live_checks_private_server_falls_back_outside_the_project(monkeypatch, tmp_path):
    """Never the project's work/ folder, which jails see through the workspaces bound from it."""
    import tempfile
    from quarterdeck import standing
    from quarterdeck.tmux import TmuxError

    blocked = tmp_path / 'file'
    blocked.write_text('not a folder')
    tried = []

    def private(place):
        tried.append(Path(place))
        if Path(place) != Path('/tmp') / f'qd-live-tmux-{os.getuid()}':
            raise TmuxError('synthetic: not here')
        return 'server'

    monkeypatch.setattr(daily_live, 'memory_root', lambda: None)
    monkeypatch.setattr(daily_live.Tmux, 'private', staticmethod(private))
    assert daily_live.open_private(blocked / 'ship-parent') == 'server'
    assert tried[-1] == Path('/tmp') / f'qd-live-tmux-{os.getuid()}'
    assert all(standing.PROJECT_DIR / 'work' not in (place, *place.parents) for place in tried)


def test_the_live_checks_last_place_is_the_systems_tmp_whatever_tmpdir_says(monkeypatch, tmp_path):
    """TMPDIR may name a folder a jail is given; the private servers stay in the system's own /tmp."""
    import tempfile
    from quarterdeck.tmux import TmuxError

    bound = tmp_path / 'bound-folder'
    bound.mkdir()
    monkeypatch.setenv('TMPDIR', str(bound))
    monkeypatch.setattr(tempfile, 'tempdir', None)
    blocked = tmp_path / 'file'
    blocked.write_text('not a folder')
    tried = []

    def private(place):
        tried.append(Path(place))
        if Path(place).parent != Path('/tmp'):
            raise TmuxError('synthetic: not here')
        return 'server'

    monkeypatch.setattr(daily_live, 'memory_root', lambda: None)
    monkeypatch.setattr(daily_live.Tmux, 'private', staticmethod(private))
    assert daily_live.open_private(blocked / 'ship-parent') == 'server'
    assert tried[-1] == Path('/tmp') / f'qd-live-tmux-{os.getuid()}'
    assert not any(bound in (place, *place.parents) for place in tried)


# A persistent timer runs a missed 23:00 on the machine's next start, after midnight.
CATCH_UP = datetime(2026, 11, 15, 7, 0, tzinfo=ZONE)
LATE_YESTERDAY = datetime(2026, 11, 14, 22, 30, tzinfo=ZONE).isoformat()  # the day that ended
EARLY_TODAY = datetime(2026, 11, 15, 6, 0, tzinfo=ZONE).isoformat()  # after midnight, before the run


def counting():
    calls = []

    def runner(origin, program, root, qd):
        calls.append(program)
        return daily_live.CheckRun(program, 0, "stood in")
    return calls, runner


def no_readings(*args, **kwargs):
    raise AssertionError("a day already reported reads nothing")


@pytest.mark.parametrize("moment, day", [
    (datetime(2026, 11, 15, 7, 0, tzinfo=ZONE), "2026-11-14"),
    (datetime(2026, 11, 15, 0, 0, tzinfo=ZONE), "2026-11-14"),
    (datetime(2026, 11, 15, 22, 59, 59, 999999, tzinfo=ZONE), "2026-11-14"),
    (datetime(2026, 11, 15, 23, 0, tzinfo=ZONE), "2026-11-15"),
    (datetime(2026, 11, 15, 23, 59, tzinfo=ZONE), "2026-11-15"),
])
def test_the_covered_day_is_the_latest_whose_23_00_has_passed(moment, day):
    covered = daily_live.covered_day(moment)
    assert covered.date().isoformat() == day
    assert (covered.hour, covered.minute, covered.tzinfo) == (23, 0, ZONE)


def test_a_catch_up_after_midnight_covers_the_day_that_ended_by_that_days_use(tmp_path):
    origin = ship(tmp_path / "daily")
    calls, runner = counting()
    message, failed = daily_live.run(
        origin, now=CATCH_UP, runner=runner, scratch=tmp_path / "scratch",
        readings=[reading("claude", ended=LATE_YESTERDAY), reading("codex", ended=EARLY_TODAY)])
    assert failed is False
    assert message.body.startswith("Daily live test for 2026-11-14.\n")
    # Used before midnight: not checked. Used only after midnight: not used that day, so checked.
    assert calls == ["codex"]
    assert "claude: used that day; not checked" in message.body
    assert "codex: not used that day; check passed" in message.body
    assert daily_live.last_reported(origin) == (datetime(2026, 11, 14).date(), message.id)
    assert json.loads((origin.runtime / daily_live.REPORTED).read_text()) == {
        "day": "2026-11-14", "report": message.id}


def test_a_second_run_the_same_day_does_nothing(tmp_path, monkeypatch):
    origin = ship(tmp_path / "daily")
    calls, runner = counting()
    first, _ = daily_live.run(origin, now=CATCH_UP, runner=runner, scratch=tmp_path / "scratch",
                              readings=[reading("codex", ended=EARLY_TODAY)])
    assert calls == ["codex"]
    record = (origin.runtime / daily_live.REPORTED).read_bytes()
    monkeypatch.setattr(daily_live.program_use, "collect", no_readings)
    for later in (CATCH_UP + timedelta(minutes=30), datetime(2026, 11, 15, 22, 59, tzinfo=ZONE)):
        assert daily_live.run(origin, now=later, runner=runner, scratch=tmp_path / "scratch") == (None, False)
    assert calls == ["codex"]
    assert [item.id for item in post.Store(origin.runtime).all()] == [first.id]
    assert (origin.runtime / daily_live.REPORTED).read_bytes() == record


def test_a_run_at_23_05_covers_that_day_after_a_catch_up_for_the_day_before(tmp_path):
    origin = ship(tmp_path / "daily")
    calls, runner = counting()
    daily_live.run(origin, now=CATCH_UP, runner=runner, scratch=tmp_path / "scratch",
                   readings=[reading("codex", ended=EARLY_TODAY)])
    tonight, failed = daily_live.run(
        origin, now=datetime(2026, 11, 15, 23, 5, tzinfo=ZONE), runner=runner, scratch=tmp_path / "scratch",
        readings=[reading("codex", ended=EARLY_TODAY), reading("grok", ended=LATE_YESTERDAY)])
    assert tonight.body.startswith("Daily live test for 2026-11-15.\n")
    assert "codex: used that day; not checked" in tonight.body
    assert calls == ["codex", "grok"]
    assert daily_live.last_reported(origin) == (datetime(2026, 11, 15).date(), tonight.id)


def test_a_run_before_23_00_with_yesterday_reported_does_nothing(tmp_path, monkeypatch):
    origin = ship(tmp_path / "daily")
    calls, runner = counting()
    yesterday, _ = daily_live.run(origin, now=datetime(2026, 11, 15, 23, 0, tzinfo=ZONE), runner=runner,
                                  scratch=tmp_path / "scratch", readings=[reading("codex")])
    monkeypatch.setattr(daily_live.program_use, "collect", no_readings)
    assert daily_live.run(origin, now=datetime(2026, 11, 16, 22, 0, tzinfo=ZONE), runner=runner) == (None, False)
    assert calls == ["codex"]
    assert [item.id for item in post.Store(origin.runtime).all()] == [yesterday.id]
    monkeypatch.setattr(cli, "load_ship", lambda args: origin)
    monkeypatch.setattr(cli.daily_live, "current_time", lambda: datetime(2026, 11, 16, 22, 0, tzinfo=ZONE))
    args = cli.build_parser().parse_args(["daily-live"])
    assert cli.cmd_daily_live(args) == [f"daily live test: 2026-11-15 is already reported ({yesterday.id}); nothing run"]


@pytest.mark.parametrize("text", ["", "{", "[]", '{"day": "2026-11-14"}', '{"day": "2026-11-14", "report": 7}', '{"day": "yesterday", "report": "x"}'])
def test_an_unreadable_record_runs_nothing(tmp_path, text):
    origin = ship(tmp_path / "daily")
    origin.runtime.mkdir(parents=True, exist_ok=True)
    (origin.runtime / daily_live.REPORTED).write_text(text)
    calls, runner = counting()
    with pytest.raises(ShipError, match="unreadable; nothing was run"):
        daily_live.run(origin, now=CATCH_UP, runner=runner, readings=[reading("codex")])
    assert calls == []
    assert not (origin.runtime / "post" / "store").exists() or not post.Store(origin.runtime).all()
