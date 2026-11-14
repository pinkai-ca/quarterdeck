"""What qd post says on success, and the guard against posting the same report, answer or ack twice.

A hand on a local model took the old answer for a failure and filed one report 23 times in 23 seconds.
The answer now starts with plain words that nothing more is to be done. A report, answer or ack that
the same seat already posted, with the same re and a byte-identical body, is not posted again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quarterdeck import cli, post, ship as shipmod


def qd(capsys, drydock: Path, *argv: str) -> tuple[int, str]:
    code = cli.main(["--ship", str(drydock), "post", *argv])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    return code, captured.out.strip()


@pytest.fixture
def operator(monkeypatch):
    monkeypatch.delenv("QD_SEAT", raising=False)


def stored(drydock: Path) -> list[post.Message]:
    store = post.Store(shipmod.load(drydock).runtime)
    post.file_drops(store)
    return store.all()


def drops(drydock: Path, seat: str) -> list[Path]:
    return sorted((drydock / "runtime" / "post" / "drop" / seat).glob("*.json"))


def an_order(capsys, drydock: Path, to: str = "worker-1", body: str = "build it") -> post.Message:
    qd(capsys, drydock, "order", "--to", to, body)
    return stored(drydock)[-1]


@pytest.mark.parametrize("kind", ["order", "question", "report", "answer", "ack"])
def test_every_kind_says_first_that_it_is_posted_and_nothing_more_is_to_be_done(drydock, capsys, operator, kind):
    re = [] if kind in ("order", "question") else ["--re", an_order(capsys, drydock, to="operator").id]
    _, out = qd(capsys, drydock, kind, "--to", "captain", *re, "the words")
    (path,) = drops(drydock, "operator")
    filed = post.Message.from_json(path.read_text())
    how = "files it" if kind == "ack" else "delivers it"
    assert out.startswith(f"Posted {filed.short} to captain. Nothing more to do: the ship {how}. Details: {filed.id} · ")
    assert "· filed by the service ·" in out


def test_a_repeated_report_still_in_the_drop_is_not_posted_again(drydock, capsys, operator):
    order = an_order(capsys, drydock)
    _, first = qd(capsys, drydock, "report", "--from", "worker-1", "--re", order.id, "done: all green")
    (path,) = drops(drydock, "worker-1")
    original = post.Message.from_json(path.read_text())
    for _ in range(3):
        _, again = qd(capsys, drydock, "report", "--from", "worker-1", "--re", order.short, "done: all green")
        assert again == f"Already posted as {original.short}; not posted again. Nothing more to do."
    assert drops(drydock, "worker-1") == [path] and first.startswith(f"Posted {original.short} ")
    reports = [m for m in stored(drydock) if m.kind == "report"]
    assert [m.id for m in reports] == [original.id]


@pytest.mark.parametrize("kind", ["report", "answer", "ack"])
def test_a_repeat_already_filed_into_the_store_is_not_posted_again(drydock, capsys, operator, kind):
    order = an_order(capsys, drydock)
    qd(capsys, drydock, kind, "--from", "worker-1", "--re", order.id, "the same words")
    (first,) = [m for m in stored(drydock) if m.kind == kind]
    assert drops(drydock, "worker-1") == []
    _, again = qd(capsys, drydock, kind, "--from", "worker-1", "--re", order.id, "the same words")
    assert again == f"Already posted as {first.short}; not posted again. Nothing more to do."
    assert drops(drydock, "worker-1") == [] and [m for m in stored(drydock) if m.kind == kind] == [first]


def test_an_ack_without_words_is_one_claim(drydock, capsys, operator):
    order = an_order(capsys, drydock)
    qd(capsys, drydock, "ack", "--from", "worker-1", "--re", order.id)
    _, again = qd(capsys, drydock, "ack", "--from", "worker-1", "--re", order.id)
    assert again.startswith("Already posted as ack-")
    assert len([m for m in stored(drydock) if m.kind == "ack"]) == 1


@pytest.mark.parametrize("kind", ["order", "question"])
def test_an_order_or_a_question_is_never_deduplicated(drydock, capsys, operator, kind):
    order = an_order(capsys, drydock, to="operator")
    for _ in range(2):
        _, out = qd(capsys, drydock, kind, "--to", "worker-1", "--re", order.id, "the same words")
        assert out.startswith("Posted ")
    assert len([m for m in stored(drydock) if m.kind == kind and m.body == "the same words"]) == 2


def test_another_body_another_re_or_another_sender_is_posted(drydock, capsys, operator):
    first, second = an_order(capsys, drydock), an_order(capsys, drydock, body="and this")
    qd(capsys, drydock, "report", "--from", "worker-1", "--re", first.id, "done")
    posts = [("worker-1", first.id, "done."),  # one byte more
             ("worker-1", first.id, "Done"),
             ("worker-1", second.id, "done"),  # another re
             ("captain", first.id, "done")]  # another seat
    for sender, re, body in posts:
        _, out = qd(capsys, drydock, "report", "--from", sender, "--re", re, "--body-file", str(_file(drydock, body)))
        assert out.startswith("Posted rep-"), (sender, re, body, out)
    assert len([m for m in stored(drydock) if m.kind == "report"]) == 5


def _file(drydock: Path, body: str) -> Path:
    path = drydock.parent / "body.txt"
    path.write_text(body)
    return path


def test_a_report_that_names_no_message_may_recur(drydock, capsys, operator):
    """A standing report with no re closes nothing; the same words tomorrow are tomorrow's report."""
    for _ in range(2):
        _, out = qd(capsys, drydock, "report", "--from", "worker-1", "--to", "captain", "nothing open")
        assert out.startswith("Posted rep-")
    assert len([m for m in stored(drydock) if m.kind == "report"]) == 2


def test_an_ack_moves_the_claim_back_and_only_the_latest_claim_is_a_repeat(drydock, capsys, operator):
    """The Ledger reads a seat's latest ack: ack A, ack B, ack A again claims A again and is posted."""
    first, second = an_order(capsys, drydock), an_order(capsys, drydock, body="and this")
    qd(capsys, drydock, "ack", "--from", "worker-1", "--re", first.id)
    _, out = qd(capsys, drydock, "ack", "--from", "worker-1", "--re", second.id)
    assert out.startswith("Posted ack-")
    _, back = qd(capsys, drydock, "ack", "--from", "worker-1", "--re", first.id)
    assert back.startswith("Posted ack-")
    _, again = qd(capsys, drydock, "ack", "--from", "worker-1", "--re", first.id)
    acks = [m for m in stored(drydock) if m.kind == "ack"]
    assert again == f"Already posted as {acks[-1].short}; not posted again. Nothing more to do."
    assert [m.re for m in acks] == [first.id, second.id, first.id]
    assert post.Store(drydock / "runtime").claimed("worker-1", post.now()) == first.id


def test_a_seat_posts_as_itself_and_the_guard_reads_its_own_drop(drydock, capsys, monkeypatch):
    monkeypatch.delenv("QD_SEAT", raising=False)
    order = an_order(capsys, drydock)
    monkeypatch.setenv("QD_SEAT", "worker-1")
    _, first = qd(capsys, drydock, "report", "--re", order.id, "filed")
    _, again = qd(capsys, drydock, "report", "--re", order.id, "filed")
    assert first.startswith("Posted rep-") and again.startswith("Already posted as rep-")
    assert len(drops(drydock, "worker-1")) == 1


def held(suffix: str) -> str:
    """A report's file name in a drop, joined so that no literal id stands in the repository (law: nothing-leaks)."""
    return "-".join(["rep", "20261114", "1600", "worker-1", suffix])


def test_what_is_no_message_in_the_drop_does_not_stop_a_post(drydock, capsys, operator):
    order = an_order(capsys, drydock)
    folder = drydock / "runtime" / "post" / "drop" / "worker-1"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{held('0000')}.json").write_text("not json")
    (folder / f"{held('0001')}.json").write_bytes(b"\xff\xfe")
    (folder / f"{held('0002')}.json").write_text("[]")
    (folder / "linked.json").symlink_to(drydock / "ship.toml")
    _, out = qd(capsys, drydock, "report", "--from", "worker-1", "--re", order.id, "done")
    assert out.startswith("Posted rep-")
    assert (folder / "linked.json").is_symlink()


def test_the_guard_reads_only_its_own_drop_and_the_store(tmp_path: Path):
    """Another seat's drop is not this seat's: an identical report there does not match."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    store = post.Store(runtime)
    order = store.new("order", "desk", "worker-1", "build")
    theirs = store.prepare("report", "worker-2", "desk", "done", re=order.id)
    post.write_drop(runtime, theirs)
    mine = store.prepare("report", "worker-1", "desk", "done", re=order.id)
    assert post.already_posted(store, mine) is None
    post.write_drop(runtime, mine)
    again = store.prepare("report", "worker-1", "desk", "done", re=order.id)
    assert post.already_posted(store, again) == mine
    assert post.already_posted(store, mine) is None, "a message is not its own repeat"


def test_the_help_says_the_words_and_both_edges_of_the_guard(capsys):
    with pytest.raises(SystemExit):
        cli.main(["post", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "Posted <id> to <seat>. Nothing more to do" in text
    assert "Already posted as <id>; not posted again" in text
    assert "A post with no re is never refused" in text
    assert "An ack is refused only while the identical ack is still this seat's latest" in text
    assert "Orders and questions are never refused" in text
