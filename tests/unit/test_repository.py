"""Two guards on the repository itself: what may never be tracked, and the laws' carriers."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from quarterdeck import standing

ROOT = Path(__file__).resolve().parents[2]
UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
SECRETS = re.compile(r"sk-ant-|\beyJ[A-Za-z0-9_-]{20,}|\"accessToken\": \"[^\"]{8,}|\"refreshToken\": \"[^\"]{8,}")
TEXT = {".py", ".md", ".toml", ".txt", ".json", ".cfg", ".gitignore"}


def tracked() -> list[Path]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True, check=True).stdout
    return [ROOT / line for line in out.splitlines() if line]


def test_nothing_from_a_runtime_is_tracked():  # law: nothing-leaks
    files = tracked()
    assert files, "run from the repository"
    assert not [p for p in files if "/runtime/" in str(p)], "a runtime is fenced, never tracked"
    ignored = (ROOT / ".gitignore").read_text().splitlines()
    assert "ships/*/runtime/" in ignored and "ships/*" in ignored
    assert "!ships/drydock/" in ignored and "!ships/default/" in ignored and "work/" in ignored
    assert ".testruns/" in ignored, "test runs, results and test sockets are never tracked"
    leaks = {}
    for path in files:
        if path.suffix not in TEXT and path.name != ".gitignore" or path == Path(__file__):
            continue
        text = path.read_text(errors="replace")
        if path.suffix in (".py", ".txt", ".md") and "docs/" not in str(path) and UUID.search(text):
            leaks[str(path.relative_to(ROOT))] = "a session or message id"
        if SECRETS.search(text):
            leaks[str(path.relative_to(ROOT))] = "a credential pattern"
    assert leaks == {}, leaks


def test_every_law_is_carried_in_code_and_tried_by_a_test_and_every_tag_names_a_law():
    laws = {law.id for law in standing.laws()}
    found = standing.carriers(ROOT)
    assert set(found) <= laws, f"tags naming no law: {set(found) - laws}"
    uncarried = [law for law in laws if not any(str(p).startswith("src/") for p, _ in found.get(law, []))]
    untried = [law for law in laws if not any(str(p).startswith("tests/") for p, _ in found.get(law, []))]
    assert uncarried == [], f"laws no code carries: {uncarried}"
    assert untried == [], f"laws no test tries to break: {untried}"
    table = standing.laws_table(ROOT)
    assert len(table) == 2 * len(laws) and "carried nowhere" not in "\n".join(table)
