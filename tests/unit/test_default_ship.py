"""qd's default ship: the mark in ships/.default, the only-ship fallback, the refusals, and how it is said.

Every case runs in a synthetic project folder; nothing reads or writes the real project's ships/.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from quarterdeck import cli, doctor, fleet_post, install, post, ship as shipmod, standing
from quarterdeck.ship import ShipError

DRYDOCK = Path(__file__).resolve().parents[2] / "ships" / "drydock" / "ship.toml"


class Project(type(Path())):
    """The synthetic project folder, carrying what the stubbed handoff was given."""


@pytest.fixture
def project(tmp_path, monkeypatch):
    for name in ("QD_SHIP", "QD_SEAT", cli.HANDED):
        monkeypatch.delenv(name, raising=False)
    root = Project(tmp_path / "project")
    (root / "ships").mkdir(parents=True)
    monkeypatch.setattr(standing, "PROJECT_DIR", root)
    handed = []
    monkeypatch.setattr(install, "handoff", lambda folder, argv: handed.append(
        (Path(folder), list(argv), os.environ.get(cli.HANDED))))
    root.handed = handed
    return root


def ship(root: Path, name: str) -> Path:
    folder = root / "ships" / name
    folder.mkdir()
    shutil.copy(DRYDOCK, folder / "ship.toml")
    return folder


def templates(root: Path) -> None:
    for name in sorted(shipmod.TEMPLATES):
        ship(root, name)


def mark(root: Path, text: str) -> None:
    (root / "ships" / ".default").write_text(text)


# -- the choice -------------------------------------------------------------------------


def test_the_mark_chooses_the_ship(project):
    templates(project)
    ship(project, "endeavour")
    ship(project, "defiant")
    mark(project, "defiant\n")
    folder, name = shipmod.choose(None)
    assert folder == (project / "ships" / "defiant").resolve() and name == "defiant"
    assert shipmod.default_ship()[1] == "mark"
    assert shipmod.find() == folder


@pytest.mark.parametrize("name", sorted(shipmod.TEMPLATES))
def test_templates_are_refused_as_new_and_preexisting_marks(project, name):
    folder = ship(project, name)
    ship(project, "endeavour")
    mark(project, "endeavour\n")
    mark_file = project / "ships/.default"
    with pytest.raises(ShipError, match="template or practice ship"):
        shipmod.mark_default(name)
    assert mark_file.read_bytes() == b"endeavour\n"

    mark(project, name)
    with pytest.raises(ShipError, match=rf"the default mark .*: {name} .*qd default <ship>"):
        shipmod.choose(None)
    assert mark_file.read_text() == name
    assert shipmod.choose(str(folder)) == (folder.resolve(), None)


@pytest.mark.parametrize("declaration", ["[ship", "[ship]\ntick = 0", "seats = true"])
def test_invalid_declaration_cannot_replace_a_mark_or_be_used_by_one(project, declaration):
    ship(project, "endeavour")
    broken = ship(project, "defiant")
    (broken / "ship.toml").write_text(declaration)
    mark(project, "endeavour\n")
    mark_file = project / "ships/.default"
    with pytest.raises(ShipError, match=r"defiant has .*invalid declaration"):
        shipmod.mark_default("defiant")
    assert mark_file.read_bytes() == b"endeavour\n"

    mark(project, "defiant\n")
    with pytest.raises(ShipError, match=r"the default mark .*: defiant has .*invalid declaration .*qd default <ship>"):
        shipmod.choose(None)
    assert mark_file.read_bytes() == b"defiant\n"


def test_with_no_mark_the_only_ship_that_is_no_template_is_the_default(project):
    templates(project)
    ship(project, "endeavour")
    (project / "ships" / "notes").mkdir()  # a folder with no ship.toml is no ship
    (project / "ships" / ".hidden").mkdir()
    shutil.copy(DRYDOCK, project / "ships" / ".hidden" / "ship.toml")
    assert shipmod.candidates() == ["endeavour"]
    assert shipmod.default_ship() == ((project / "ships" / "endeavour").resolve(), "only ship")
    assert shipmod.choose(None)[1] == "endeavour"


def test_several_ships_and_no_mark_are_refused_by_name(project):
    templates(project)
    ship(project, "endeavour")
    ship(project, "defiant")
    with pytest.raises(ShipError) as refused:
        shipmod.choose(None)
    assert "defiant, endeavour" in str(refused.value) and "qd default <ship>" in str(refused.value)


@pytest.mark.parametrize("with_templates", [True, False])
def test_no_ship_at_all_is_refused_as_before(project, with_templates):
    if with_templates:
        templates(project)
    with pytest.raises(ShipError, match="no ship named: pass --ship <folder>"):
        shipmod.choose(None)


def test_no_ships_folder_is_refused(tmp_path, monkeypatch):
    for name in ("QD_SHIP", "QD_SEAT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(standing, "PROJECT_DIR", tmp_path / "nowhere")
    with pytest.raises(ShipError, match="no ship named"):
        shipmod.choose(None)


def test_a_mark_naming_a_missing_folder_is_refused_and_never_falls_back(project):
    ship(project, "endeavour")  # the one ship the fallback would choose
    mark(project, "voyager")
    with pytest.raises(ShipError) as refused:
        shipmod.choose(None)
    assert "voyager" in str(refused.value) and "missing" in str(refused.value)
    (project / "ships" / "voyager").mkdir()  # a folder with no ship.toml is as invalid
    with pytest.raises(ShipError, match="voyager"):
        shipmod.choose(None)


@pytest.mark.parametrize("text", ["", "\n", "../endeavour", "ships/endeavour", ".endeavour", "endeavour\n\n",
                                  " endeavour", "endeavour\ndefiant\n", "/abs/endeavour"])
def test_a_mark_that_is_no_single_folder_name_is_refused(project, text):
    ship(project, "endeavour")
    mark(project, text)
    with pytest.raises(ShipError, match="does not hold one ship folder name"):
        shipmod.choose(None)


def test_named_ship_and_qd_ship_win_over_the_mark(project, monkeypatch):
    ship(project, "endeavour")
    other = ship(project, "defiant")
    mark(project, "endeavour")
    assert shipmod.choose(str(other)) == (other.resolve(), None)
    monkeypatch.setenv("QD_SHIP", str(other))
    assert shipmod.choose(None) == (other.resolve(), None)


def test_a_seat_never_reads_the_mark(project, monkeypatch):
    ship(project, "endeavour")
    mark(project, "endeavour")
    monkeypatch.setenv("QD_SEAT", "worker-1")
    with pytest.raises(ShipError, match="a seat runs with its own QD_SHIP"):
        shipmod.choose(None)
    mark(project, "../broken")  # not even a broken mark is read
    with pytest.raises(ShipError, match="a seat runs with its own QD_SHIP"):
        shipmod.choose(None)
    own = ship(project, "defiant")
    monkeypatch.setenv("QD_SHIP", str(own))
    assert shipmod.choose(None) == (own.resolve(), None)


def test_mark_default_writes_only_the_mark(project):
    ship(project, "endeavour")
    before = sorted(p.relative_to(project) for p in project.rglob("*"))
    assert shipmod.mark_default("endeavour") == (project / "ships" / "endeavour").resolve()
    assert (project / "ships" / ".default").read_text() == "endeavour\n"
    after = sorted(p.relative_to(project) for p in project.rglob("*"))
    assert set(after) - set(before) == {Path("ships/.default")}  # law: one-folder
    for bad in ("voyager", "../endeavour", ".default", ""):
        with pytest.raises(ShipError):
            shipmod.mark_default(bad)
    assert (project / "ships" / ".default").read_text() == "endeavour\n"


# -- the command line -------------------------------------------------------------------


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_a_verb_on_the_default_ship_says_so_and_hands_over_the_same_folder(project, capsys):
    folder = ship(project, "endeavour").resolve()
    code, out, _ = run(capsys, "ledger")
    assert code == 0
    assert out.splitlines()[0].endswith(" (default ship: endeavour)")
    assert project.handed == [(folder, ["--ship", str(folder), "ledger"], str(folder))]
    assert cli.HANDED not in os.environ, "nothing launched afterwards inherits it"


def test_the_json_reading_carries_the_default_ship_as_a_field(project, capsys):
    ship(project, "endeavour")
    code, out, _ = run(capsys, "--json", "ledger")
    data = json.loads(out)
    assert code == 0 and data["default_ship"] == "endeavour"


def test_a_named_ship_is_not_called_the_default(project, capsys, monkeypatch):
    folder = ship(project, "endeavour")
    mark(project, "endeavour")
    for argv in (("--ship", str(folder), "ledger"), ("--json", "--ship", str(folder), "ledger")):
        code, out, _ = run(capsys, *argv)
        assert code == 0 and "default ship" not in out and "default_ship" not in out
    monkeypatch.setenv("QD_SHIP", str(folder))
    code, out, _ = run(capsys, "ledger")
    assert code == 0 and "default ship" not in out
    assert all(handed[2] is None for handed in project.handed)


def test_the_ships_own_copy_names_the_default_it_was_handed(project, capsys, monkeypatch):
    folder = ship(project, "endeavour").resolve()
    other = ship(project, "defiant").resolve()
    monkeypatch.setenv(cli.HANDED, str(folder))
    code, out, _ = run(capsys, "--ship", str(folder), "ledger")
    assert code == 0 and out.splitlines()[0].endswith("(default ship: endeavour)")
    assert cli.HANDED not in os.environ
    monkeypatch.setenv(cli.HANDED, str(other))  # handed a different folder: never said
    code, out, _ = run(capsys, "--ship", str(folder), "ledger")
    assert code == 0 and "default ship" not in out


def test_a_refusal_on_the_default_ship_names_it(project, capsys):
    ship(project, "endeavour")
    code, _, err = run(capsys, "brief", "no-such-seat")
    assert code == 2 and err.rstrip().endswith("(default ship: endeavour)")


def test_several_ships_refuse_a_plain_verb_by_name(project, capsys):
    ship(project, "endeavour")
    ship(project, "defiant")
    code, _, err = run(capsys, "ledger")
    assert code == 2 and "defiant, endeavour" in err and "qd default <ship>" in err
    assert project.handed == []


def test_a_seat_without_its_own_ship_is_refused_and_never_handed_the_default(project, capsys, monkeypatch):
    ship(project, "endeavour")
    mark(project, "endeavour")
    monkeypatch.setenv("QD_SEAT", "worker-1")
    code, _, err = run(capsys, "ledger")
    assert code == 2 and "a seat runs with its own QD_SHIP" in err and "default ship:" not in err
    assert project.handed == []


def test_verbs_that_act_on_no_ship_never_take_the_default(project, capsys):
    ship(project, "endeavour")
    code, out, _ = run(capsys, "laws")
    assert code == 0 and "(default ship:" not in out and project.handed == []
    parser = cli.build_parser()
    for argv in (["laws"], ["drill"], ["default"], ["controls", "--harness", "claude"]):
        assert not cli.needs_ship(parser.parse_args(argv)), argv
    for argv in (["status"], ["up"], ["sync"], ["doctor"], ["check", "claude"], ["post", "order", "x"]):
        assert cli.needs_ship(parser.parse_args(argv)), argv


# -- qd default -------------------------------------------------------------------------


def test_qd_default_prints_the_default_and_how_it_was_chosen(project, capsys):
    ship(project, "endeavour")
    code, out, _ = run(capsys, "default")
    assert code == 0 and out.startswith("default ship: endeavour · the only ship under ships/, with no mark")
    ship(project, "defiant")
    code, out, _ = run(capsys, "default")
    assert code == 1 and out.startswith("no default ship · ") and "defiant, endeavour" in out
    code, out, _ = run(capsys, "default", "defiant")
    assert code == 0 and out.startswith("default ship: defiant · now marked in ships/.default")
    code, out, _ = run(capsys, "--json", "default")
    data = json.loads(out)
    assert code == 0 and (data["ship"], data["how"], data["marked"]) == ("defiant", "mark", False)
    assert project.handed == []


def test_qd_default_takes_the_folder_by_path_too_and_refuses_a_missing_one(project, capsys):
    ship(project, "endeavour")
    code, out, _ = run(capsys, "default", str(project / "ships" / "endeavour"))
    assert code == 0 and (project / "ships" / ".default").read_text() == "endeavour\n"
    code, _, err = run(capsys, "default", "voyager")
    assert code == 2 and "voyager" in err
    assert (project / "ships" / ".default").read_text() == "endeavour\n"


def test_qd_default_only_reads_or_changes_the_invoking_checkout(project, capsys, monkeypatch):
    ship(project, "endeavour")
    other = ship(project, "defiant")
    mark(project, "endeavour\n")
    monkeypatch.setenv("QD_SHIP", str(other))
    monkeypatch.chdir(other)
    code, out, err = run(capsys, "default")
    assert code == 0 and not err and out.startswith("default ship: endeavour")
    assert "QD_SHIP is set in this shell and wins over the default ship" in out
    assert (project / "ships/.default").read_bytes() == b"endeavour\n"
    code, out, err = run(capsys, "default", "defiant")
    assert code == 0 and not err and out.startswith("default ship: defiant")
    assert (project / "ships/.default").read_bytes() == b"defiant\n"
    monkeypatch.setenv("QD_SEAT", "worker-1")
    _, out, _ = run(capsys, "default")
    assert "this shell is a seat" in out
    assert project.handed == []


@pytest.mark.parametrize("argument", [[], ["defiant"]])
def test_qd_default_refuses_the_global_ship_option(project, capsys, argument):
    folder = ship(project, "endeavour")
    ship(project, "defiant")
    mark(project, "endeavour\n")
    assert run(capsys, "--ship", str(folder), "default", *argument) == (
        2, "", "qd: name the ship as the argument\n")
    assert (project / "ships/.default").read_bytes() == b"endeavour\n"
    assert project.handed == []


# -- installed copies -------------------------------------------------------------------


def synthetic_copy(folder, capable):
    """A usable recorded copy with either the current package or an older stand-in."""
    declaration = folder / "ship.toml"
    declaration.write_text(declaration.read_text().replace('code = "checkout"\n', ''))
    venv = folder / "runtime/venv"
    package = venv / "lib/python3/site-packages/quarterdeck"
    if capable:
        shutil.copytree(Path(cli.__file__).parent, package, ignore=shutil.ignore_patterns("__pycache__"))
        body = (
            "import sys\n"
            "from pathlib import Path\n"
            f"sys.path.insert(0, {str(package.parent)!r})\n"
            "sys.executable = str(Path(sys.argv[0]).with_name('python'))\n"
            "sys.prefix = str(Path(sys.executable).parents[1])\n"
            "from quarterdeck import main, presentation\n"
            "presentation.published_reading = lambda *args, **kwargs: ('synthetic status', None)\n"
            "raise SystemExit(main())\n"
        )
    else:
        package.mkdir(parents=True)
        (package / "cli.py").write_text("# older CLI\n")
        body = (
            "import json, os, sys\n"
            "assert 'QD_DEFAULT_SHIP' not in os.environ\n"
            f"assert sys.argv[1:3] == ['--ship', {str(folder)!r}]\n"
            "print(json.dumps({'ship': sys.argv[2]}) if '--json' in sys.argv else 'historical output')\n"
        )
    (venv / "bin").mkdir()
    (venv / "bin/python").symlink_to(sys.executable)
    qd = venv / "bin/qd"
    qd.write_text(f"#!{sys.executable}\n" + body)
    qd.chmod(0o755)
    laws = venv / "share/quarterdeck/standing-orders/laws.md"
    laws.parent.mkdir(parents=True)
    laws.write_text("- `example` synthetic law\n")
    (folder / "runtime/installed.json").write_text(json.dumps(
        dict(commit="a" * 40, ref="copy", at="2026-11-17T00:00:00+00:00")))
    return qd


@pytest.mark.parametrize("capable", [False, True])
def test_installed_handoff_keeps_the_pinned_folder_and_calls_out_old_copies(project, tmp_path, capable):
    folder = ship(project, "endeavour").resolve()
    ship(project, "defiant")
    mark(project, "endeavour\n")
    synthetic_copy(folder, capable)
    # This subprocess starts the ordinary checkout CLI and really execs the recorded synthetic copy.
    caller = (
        "import sys; "
        f"sys.path.insert(0, {str(Path(cli.__file__).parents[1])!r}); "
        "from pathlib import Path; from quarterdeck import cli, standing; "
        f"standing.PROJECT_DIR = Path({str(project)!r}); "
        "raise SystemExit(cli.main())"
    )
    warning = (f"qd: acting on the default ship endeavour ({folder}); "
               "its copy is older than the default-ship annotation\n")
    for as_json in (False, True):
        argv = ["--json", "ledger"] if as_json else ["ledger"]
        result = subprocess.run([sys.executable, "-c", caller, *argv], cwd=tmp_path,
                                capture_output=True, text=True, check=True)
        assert result.stderr == ("" if capable else warning)
        if as_json:
            data = json.loads(result.stdout)
            if capable:
                assert data["default_ship"] == "endeavour"
            else:
                assert data == {"ship": str(folder)}
        elif capable:
            assert result.stdout.splitlines()[0].endswith("(default ship: endeavour)")
        else:
            assert result.stdout == "historical output\n"

    if capable:
        result = subprocess.run([sys.executable, "-c", caller, "status", "--line"],
                                capture_output=True, text=True, check=True)
        assert (result.stdout, result.stderr) == ("synthetic status (default ship: endeavour)\n", "")
    else:
        named = subprocess.run([sys.executable, "-c", caller, "--ship", str(folder), "ledger"],
                               capture_output=True, text=True, check=True)
        assert (named.stdout, named.stderr) == ("historical output\n", "")


@pytest.mark.parametrize('layout', ['separate', 'office-only', 'embedded'])
def test_desk_request_installed_copy_and_operator_reply_round_trip(project, tmp_path, layout):
    office = project / "office"
    office.mkdir()
    if layout != 'embedded':
        template = DRYDOCK.read_text()
        office_text = template.split("[seats.captain]", 1)[0] + "[seats.desk]" + template.split(
            "[seats.desk]", 1)[1].split("[seats.worker-1]", 1)[0]
        office_text = office_text.replace('[ship]\n', '[ship]\nkind = "office"\n', 1)
        office_text = office_text.replace('operator = "operator"', 'operator = "Office operator"')
        office_text = office_text.replace('[sessions.bridge]\ntitle = "Bridge"\n', '')
        office_text = office_text.replace('[sessions.crew]\ntitle = "Crew"\n', '')
        (office / "ship.toml").write_text(office_text)
    origin = office
    if layout != 'office-only':
        origin = ship(project, "endeavour").resolve()
        declaration = origin / "ship.toml"
        crew_text = declaration.read_text().replace('operator = "operator"', 'operator = "Crew operator"')
        if layout == 'separate':
            crew_text = crew_text.replace('human = true', 'human = false')
        declaration.write_text(crew_text)
        mark(project, "endeavour\n")
    desk_folder = origin if layout == 'embedded' else office
    synthetic_copy(origin, capable=True)
    caller = (
        "import sys; "
        f"sys.path.insert(0, {str(Path(cli.__file__).parents[1])!r}); "
        "from pathlib import Path; from quarterdeck import cli, desk_request, standing; "
        f"standing.PROJECT_DIR = Path({str(project)!r}); "
        "desk_request.read_stdin = lambda *a: sys.exit('the checkout read stdin before handoff'); "
        "raise SystemExit(cli.main())"
    )
    bin_dir = tmp_path / "shell bin"
    bin_dir.mkdir()
    qd = bin_dir / "qd"
    qd.write_text(f"#!{sys.executable}\n{caller}\n")
    qd.chmod(0o755)
    env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]}
    ordinary_shell = tmp_path / "small project"
    ordinary_shell.mkdir()
    result = subprocess.run(["qd", "Review this file"], input="the attachment\n", cwd=ordinary_shell,
                            env=env, capture_output=True, text=True, check=True)
    assert result.stderr == '' and len(result.stdout.splitlines()) == 1
    proof, command = result.stdout.strip().rsplit(" · see the reply with ", 1)
    flags = ["--ship", str(office)] if layout == 'office-only' else []
    assert shlex.split(command) == ["qd", *flags, "inbox", "--wait"]

    source, destination = shipmod.load(origin), shipmod.load(desk_folder)
    store, desk_store = post.Store(source.runtime), post.Store(destination.runtime)
    assert post.file_drops(store) == []
    order, = store.all()
    assert proof.startswith(
        f"Filed {order.id} for desk@{desk_folder.name} · delivery waits for the {origin.name} service · ")
    assert shlex.split(proof.split('start it with ', 1)[1]) == ['qd', *flags, 'up']
    assert order.body == f"Working directory: {ordinary_shell}\n\nReview this file\n\nAttached stdin:\nthe attachment\n"
    assert order.subject is None
    if layout == 'separate':
        assert (order.sender, order.to) == ('crew operator', 'desk@office')
        assert fleet_post.run(source, store) == [] and fleet_post.run(destination, desk_store) == []
        received, = desk_store.open_for('desk')
        assert (received.id, received.sender, received.to) == (order.id, 'crew operator@endeavour', 'desk')
    else:
        assert (order.sender, order.to) == (source.operator.lower(), 'desk')
        assert desk_store.open_for('desk') == [order]

    subprocess.run(['qd', 'post', 'report', '--re', order.short, 'Finished the review.'], cwd=ordinary_shell,
                   env={**env, 'QD_SHIP': str(desk_folder), 'QD_SEAT': 'desk'}, capture_output=True, text=True, check=True)
    assert post.file_drops(desk_store) == []
    if layout == 'separate':
        assert fleet_post.run(destination, desk_store) == [] and fleet_post.run(source, store) == []
    reply = store.closing_report(order)
    assert reply.to == source.operator.lower() and reply.re == order.id
    if layout == 'separate':
        for message in order, reply:
            relative = Path('post/store', f'{message.id}.json')
            assert (source.runtime / relative).read_bytes() == (destination.runtime / relative).read_bytes()

    result = subprocess.run(shlex.split(command), stdin=subprocess.DEVNULL, cwd=ordinary_shell,
                            env=env, capture_output=True, text=True, check=True)
    assert result.stderr == ''
    assert f'report from {reply.sender} · {reply.short} · re {order.short}' in result.stdout
    assert 'Finished the review.' in result.stdout


def test_the_installed_copy_requires_a_named_ship_and_has_no_default_mark(project, tmp_path):
    folder = ship(project, "endeavour")
    other = ship(project, "defiant")
    qd = synthetic_copy(folder, capable=True)
    mark(project, "endeavour\n")
    refusal = "qd: this is a ship's own copy; name its ship with --ship or QD_SHIP\n"

    def invoke(*argv, **env):
        return subprocess.run([str(qd), *argv], cwd=project, capture_output=True, text=True,
                              env={**os.environ, **env})

    for argv in (("ledger",), ("default",), ("default", "defiant")):
        result = invoke(*argv)
        assert (result.returncode, result.stdout, result.stderr) == (2, "", refusal)
    for argv in (("default",), ("default", "defiant")):
        result = invoke(*argv, QD_SHIP=str(other))
        assert (result.returncode, result.stdout, result.stderr) == (2, "", refusal)
    assert invoke("ledger", QD_SHIP=str(folder)).returncode == 0
    result = invoke("--ship", str(folder), "ledger", QD_SHIP=str(tmp_path / "absent"))
    assert result.returncode == 0 and "(default ship:" not in result.stdout
    assert invoke("laws").returncode == 0
    assert (project / "ships/.default").read_bytes() == b"endeavour\n"


# -- doctor -----------------------------------------------------------------------------


def test_doctor_warns_of_qd_ship_in_a_shell_that_is_no_seat(project, monkeypatch):
    folder = ship(project, "endeavour")
    assert doctor.shell_check() is None
    monkeypatch.setenv("QD_SHIP", str(folder))
    warning = doctor.shell_check()
    assert warning.passed, "a warning, never a failure"
    assert warning.detail.startswith("warning: QD_SHIP is set in a shell that is no seat")
    assert "default ship replaces it" in warning.detail and "start-up file" in warning.detail
    monkeypatch.setenv("QD_SEAT", "worker-1")
    assert doctor.shell_check() is None


def test_doctor_lists_the_warning_among_its_checks(project, monkeypatch):
    folder = ship(project, "endeavour")
    monkeypatch.setenv("QD_SHIP", str(folder))
    found = doctor.checks(shipmod.load(folder))
    assert [c.name for c in found][:2] == ["ship file", "QD_SHIP in this shell"]


def test_git_never_tracks_the_mark():
    """The mark lies inside the project folder, where the framework's repository ignores it."""  # law: one-folder
    root = Path(__file__).resolve().parents[2]
    done = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", "--no-index", "ships/.default"])
    assert done.returncode == 0, "ships/.default must be ignored by the framework repository"
