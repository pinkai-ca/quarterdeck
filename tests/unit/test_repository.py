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
KINDS = "ord|rep|que|ans|ack"
# A post id, long (kind-yyyymmdd-hhmm-seat-xxxx) or short (kind-xxxx), whole words only: record-first is no id.
POST_ID = re.compile(rf"\b(?:{KINDS})-(?:[0-9]{{8}}-[0-9]{{4}}-[A-Za-z0-9_.-]+?-)?[0-9a-f]{{4}}\b")
# Exact values only, never a pattern. The documented examples may stand anywhere they are explained.
EXAMPLES = {"ord-7f3a", "ord-20261106-2101-desk-7f3a", "ord-9c21", "ord-1a2b"}
# Invented values that tests and their scrubbed fixtures type, read or match; under tests/ only.
SAMPLES = {"ord-0000", "ord-0002", "ord-1234", "ord-5678", "ord-7777", "ord-7f3b", "ord-9abc", "ord-a1b2", "ord-ab12",
           "ord-abcd", "ord-70e2", "ans-1111", "ans-a1b2", "ans-bbbb", "ans-cccc", "que-2222", "que-9999", "rep-6bee",
           "ord-19990101-0000-lead-0000", "ord-20261109-2200-pike-0d0a", "ack-20261109-2201-worker-0d0b",
           "ord-20300101-0000-captain-abcd", "ack-20300101-0000-worker-abcd", "rep-20300101-0000-worker-cdef",
           "que-20990101-0000-operator-0000"}


def post_ids(text: str, where: str) -> list[str]:
    """The post ids in ``text`` that may not stand at ``where``, a repository path or "commit message"."""
    allowed = EXAMPLES | (SAMPLES if where.startswith("tests/") else set())
    return [found for found in POST_ID.findall(text) if found not in allowed]


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


def test_no_post_id_from_a_runtime_is_tracked_or_in_a_commit_message():  # law: nothing-leaks
    found = {}
    for path in tracked():
        if path == Path(__file__) or not path.is_file():
            continue
        data = path.read_bytes()
        try:
            text = data.decode()
        except UnicodeDecodeError:
            continue  # not text
        where = str(path.relative_to(ROOT))
        if "\0" not in text and (ids := post_ids(text, where)):
            found[where] = sorted(set(ids))
    messages = subprocess.run(["git", "-C", str(ROOT), "log", "--format=%H%x00%B%x01"], capture_output=True,
                              text=True, check=True).stdout
    for entry in filter(str.strip, messages.split("\x01")):
        commit, _, body = entry.strip().partition("\0")
        if ids := post_ids(body, "commit message"):
            found[f"commit {commit[:12]}"] = sorted(set(ids))
    assert found == {}, f"runtime post ids leave the runtime (law: nothing-leaks): {found}"


def test_the_post_id_guard_reads_both_forms_and_no_ordinary_word():
    long, short = "-".join(["que", "20261113", "0320", "worker1", "9c8a"]), "-".join(["ord", "c304"])
    assert post_ids(f"see {long}.", "src/x.py") == [long]
    assert post_ids(f"(per {short})", "src/x.py") == [short]
    assert post_ids(f"{short}: reworded", "commit message") == [short]
    for kind in ("ord", "rep", "que", "ans", "ack"):
        assert post_ids(f"x {kind}-{'beef'} y", "docs/x.md") == [f"{kind}-beef"]
    assert post_ids("record-first, rep-orted, ack-nowledged, order-1234, ord-12345, ord-12g4, word-c304", "src/x.py") == []
    assert post_ids("qd > order from desk · ord-1234", "tests/unit/x.py") == []
    assert post_ids("qd > order from desk · ord-1234", "src/x.py") == ["ord-1234"], "a test sample stays in tests/"
    assert post_ids("such as ord-7f3a", "docs/DESIGN.md") == []


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
