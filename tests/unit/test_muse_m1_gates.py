"""Claude-owned synthetic gates for the Muse fixed-path mask and its quota-offer dialog.

No native Muse, provider, credential or operator path is involved. The password-database home is
replaced by a synthetic folder under the test's own tmp, so every mount target here is synthetic;
nothing is executed (the real-bwrap cases live in tests/integration/test_muse_m1_mask_bwrap.py).
Dialog screens are built only from the labels the pinned 1.4.15 binary carries; their layout,
option order and preselection are unmeasured, so the gates never depend on positions.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import turn_failure
from quarterdeck.door import Door
from quarterdeck.harness import get, muse_mask, muse_screen, NAMES
from quarterdeck.harness.base import Harness
from quarterdeck.harness.muse import MuseHarness
from quarterdeck.harness.types import State
from quarterdeck.ship import Pool, SeatSpec, ShipError

from .test_owned_paste import editor, frame  # noqa: F401  (shared synthetic editor fixture)

TITLE = "You're using your quota fast"
OPTIONS = ("Keep going", "Switch to Spark", "Turn on contributor mode")
SWITCHED = "Switched to Spark (saved as default)"
CONTRIBUTOR_ON = "Contributor mode on"
CONTRIBUTOR_OFF_NOTICE = ("Contributor mode is off, so your content won't be used for product improvement. "
                          "Turn it on in /settings.")
RULE = "─" * 40


def composer(text: str = "", above: str = "") -> str:
    """The offline-measured idle composer, as a synthetic frame."""
    return (f"  Muse Code 1.4.15\n{above}{RULE}\n❯ {text}\n{RULE}\n"
            "  echo · /s/y/n/workspace · Launch overrides")


def offer(*options: str, title: bool = True, wrap: bool = False) -> str:
    lines = ([TITLE] if title else []) + [f"  {option}" for option in options]
    if wrap:  # every word on its own line: no layout is assumed
        lines = [word for line in lines for word in line.split(" ")]
    return "\n".join(lines) + "\nnot active yet - esc close\n"


@pytest.fixture
def passwd_home(tmp_path, monkeypatch):
    """A synthetic password-database home with Muse's two fixed folders present, as on a host."""
    home = tmp_path / "synthetic-passwd-home"
    (home / ".local/share/muse").mkdir(parents=True)
    (home / ".config/muse").mkdir(parents=True)
    monkeypatch.setattr(muse_mask.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(home)))
    return home


@pytest.fixture
def seat_home(tmp_path, passwd_home):
    home = tmp_path / "synthetic-seat-home"
    muse_mask.prepare(home)
    return home


def production(home: Path, cwd: Path) -> tuple[str, ...]:
    return ("/usr/bin/bwrap", "--die-with-parent", "--dev-bind", "/", "/",
            "--bind", str(home / "mask/passwd-muse"), str(muse_mask.authority_target()),
            "--bind", str(home / "mask/passwd-config"), str(muse_mask.config_target()),
            "--bind", str(home / "mask/tmp"), "/tmp",
            "--chdir", str(cwd), "--")


# -- the mask: one shared prefix ------------------------------------------------------------------


def test_prefix_is_the_host_root_plus_exactly_three_private_binds(seat_home, passwd_home, tmp_path):
    prefix = muse_mask.prefix(seat_home, tmp_path)
    assert prefix == production(seat_home, tmp_path)
    assert muse_mask.authority_target() == passwd_home / ".local/share/muse"
    assert muse_mask.config_target() == passwd_home / ".config/muse"
    # Not the endpoint jail: no read-only root, no hidden home, no namespace of its own.
    assert not any(flag.startswith(("--ro-bind", "--tmpfs", "--unshare", "--clearenv")) for flag in prefix)
    assert prefix.count("--bind") == 3 and prefix.count("/usr/bin/bwrap") == 1


def test_offline_adds_only_a_network_namespace_to_the_same_invocation(seat_home, tmp_path):
    online, offline = muse_mask.prefix(seat_home, tmp_path), muse_mask.prefix(seat_home, tmp_path, offline=True)
    assert "--unshare-net" not in online
    assert offline.count("--unshare-net") == 1 and offline.count("/usr/bin/bwrap") == 1
    assert tuple(flag for flag in offline if flag != "--unshare-net") == online
    argv = ("/synthetic/program", "synthetic-argument")
    assert muse_mask.command(seat_home, tmp_path, argv) == (*online, *argv)
    assert muse_mask.command(seat_home, tmp_path, argv, offline=True) == (*offline, *argv)


def test_password_database_home_wins_over_home_and_xdg(passwd_home, tmp_path, monkeypatch):
    for name in ("HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(tmp_path / f"synthetic-{name.lower()}"))
    assert muse_mask.authority_target() == passwd_home / ".local/share/muse"
    assert muse_mask.config_target() == passwd_home / ".config/muse"


def test_private_sources_are_owner_only_folders_under_the_seat_home(seat_home):
    for name in ("passwd-muse", "passwd-config", "tmp"):
        folder = seat_home / "mask" / name
        info = folder.lstat()
        assert stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700
        assert info.st_uid == os.getuid() and folder.parent.parent == seat_home


@pytest.mark.parametrize("fault", ["link", "mode"])
def test_a_replaced_or_widened_private_source_refuses_the_prefix(seat_home, tmp_path, fault):
    folder = seat_home / "mask/tmp"
    if fault == "link":
        folder.rmdir()
        elsewhere = tmp_path / "synthetic-elsewhere"
        elsewhere.mkdir(mode=0o700)
        folder.symlink_to(elsewhere, target_is_directory=True)
    else:
        folder.chmod(0o755)
    with pytest.raises(ShipError):
        muse_mask.prefix(seat_home, tmp_path)


def test_missing_system_bubblewrap_refuses_and_never_plans_an_unmasked_start(passwd_home, tmp_path, monkeypatch):
    monkeypatch.setattr(muse_mask, "PROGRAM", tmp_path / "synthetic-absent-bwrap")
    with pytest.raises(ShipError, match="bwrap"):
        muse_mask.prepare(tmp_path / "synthetic-seat-home")
    assert not (tmp_path / "synthetic-seat-home").exists()


def test_absent_fixed_folders_refuse_rather_than_letting_bwrap_create_them(tmp_path, monkeypatch):
    home = tmp_path / "synthetic-bare-passwd-home"
    home.mkdir()
    monkeypatch.setattr(muse_mask.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(home)))
    with pytest.raises(ShipError, match="missing"):
        muse_mask.preflight(tmp_path / "synthetic-seat-home")
    assert list(home.iterdir()) == []


def test_a_seat_home_under_the_masked_tmp_is_refused(passwd_home):
    with pytest.raises(ShipError, match="outside the /tmp"):
        muse_mask.preflight(Path("/tmp") / "synthetic-seat-home-wxyz")


# -- the registered launch uses that prefix ------------------------------------------------------


@pytest.fixture
def launched(tmp_path, passwd_home, monkeypatch):
    """Build a seat home and launch plans through the registered adapter; nothing is executed."""
    monkeypatch.setattr("quarterdeck.harness.muse_program.binary",
                        lambda **kwargs: Path("/synthetic/bin/muse-bin-1.4.15-R8339.1"))
    template = tmp_path / "synthetic-template"
    template.mkdir()
    (template / "settings.json").write_text(json.dumps(dict(provider="echo", model="synthetic-catalog-model")))
    workspace = tmp_path / "synthetic-workspace"
    workspace.mkdir()
    spec = SeatSpec("synthetic-seat", "synthetic-session", "worker", "muse", ("synthetic-pool",), cwd=workspace)
    pool = Pool("synthetic-pool", "meta", config_dir=tmp_path / "synthetic-unused-pool")
    harness, home = get("muse"), tmp_path / "synthetic-launch-home"
    harness.build_home(home, spec, pool, template)
    brief = tmp_path / "synthetic-brief.txt"
    brief.write_text("A fictional brief.")

    def plan(**kwargs):
        return harness.launch(spec, pool, home, brief, None, None, **kwargs)
    return SimpleNamespace(harness=harness, home=home, workspace=workspace, plan=plan)


def test_registered_launch_is_masked_by_the_shared_prefix_and_production_by_default(launched):
    default, offline = launched.plan(), launched.plan(offline=True)
    prefix = muse_mask.prefix(launched.home, launched.workspace)
    assert default.argv[:len(prefix)] == prefix and "--unshare-net" not in default.argv
    assert offline.argv[:len(prefix) + 1] == muse_mask.prefix(launched.home, launched.workspace, offline=True)
    assert offline.argv.count("--unshare-net") == 1
    assert default.argv[len(prefix):] == offline.argv[len(prefix) + 1:]
    for launch in (default, offline):
        assert "--disable-sandbox" in launch.argv and launch.env["TMPDIR"] == str(launched.home / "mask/tmp")
        assert launched.harness.owns_launch(launch.argv)


def test_ownership_refuses_a_wrapper_whose_bind_targets_differ(launched, tmp_path):
    argv = list(launched.plan().argv)
    argv[argv.index(str(muse_mask.authority_target()))] = str(tmp_path / "synthetic-other-target")
    assert not launched.harness.owns_launch(argv)
    assert not launched.harness.owns_launch(list(launched.plan().argv)[5:])


def test_only_muse_overrides_the_guard_hooks_and_the_strict_interval():
    for name in NAMES:
        cls = type(get(name))
        hooks = (cls.delivery_obstruction is Harness.delivery_obstruction,
                 cls.interrupt_obstruction is Harness.interrupt_obstruction, cls.strict_usage_interval is False)
        assert hooks == ((False, False, False) if name == "muse" else (True, True, True)), name


# -- the quota offer: named, never answered ------------------------------------------------------


@pytest.mark.parametrize("option", OPTIONS)
def test_the_offer_is_named_from_its_title_and_any_one_option(option):
    for text in (offer(option), offer(option) + composer(), "\x1b[1m" + offer(option) + "\x1b[0m"):
        screen = muse_screen.read(text)
        assert screen.interruption == muse_screen.QUOTA_OFFER and screen.box is None
        assert screen.unknown is None and not screen.working
    plain = muse_screen.read(composer())
    assert plain.interruption is None and plain.box == ""  # the control: an ordinary composer still opens


@pytest.mark.parametrize("text", [offer(*OPTIONS, wrap=True), offer(), offer(*OPTIONS) + composer("draft")],
                         ids=["wrapped", "options-unreadable", "over-a-composer"])
def test_a_wrapped_cut_or_overlaid_offer_is_held_never_open(text):
    screen = muse_screen.read(text)
    assert screen.interruption in (muse_screen.QUOTA_OFFER, muse_screen.QUOTA_INCOMPLETE)
    assert screen.box is None


def offering(editor, monkeypatch, text):
    editor.seat.harness = MuseHarness()
    monkeypatch.setattr(editor.terminal, "capture", lambda *a, **kw: text)
    monkeypatch.setattr(Door, "adapter_state", lambda self: None)
    monkeypatch.setattr(turn_failure, "obstruction", lambda seat, store=None: None)
    return Door(editor.seat)


def test_while_the_offer_shows_the_door_types_nothing_and_presses_no_key(editor, monkeypatch):
    door = offering(editor, monkeypatch, offer(*OPTIONS) + composer())
    result = door.deliver("A fictional order.")
    assert result.result == "withheld" and muse_screen.QUOTA_OFFER.name in result.detail
    assert door.interrupt() and editor.terminal.submitted == []
    assert door.clear_known() is not None
    assert editor.terminal.pastes == [] and editor.terminal.submitted == [] and editor.terminal.clears == []


def test_the_esc_hint_never_makes_a_model_choice_dismissable():
    for interruption in muse_screen.MODEL_CHOICES:
        assert interruption.key is None and not interruption.dismissable


def test_the_offer_reaches_a_person_and_is_named(editor, monkeypatch):
    door = offering(editor, monkeypatch, offer("Switch to Spark"))
    reading, reason = door.human_obstruction()
    assert not reading.open and reading.clears == "needs a human"
    assert reason and muse_screen.QUOTA_OFFER.name in reason and "needs a human" in reason


def test_an_offer_alone_claims_no_model_and_no_contributor_state():
    screen = muse_screen.read(offer(*OPTIONS))
    assert "model" not in screen.shows
    harness = MuseHarness()
    state = State(turn_open=False, model="synthetic-catalog-model")
    held = harness.delivery_obstruction(state, screen)
    assert held and "contributor model" not in held and "selected" not in held
    assert harness.delivery_obstruction(state, muse_screen.read(composer())) is None


@pytest.mark.parametrize("text,named", [
    (SWITCHED, muse_screen.MODEL_SWITCHED),
    (CONTRIBUTOR_ON, muse_screen.CONTRIBUTOR_ENABLED),
    ("Contributor mode is on for this session only: settings could not be saved.", muse_screen.CONTRIBUTOR_ENABLED),
])
def test_a_made_choice_is_reported_as_a_change_and_holds(text, named):
    screen = muse_screen.read(composer(above=text + "\n"))
    assert screen.interruption == named and screen.box is None
    assert MuseHarness().delivery_obstruction(None, screen)


def test_the_off_notice_is_not_misread_as_contributor_on_and_a_contributor_record_holds():
    assert muse_screen.read(composer(above=CONTRIBUTOR_OFF_NOTICE + "\n")).interruption is None
    held = MuseHarness().delivery_obstruction(State(model="synthetic-catalog-model-contributor"), None)
    assert held and "contributor" in held


@pytest.mark.parametrize("option", ["Switch to Spark", "Turn on contributor mode"])
def test_a_model_or_data_use_option_without_its_title_still_holds(option):
    """The captain's rule: either label anywhere names a held quota offer; a quoted line is a safe false hold."""
    for text in (composer(above=f"  {option}\n"), offer(option, title=False) + composer(),
                 offer(option, title=False, wrap=True) + composer()):
        screen = muse_screen.read(text)
        assert screen.interruption in muse_screen.MODEL_CHOICES and screen.box is None
        assert MuseHarness().delivery_obstruction(None, screen)


def test_keep_going_alone_is_too_ordinary_to_hold():
    screen = muse_screen.read(composer(above="  Keep going\n"))
    assert screen.interruption is None and screen.box == ""
