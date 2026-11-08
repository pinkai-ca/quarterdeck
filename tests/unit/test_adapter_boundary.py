"""Generic consumers obtain program facts from the adapter rather than a second table."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import json
import re
import sys

from quarterdeck import credential, harness, quota, record_owner
from quarterdeck.harness.base import Input, ship_template, trimmed
from quarterdeck.harness.claude import ClaudeHarness
from quarterdeck.harness.codex import CodexHarness
from quarterdeck.harness.fake import FakeHarness
from quarterdeck.harness.pi import PiHarness
from quarterdeck.ship import Pool


def test_every_saved_program_binding_is_resolved_by_the_adapter():
    for name in harness.NAMES:
        adapter = harness.get(name)
        argv = [adapter.program] if not adapter.synthetic else list(adapter.launch(
            SimpleNamespace(settings={}, model=None, effort=None), Pool("synthetic", name), Path("home"), Path("brief"), None, None).argv)
        assert record_owner.launch_reader(SimpleNamespace(argv=argv)) == name
    assert FakeHarness().program == sys.executable
    assert not CodexHarness().reads_usage and CodexHarness().headline_window is None
    assert record_owner.launch_reader(SimpleNamespace(argv=["unrecognized-synthetic-program"])) is None


def test_credential_shape_is_selected_by_pool_not_guessed_from_contents(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"claudeAiOauth": {"refreshToken": "x", "expiresAt": 1900000000000}}))
    assert CodexHarness().freshness(path) is None
    assert ClaudeHarness().freshness(path)[0] == 1900000000
    path.write_text(json.dumps({"tokens": {"refresh_token": "x"}}))
    assert CodexHarness().freshness(path)[0] == 0
    assert ClaudeHarness().freshness(path) is None
    assert credential.pool_file(Pool("endpoint", "endpoint", endpoint="http://synthetic.invalid/v1")) is None


def test_shared_home_and_box_helpers_preserve_settings_and_meaningful_whitespace(tmp_path):
    (tmp_path / "config.toml").write_text('title = "synthetic"\n[profile]\nnumber = 3\n')
    (tmp_path / "settings.json").write_text('{"nested":{"list":[1,2]}}')
    assert ship_template(tmp_path, "config.toml") == {"title": "synthetic", "profile": {"number": 3}}
    assert ship_template(tmp_path, "settings.json") == {"nested": {"list": [1, 2]}}
    assert ship_template(None, "config.toml") == {}
    assert trimmed(["a  b ", "", " c ", "   ", ""]) == ["a  b ", "", " c "]


def test_steered_and_consumed_inputs_keep_their_own_timestamps(tmp_path):
    at = datetime(2030, 1, 1, tzinfo=timezone.utc)
    for name, adapter, shape in [
        ("codex", CodexHarness(), lambda text: {"type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}}),
        ("pi", PiHarness(), lambda text: {"type": "message", "message": {"role": "user", "content": text}}),
    ]:
        path = tmp_path / (name + ".jsonl")
        path.write_text("\n".join(json.dumps(dict(shape(text), timestamp=(at + timedelta(seconds=i)).isoformat()))
                                  for i, text in enumerate(["first", "steered", "later"])))
        assert adapter.inputs(path) == [Input("first", at),
            Input("steered", at + timedelta(seconds=1), "consumed", at + timedelta(seconds=1)),
            Input("later", at + timedelta(seconds=2), "consumed", at + timedelta(seconds=2))]


def test_production_program_knowledge_stays_in_adapters():
    root = Path(harness.__file__).parent.parent
    names = re.compile(r"\b(?:claude|codex|antigravity|opencode|pi|fake)\b", re.I)
    offenders = [p.relative_to(root) for p in root.rglob("*.py")
                 if "harness" not in p.relative_to(root).parts and names.search(p.read_text())]
    assert offenders == [], f"program knowledge outside harness/: {offenders}"


def test_attempt_acceptance_does_not_rename_its_persisted_queue_result():
    from quarterdeck.post import Attempt

    at = datetime(2030, 1, 1, tzinfo=timezone.utc).isoformat()
    attempt = Attempt("synthetic-message", "worker", at, result="queued")
    assert attempt.accepted and attempt.result == "queued"
    assert not Attempt("synthetic-message", "worker", at, result="pending").accepted
