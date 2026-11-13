"""Scripted fake missions prove transport and artifacts, not an agent's judgment."""

import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time

import pytest

from quarterdeck import post, seat, service, ship
from quarterdeck.tmux import Tmux


ROOT = Path(__file__).resolve().parents[2]

# Written only into this test's scratch mission. Each stage is invoked as a
# tool by a fake seat, using ordinary qd posts for the three-role handoffs.
STAGES = r'''
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys

mission = Path(__file__).parent
config = json.loads((mission / "config.json").read_text())
repo = Path(config["repository"])
worktree = mission / "worktree"


def git(folder, *args):
    return subprocess.check_output([
        "git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
        "-c", "user.name=Shuttle Fixture", "-c", "user.email=shuttle@example.invalid",
        "-C", str(folder), *args,
    ], text=True).strip()


def save(name, value):
    (mission / name).write_text(json.dumps(value, sort_keys=True) + "\n")


def read(name):
    return json.loads((mission / name).read_text())


def digest(name):
    return hashlib.sha256((mission / name).read_bytes()).hexdigest()


stage = sys.argv[1]
if stage == "engineer":
    git(repo, "worktree", "add", "-b", "mission-fixture", str(worktree), "main")
    (worktree / "sentence.txt").write_text("A reviewed sentence.\n")
    git(worktree, "add", "sentence.txt")
    git(worktree, "commit", "-m", "Revise the example sentence")
    candidate = dict(base=config["base"], tip=git(worktree, "rev-parse", "HEAD"),
                     tree=git(worktree, "rev-parse", "HEAD^{tree}"), branch="mission-fixture")
    check = [sys.executable, "-c",
             "from pathlib import Path; assert Path('sentence.txt').read_text() == 'A reviewed sentence.' + chr(10)"]
    result = subprocess.run(check, cwd=worktree, capture_output=True, text=True)
    save("tests.json", dict(argv=check, exit=result.returncode, stdout=result.stdout,
                            stderr=result.stderr, tip=candidate["tip"], tree=candidate["tree"]))
    assert result.returncode == 0, result.stderr
    save("candidate.json", candidate)
    (mission / "engineering-report.txt").write_text(json.dumps(candidate) + "\nTests exit 0.\n")
elif stage == "sign":
    candidate, tests = read("candidate.json"), read("tests.json")
    assert git(worktree, "rev-parse", "HEAD") == candidate["tip"]
    assert git(worktree, "rev-parse", "HEAD^{tree}") == candidate["tree"]
    assert tests["exit"] == 0 and tests["tip"] == candidate["tip"] and tests["tree"] == candidate["tree"]
    signed = dict(candidate, tests_sha256=digest("tests.json"), signer="first-officer")
    save("signature.json", signed)
    (mission / "signature-report.txt").write_text("Signed exact candidate: " + json.dumps(signed) + "\n")
elif stage == "finish":
    signed = read("signature.json")
    if config["destination"]:
        assert git(repo, "rev-parse", "main") == signed["base"]
        git(repo, "merge", "--ff-only", signed["tip"])
        assert git(repo, "rev-parse", "main") == signed["tip"]
        outcome = "signed and fast-forwarded to main"
    else:
        outcome = "signed on mission-fixture; destination left to operator"
    merge = "git -C <operator-chosen-target-worktree> merge --ff-only " + shlex.quote(signed["tip"])
    final = dict(signed, outcome=outcome, signature_sha256=digest("signature.json"))
    if not config["destination"]:
        final["merge_command"] = merge
    (mission / "officer-report.txt").write_text(json.dumps(final) + "\n")
    (mission / "captain-report.txt").write_text("Scripted captain completion: " + json.dumps(final) + "\n")
else:
    raise ValueError("unknown scripted stage")
'''


def git(folder, *args):
    return subprocess.check_output([
        "git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
        "-c", "user.name=Shuttle Fixture", "-c", "user.email=shuttle@example.invalid",
        "-C", str(folder), *args,
    ], text=True).strip()


def arrive(controller, predicate):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        controller.tick()
        found = predicate()
        if found:
            return found
        time.sleep(0.03)
    raise AssertionError("scripted shuttle did not finish its expected post stage")


@pytest.mark.parametrize("destination", [True, False], ids=["named-branch", "branchless"])
def test_fake_shuttle_carries_mission_to_exact_signed_result(tmp_path, destination):
    folder = tmp_path / "shuttle-fixture"
    shutil.copytree(ROOT / "ships" / "shuttle", folder)
    text = (folder / "ship.toml").read_text()
    text = re.sub(r'\[pools\.anthropic\].*?(?=\[sessions\.)',
                  '[pools.fake]\nprovider = "synthetic"\n\n', text, flags=re.S)
    text = re.sub(r'harness = "(?:claude|codex)"', 'harness = "fake"', text)
    text = re.sub(r'pool = "(?:anthropic|openai)"', 'pool = "fake"', text)
    text = re.sub(r'model = "[^"]+"', 'model = "fake-1"', text)
    (folder / "ship.toml").write_text(text)
    crew = ship.load(folder)
    repository = tmp_path / "example-repository"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    (repository / "sentence.txt").write_text("An initial sentence.\n")
    git(repository, "add", "sentence.txt")
    git(repository, "commit", "-m", "Add the example sentence")
    base = git(repository, "rev-parse", "HEAD")
    mission = crew.runtime / "missions" / "ordinary-fixture"
    mission.mkdir(parents=True)
    (mission / "config.json").write_text(json.dumps(dict(repository=str(repository), base=base,
                                                        destination=destination)))
    stages = mission / "stages.py"
    stages.write_text(STAGES)
    python = shlex.quote(sys.executable)
    script = shlex.quote(str(stages))

    def command(stage):
        return f"{python} {script} {stage}"

    engineering = mission / "engineering.txt"
    engineering.write_text("Scripted engineer, change the sentence and run its check.\n$ " +
        command("engineer") + " && qd post report --re {this} --body-file " +
        shlex.quote(str(mission / "engineering-report.txt")) + "\n")
    officer = mission / "officer.txt"
    officer.write_text("Scripted independent check, exact signature, and authorized outcome.\n$ " +
        command("sign") + " && qd post answer --re {this} --body-file " +
        shlex.quote(str(mission / "signature-report.txt")) + " && " + command("finish") +
        " && qd post report --re {this} --body-file " +
        shlex.quote(str(mission / "officer-report.txt")) + "\n")

    terminal = Tmux.private(tmp_path / "tmux")
    controller = None
    try:
        seat.bring_up(crew, terminal)
        controller = service.Service(crew, terminal)
        store = post.Store(crew.runtime)
        root = store.new("order", "operator", "captain", "Scripted repository mission.\n$ " +
            "qd post order --to engineer --re {this} --body-file " + shlex.quote(str(engineering)) + "\n")
        authored = arrive(controller, lambda: next((m for m in store.all()
            if m.kind == "order" and m.to == "engineer" and m.re == root.id), None))
        engineering_report = arrive(controller, lambda: store.closing_report(authored))
        candidate = json.loads((mission / "candidate.json").read_text())
        # The test supplies this review; the fake does not interpret its card.
        store.new("answer", "operator", "captain", "Scripted review of " + candidate["tip"] +
            ". Ask the first officer.\n$ qd post order --to first-officer --re " + root.id +
            " --body-file " + shlex.quote(str(officer)) + "\n", re=engineering_report.id)
        signature_order = arrive(controller, lambda: next((m for m in store.all()
            if m.kind == "order" and m.to == "first-officer" and m.re == root.id), None))
        officer_report = arrive(controller, lambda: store.closing_report(signature_order))
        signatures = [m for m in store.replies(signature_order)
                      if m.kind == "answer" and m.sender == "first-officer"]
        assert len(signatures) == 1
        assert candidate["tip"] in signatures[0].body and candidate["tree"] in signatures[0].body
        store.new("answer", "operator", "captain", "Scripted final report after signature.\n$ " +
            "qd post report --re " + root.id + " --body-file " +
            shlex.quote(str(mission / "captain-report.txt")) + "\n", re=officer_report.id)
        final = arrive(controller, lambda: store.closing_report(root))
        signed = json.loads((mission / "signature.json").read_text())
        assert signed["tip"] == candidate["tip"] and signed["tree"] == candidate["tree"]
        assert candidate["tip"] in final.body and candidate["tree"] in final.body
        assert hashlib.sha256((mission / "signature.json").read_bytes()).hexdigest() in final.body
        assert git(repository, "rev-parse", "mission-fixture") == candidate["tip"]
        assert git(repository, "rev-parse", "main") == (candidate["tip"] if destination else base)
        if not destination:
            assert "git -C <operator-chosen-target-worktree> merge --ff-only " + candidate["tip"] in final.body
    finally:
        try:
            seat.bring_down(crew, terminal)
        finally:
            if controller is not None:
                controller.ledger.close()
            assert terminal.close(), terminal.held
