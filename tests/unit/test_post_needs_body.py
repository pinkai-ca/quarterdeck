"""A CLI message needs words before anything is filed in the drop or store."""

from __future__ import annotations

from io import StringIO

import pytest

from quarterdeck import cli, post, ship as shipmod


@pytest.mark.parametrize("kind", ["order", "question", "report", "answer", "ack"])
@pytest.mark.parametrize("source", ["argument", "file", "stdin"])
@pytest.mark.parametrize("body", ["", " \t\r\n ", "\u2003\u00a0"],
                         ids=["empty", "whitespace", "unicode-whitespace"])
def test_blank_body_files_nothing(drydock, tmp_path, capsys, monkeypatch, kind, source, body):
    monkeypatch.delenv("QD_SEAT", raising=False)
    runtime = shipmod.load(drydock).runtime
    if source == "file":
        path = tmp_path / "body.txt"
        path.write_text(body, encoding="utf-8")
        words = ["--body-file", str(path)]
    elif source == "stdin":
        monkeypatch.setattr("sys.stdin", StringIO(body))
        words = ["-"]
    else:
        words = [body]

    code = cli.main(["--ship", str(drydock), "post", kind, "--to", "captain", *words])

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert captured.err.strip() == "qd: a message needs a body"
    assert not list((runtime / "post" / "drop").rglob("*.json"))
    assert not list((runtime / "post" / "store").glob("*.json"))
    assert post.Store(runtime).all() == []


@pytest.mark.parametrize("kind", ["order", "question", "report", "answer"])
def test_missing_body_has_the_same_reason(drydock, capsys, monkeypatch, kind):
    monkeypatch.delenv("QD_SEAT", raising=False)
    code = cli.main(["--ship", str(drydock), "post", kind, "--to", "captain"])
    captured = capsys.readouterr()
    assert code == 2 and captured.out == ""
    assert captured.err.strip() == "qd: a message needs a body"
    assert not list((drydock / "runtime" / "post").rglob("*.json"))


@pytest.mark.parametrize("source", ["argument", "file", "stdin"])
def test_nonempty_text_is_still_posted(drydock, tmp_path, capsys, monkeypatch, source):
    monkeypatch.delenv("QD_SEAT", raising=False)
    body = " \tThe measured result.\n "
    if source == "file":
        path = tmp_path / "body.txt"
        path.write_text(body)
        words = ["--body-file", str(path)]
    elif source == "stdin":
        monkeypatch.setattr("sys.stdin", StringIO(body))
        words = ["-"]
    else:
        words = [body]
    code = cli.main(["--ship", str(drydock), "post", "report", "--to", "captain", *words])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    store = post.Store(drydock / "runtime")
    post.file_drops(store)
    (message,) = store.all()
    assert message.body == "The measured result."
