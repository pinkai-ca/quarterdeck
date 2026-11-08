"""Codex 0.166.0 against the screens saved from the spike of 7 November 2026."""

from __future__ import annotations

from pathlib import Path

from quarterdeck.harness.codex import CodexHarness
from quarterdeck.ship import Pool, SeatSpec

from tests.conftest import FIXTURES

VERSION = "0.166.0"
SCREENS = FIXTURES / "codex" / VERSION


def screen(name: str):
    return CodexHarness().screen((SCREENS / f"{name}.txt").read_text())


def test_every_fixture_set_has_the_screens_the_adapter_reads():
    names = {p.stem for p in SCREENS.glob("*.txt")}
    assert {"idle", "paste-6-lines-shown", "paste-long-folded", "working", "model-picker", "interrupted"} <= names


def test_an_idle_seat_shows_its_placeholder_as_an_empty_box():
    s = screen("idle")
    assert s.box == "" and not s.working and s.interruption is None
    assert s.shows == {"model": "GPT-6-Astra", "effort": "low"}


def test_a_short_paste_shows_whole_and_a_long_one_folds_to_its_length():
    """Probed 7 November 2026: six short lines show whole; 2,003 characters fold to a placeholder counting every one."""
    harness = CodexHarness()
    six = "qd > order from desk · ord-5678 · Six\na\nb\nc\nd\nqd > reply: qd post report --re ord-5678"
    s = screen("paste-6-lines-shown")
    assert harness.box(six).matches(s.box), s.box
    assert not harness.box("something else").matches(s.box)
    long = "qd probe B: first line\n" + " ".join(f"word{i}" for i in range(260)) + "\nlast line\n"
    assert len(long) == 2003
    folded = screen("paste-long-folded")
    assert folded.box == "[Pasted Content 2003 chars]" and harness.box(long).matches(folded.box)
    assert not harness.box(long + "x").matches(folded.box), "the count is this paste's own"


def test_a_working_seat_is_open_and_queues():
    s = screen("working")
    assert s.working and s.box == "" and s.interruption is None
    assert CodexHarness().queues_while_working(SeatSpec("s", "bridge", "engineering", "codex", ("codex",)))


def test_a_message_queued_mid_turn_is_echoed_above_the_working_line():
    from quarterdeck.door import echoed

    s = screen("working")
    typed = "qd > order from desk · ord-0002 · Second\nThis arrived mid-turn. Say SECOND-SEEN.\nqd > reply: qd post report --re ord-0002\n"
    assert any(echoed(e, " ".join(typed.split())) for e in s.echoes), s.echoes
    assert s.echoes[-1].startswith("qd > order from desk · ord-0002 · Second"), "the queued one is the last echo before Working"
    assert any("Run exactly this shell command" in e for e in s.echoes), "the turn's own prompt is echoed too"
    assert echoed("qd > order from desk · ord-7f3a · Spike What seat are you, per your develope…",
                  "qd > order from desk · ord-7f3a · Spike What seat are you, per your developer instructions? And more")
    assert not echoed("qd > order from desk · ord-7f3a · Spike", "qd > order from desk · ord-7f3b · Spike")


def test_the_model_picker_is_known_and_closed_with_escape():
    s = screen("model-picker")
    assert s.interruption is not None and s.interruption.name == "model picker" and s.interruption.key == "Escape"
    harness = CodexHarness()
    assert harness.control("model").session is None, "model changes at launch: qd set says restart applies it"


def test_a_paste_with_a_blank_line_shows_whole_in_the_box():
    harness = CodexHarness()
    msg = "qd > order from desk · ord-1234\nFirst paragraph.\n\nSecond paragraph.\nqd > reply: qd post report --re ord-1234"
    raw = ("› qd > order from desk · ord-1234\n  First paragraph.\n\n  Second paragraph.\n  qd > reply: qd post report --re ord-1234\n\n"
           "  GPT-6-Astra low · /work · main\n  ? for shortcuts\n")
    s = harness.screen(raw)
    assert s.box == msg and harness.box(msg).matches(s.box)
    working = raw.replace("\n  GPT-6-Astra", "\n• Working (1m 05s • esc to interrupt)\n\n› Ask Codex to do anything\n\n  GPT-6-Astra")
    w = harness.screen(working)
    assert w.working and w.box == "" and any(" ".join(e.split()) == " ".join(msg.split()) for e in w.echoes)
    assert harness.screen((SCREENS / "model-picker.txt").read_text()).box is None, "a picker is not a box"


def test_prose_on_the_screen_is_never_a_dialog_while_the_box_is_there():
    s = CodexHarness().screen("Daybreak\nSelect Model and Effort\n" + (SCREENS / "working.txt").read_text())
    assert s.working and s.box == "" and s.interruption is None


def test_a_failed_shell_call_in_list_form_is_a_failure(tmp_path: Path):
    import json

    from quarterdeck.harness import codex

    record = tmp_path / "rollout.jsonl"
    record.write_text("\n".join(json.dumps(e) for e in [
        {"timestamp": "2026-11-07T04:00:00Z", "type": "event_msg", "payload": {"type": "task_started"}},
        {"timestamp": "2026-11-07T04:00:01Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": "run it"}},
        {"timestamp": "2026-11-07T04:00:02Z", "type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "c1"}},
        {"timestamp": "2026-11-07T04:00:03Z", "type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "c1",
                                                                                    "output": [{"type": "output_text", "text": "Exit code 17\nboom"}]}},
        {"timestamp": "2026-11-07T04:00:04Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": "qd > order · ord-0002\nsecond"}},
        {"timestamp": "2026-11-07T04:00:10Z", "type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "done"}},
    ]) + "\n")
    state, turns = codex.read(record)
    assert [t.failed for t in turns[0].tools] == [True]
    assert state.consumed == ("qd > order · ord-0002\nsecond",) and state.last_input.startswith("qd > order"), "a message taken into the open turn is kept"


def test_seat_settings_replace_template_keys_and_a_ship_template_keeps_the_required_sections(tmp_path: Path):
    import tomllib

    harness = CodexHarness()
    pool_dir = tmp_path / "pool"
    pool_dir.mkdir()
    (pool_dir / "auth.json").write_text("{}")
    pool = Pool("codex", "codex", config_dir=pool_dir)
    seat = SeatSpec("w", "crew", "worker", "codex", ("codex",), effort="high", cwd=tmp_path / "work",
                    settings={"model_reasoning_effort": "low", "sandbox_mode": "read-only", "project_doc_max_bytes": 4096})
    home = tmp_path / "ship" / "runtime" / "seats" / "w" / "home"
    harness.build_home(home, seat, pool, None)
    config = tomllib.loads((home / "config.toml").read_text())
    assert config["model_reasoning_effort"] == "low" and config["sandbox_mode"] == "danger-full-access" and config["project_doc_max_bytes"] == 4096
    ship_home = tmp_path / "homes" / "codex"
    ship_home.mkdir(parents=True)
    (ship_home / "config.toml").write_text('model = "gpt-6-luna"\nhide_agent_reasoning = true\n')
    plain = SeatSpec("x", "crew", "worker", "codex", ("codex",), cwd=tmp_path / "work")
    harness.build_home(home, plain, pool, ship_home)
    config = tomllib.loads((home / "config.toml").read_text())
    assert config["model"] == "gpt-6-luna" and config["hide_agent_reasoning"] is True, "the ship's own choices stand"
    assert config["project_doc_max_bytes"] == 0 and config["sandbox_mode"] == "danger-full-access"
    assert config["projects"][str(tmp_path / "work")]["trust_level"] == "trusted"
    assert config["approval_policy"] == "never" and "sandbox_workspace_write" not in config  # law: can-post


def test_an_interrupted_seat_is_idle():
    s = screen("interrupted")
    assert not s.working


def test_the_launch_builds_a_home_of_the_ships_own(tmp_path: Path):
    harness = CodexHarness()
    pool_dir = tmp_path / "pool"
    pool_dir.mkdir()
    (pool_dir / "auth.json").write_text("{}")
    pool = Pool("codex", "codex", config_dir=pool_dir)
    runtime = tmp_path / "ship" / "runtime"
    seat = SeatSpec("scott", "bridge", "engineering", "codex", ("codex",), model="gpt-6-astra", effort="xhigh",
                    cwd=tmp_path / "work", settings={"model_auto_compact_token_limit": 200000})
    home = runtime / "seats" / "scott" / "home"
    credential = harness.build_home(home, seat, pool, None)
    assert credential.link == (home / "auth.json", pool_dir / "auth.json") and (home / "auth.json").is_symlink()
    config = (home / "config.toml").read_text()
    assert 'model = "gpt-6-astra"' in config and 'model_reasoning_effort = "xhigh"' in config
    assert "project_doc_max_bytes = 0" in config and 'sandbox_mode = "danger-full-access"' in config
    assert f'[projects."{tmp_path / "work"}"]\ntrust_level = "trusted"' in config
    assert "model_auto_compact_token_limit = 200000\n" in config, "the seat's own keys pass through"
    brief = tmp_path / "brief.md"
    brief.write_text("# Standing orders\nBe brief.\n")
    launch = harness.launch(seat, pool, home, brief, None, None)
    assert launch.argv[:3] == ("codex", "--no-daemon", "-c") and launch.argv[3] == 'developer_instructions="# Standing orders\\nBe brief.\\n"'
    assert launch.argv[4:] == ("-m", "gpt-6-astra", "-c", 'model_reasoning_effort="xhigh"')
    assert launch.env == {"CODEX_HOME": str(home)}
    resumed = harness.launch(seat, pool, home, brief, "0199-abc", "qd > briefed")
    assert resumed.argv[-3:] == ("resume", "0199-abc", "qd > briefed")


def test_rebuild_replaces_legacy_home_template_and_profile_policy_without_editing_sources(tmp_path):
    import copy
    import tomllib

    home = tmp_path / 'runtime' / 'seats' / 'worker' / 'home'
    home.mkdir(parents=True)
    (home / 'config.toml').write_text('sandbox_mode = "workspace-write"\napproval_policy = "on-request"\n'
                                     '[sandbox_workspace_write]\nwritable_roots = ["/old"]\nnetwork_access = true\n')
    template = tmp_path / 'templates'
    template.mkdir()
    original = '''model = "chosen-model"
profile = "legacy"
sandbox_mode = "workspace-write"
approval_policy = "on-request"
model_auto_compact_token_limit = 12345
[sandbox_workspace_write]
writable_roots = ["/old"]
network_access = false
[profiles.legacy]
sandbox_mode = "read-only"
approval_policy = "on-request"
model_reasoning_effort = "low"
[profiles.legacy.sandbox_workspace_write]
writable_roots = ["/old-profile"]
[tui]
notifications = ["agent-turn-complete"]
[mcp_servers.local]
command = "local-server"
args = ["--name", "example"]
env = { EXAMPLE = "preserved" }
'''
    source = template / 'config.toml'
    source.write_text(original)
    settings = {'approval_policy':'on-request','sandbox_mode':'read-only','model_reasoning_summary':'detailed'}
    before = copy.deepcopy(settings)
    spec = SeatSpec('worker','crew','worker','codex',('codex',),cwd=tmp_path,settings=settings)
    harness, pool = CodexHarness(), Pool('codex','codex')
    harness.build_home(home,spec,pool,template)
    first = (home/'config.toml').read_text()
    parsed = tomllib.loads(first)
    for scope in (parsed,parsed['profiles']['legacy']):
        assert scope['sandbox_mode'] == 'danger-full-access' and scope['approval_policy'] == 'never'  # law: can-post
        assert 'sandbox_workspace_write' not in scope
    assert parsed['features']['memories'] is False, "the program's own memory is off: a seat keeps nothing of its own between conversations"
    expected = tomllib.loads(original)
    assert parsed['model'] == 'chosen-model' and parsed['profile'] == 'legacy'
    assert parsed['tui'] == expected['tui'] and parsed['mcp_servers'] == expected['mcp_servers']
    assert parsed['profiles']['legacy']['model_reasoning_effort'] == 'low'
    assert parsed['model_auto_compact_token_limit'] == 12345
    assert parsed['model_reasoning_summary'] == 'detailed'
    assert source.read_text() == original and settings == before
    harness.build_home(home,spec,pool,template)
    assert (home/'config.toml').read_text() == first, 'rebuilding is idempotent'
    # An old generated home without a template is rebuilt by the same path too.
    (home/'config.toml').write_text(original)
    harness.build_home(home,spec,pool,None)
    assert tomllib.loads((home/'config.toml').read_text())['sandbox_mode'] == 'danger-full-access'
    assert 'sandbox_workspace_write' not in (home/'config.toml').read_text()


def test_config_writer_preserves_unrelated_toml_types_and_does_not_mutate_seat_profile(tmp_path):
    import copy
    import tomllib
    from quarterdeck.harness.codex import config_text

    document = '''title = "workspace 🚀"
limit = 100
scale = 0.5
flags = [true, false]
date = 2026-01-01
when = 2026-01-01T12:00:00Z
clock = 12:34:56
servers = [{name = "local", args = ["a"]}]
["key.with.dots".empty]
'''
    assert tomllib.loads(config_text(tomllib.loads(document))) == tomllib.loads(document)
    settings = {'profiles': {'custom': {'sandbox_mode': 'read-only', 'model': 'custom-model'}}}
    before = copy.deepcopy(settings)
    home = tmp_path/'runtime'/'seats'/'worker'/'home'
    spec = SeatSpec('worker','crew','worker','codex',('codex',),settings=settings)
    CodexHarness().build_home(home,spec,Pool('codex','codex'),None)
    assert settings == before
    result = tomllib.loads((home/'config.toml').read_text())
    assert result['profiles']['custom'] == {'sandbox_mode':'danger-full-access','approval_policy':'never','model':'custom-model'}


def test_restart_rebuilds_generated_policy_before_opening_the_program(tmp_path, monkeypatch):
    import tomllib
    from types import SimpleNamespace
    from quarterdeck import seat as seatmod
    from quarterdeck.seat import Seat
    from quarterdeck.ship import Session, Ship

    spec = SeatSpec('worker','crew','worker','codex',('codex',),cwd=tmp_path)
    ship = Ship(tmp_path,'example','admiral',tmp_path,{'codex':Pool('codex','codex')},
                {'crew':Session('crew','crew')},{'worker':spec})
    seat = Seat(ship,spec,SimpleNamespace())
    seat.home.mkdir(parents=True)
    (seat.home/'config.toml').write_text('sandbox_mode = "workspace-write"\napproval_policy = "on-request"\n')
    monkeypatch.setattr(seat,'pane',lambda:None)
    monkeypatch.setattr(seat,'_resume_id',lambda:'synthetic-resume')
    monkeypatch.setattr(seat,'_survives',lambda *args:None)
    monkeypatch.setattr(seat.harness,'version',lambda:'synthetic-version')
    monkeypatch.setattr(seatmod.shutil,'which',lambda *args,**kwargs:'/synthetic/codex')
    launches = []

    def open_window(argv,env,path):
        config = tomllib.loads((seat.home/'config.toml').read_text())
        assert config['sandbox_mode'] == 'danger-full-access' and config['approval_policy'] == 'never'
        assert 'sandbox_workspace_write' not in config
        assert env['CODEX_HOME'] == str(seat.home)
        launches.append(argv)
        return SimpleNamespace(id='%1',pid=1234)

    monkeypatch.setattr(seat,'_open_window',open_window)
    assert 'resumed synthet' in seat.restart()
    assert len(launches) == 1 and launches[0][-2:] == ['resume','synthetic-resume']


def test_config_writer_escapes_del_in_strings_and_keys_without_surrogate_pairs():
    import tomllib
    from quarterdeck.harness.codex import config_text

    source = r'"key\u007f🚀" = "value\u007f🚀"'
    expected = {"key\x7f🚀": "value\x7f🚀"}
    assert tomllib.loads(source) == expected
    encoded = config_text(tomllib.loads(source))
    assert "\x7f" not in encoded and r"\u007f" in encoded
    assert tomllib.loads(encoded) == expected
