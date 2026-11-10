"""Codex textwrap boundaries preserve exact text and the observed padded geometry.

Source: openai/codex rust-v0.166.0, codex-rs/tui/src/bottom_pane/textarea/wrapping.rs.
It selects textwrap 0.16.2 FirstFit with unicode-linebreak 0.1.5 (Unicode 15).
The separator suppresses HY breaks; HyphenSplitter restores only those between
alphanumeric characters. Cases below cover the printable-ASCII projection,
including bracket/quote/number context and established Latin letters. Unknown
classes constrain their own context rather than invalidating the whole line.
The saved-shape fixture substitutes every private letter/digit and its headers.
"""

import json
from pathlib import Path

import pytest

from quarterdeck.harness.codex import CodexHarness
from .test_padded_box import capture, door_for

FIXTURE = Path(__file__).parents[1] / "fixtures" / "codex" / "slash-wrap.json"


def test_saved_shape_slash_break_verifies_without_opening_a_foreign_box(monkeypatch):
    example = json.loads(FIXTURE.read_text())
    door = door_for("codex", example["capture"], monkeypatch, example["pane_columns"])
    screen = door.screen()
    assert door.harness.box_columns(example["pane_columns"]) == example["box_columns"] == 475
    rows = screen.box.splitlines()
    assert [len(row) for row in rows] == [476, 476, 472, 30]
    assert [len(row.rstrip(" ")) for row in rows] == [47, 471, 324, 30]
    assert rows[1].rstrip(" ").endswith("/")
    assert door.matches(door.harness.box(example["payload"]), screen)
    assert not door.reading().open


@pytest.mark.parametrize("left,right", [("folder/", "branch"), ("answer!", "follow"),
    ("query?", "reply"), ("option|", "other"), ("value}", "next"), ("value)", "(next)"),
    ("part-", "continued"), ("choice%", "+more"), ("label:", "(group)")])
def test_source_supported_breaks_need_overflow_and_keep_all_characters(monkeypatch, left, right):
    first = "a" * (37-len(left)) + left
    payload = first + right
    door = door_for("codex", capture("codex", first+"\n"+right, "full", 43), monkeypatch, 43)
    assert door.matches(door.harness.box(payload), door.screen())
    assert not door.matches(door.harness.box(payload.replace(left, left[:-1])), door.screen())
    too_wide = door_for("codex", capture("codex", first+"\n"+right, "full", 83), monkeypatch, 83)
    assert not too_wide.matches(too_wide.harness.box(payload), too_wide.screen())


@pytest.mark.parametrize("left,right", [("folder,", "branch"), ("label:", "value"),
    ("value.", "next"), ("value;", "next"), ("option_", "next"), ("option=", "next"),
    ("path\\", "next"), ("amount/", "12345"), ("value)", "next"), ("value\"", "next"),
    ("flag--", "next"), ("open(", "next"), ("money$", "12345")])
def test_plausible_punctuation_is_not_an_arbitrary_break(monkeypatch, left, right):
    first = "a" * (37-len(left)) + left
    door = door_for("codex", capture("codex", first+"\n"+right, "full", 43), monkeypatch, 43)
    assert not door.matches(door.harness.box(first+right), door.screen())
    assert door.matches(door.harness.box(first+"\n"+right), door.screen())


@pytest.mark.parametrize("name", ["pi", "claude", "antigravity", "fake", "opencode"])
def test_other_adapters_do_not_acquire_codex_break_rules(name, monkeypatch):
    from quarterdeck.harness import get
    width = 48
    columns = get(name).box_columns(width)
    first = "x" * (columns - 5) + "/"
    door = door_for(name, capture(name, first+"\nbranch", "full", width), monkeypatch, width)
    assert not door.matches(door.harness.box(first+"branch"), door.screen())


@pytest.mark.parametrize("left,right", [("open( ", "more"), ("quote\"  ", "(more)"),
                                      ("letter\u200d/", "branch"), ("text\u00a0", "next")])
def test_unestablished_or_nonbreaking_context_is_not_a_space_wrap(monkeypatch, left, right):
    first = "x" * (37-len(left)) + left
    door = door_for("codex", capture("codex", first.rstrip()+"\n"+right, "full", 43), monkeypatch, 43)
    assert not door.matches(door.harness.box(first+right), door.screen())


@pytest.mark.parametrize("payload,rows", [
    ("café nextword", "café\nnextword"),
    ("a café-barbaz", "a café-\nbarbaz"),
    ("a café/barbaz", "a café/\nbarbaz"),
    ("tag caféword", "tag\ncaféword"),
    ("a foo-barbaz", "a foo-\nbarbaz"),
    ("a foo/barbaz", "a foo/\nbarbaz"),
    ("Ω note nextword", "Ω note\nnextword"),
    ("tag nextword Ω", "tag\nnextword Ω"),
    ("☃ café/barbaz", "☃ café/\nbarbaz"),
    ("go résumé-tail", "go résumé-\ntail"),
    ("a אב-nextword", "a אב-\nnextword"),
])
def test_established_word_breaks_do_not_depend_on_unrelated_unicode(monkeypatch, payload, rows):
    door = door_for("codex", capture("codex", rows, "full", 13), monkeypatch, 13)
    assert door.harness.box_columns(13) == 10
    assert door.matches(door.harness.box(payload), door.screen())
    assert not door.reading().open, "a match alone cannot claim a foreign draft"
    assert not door.matches(door.harness.box(payload + "!"), door.screen())
    wider = door_for("codex", capture("codex", rows, "full", 43), monkeypatch, 43)
    assert not wider.matches(wider.harness.box(payload), wider.screen()), "a fitting word cannot justify a break"


@pytest.mark.parametrize("payload,rows", [
    ("café nextword", "caf é\nnextword"),
    ("café nextword", "cafe\nnextword"),
    ("café nextword", "café\nnextwor"),
    ("cafe\u0301 nextword", "cafe\u0301\nnextword"),
    ("a 👩\u200d💻nextword", "a 👩\u200d💻\nnextword"),
    ("1234567 café\u0301tail", "1234567\ncafé\u0301tail"),
    ("x\u00a0nextword", "x\nnextword"),
    ("open( nextword é", "open(\nnextword é"),
    ('quote" (more) é', 'quote"\n(more) é'),
    ("name:nextword é", "name:\nnextword é"),
    ("a 「 nextword", "a 「\nnextword"),
    ("a אב|+nextword", "a אב|\n+nextword"),
])
def test_unicode_does_not_authorize_changed_text_or_unknown_context(monkeypatch, payload, rows):
    door = door_for("codex", capture("codex", rows, "full", 13), monkeypatch, 13)
    assert not door.matches(door.harness.box(payload), door.screen())


@pytest.mark.parametrize("payload,shown", [
    ("a b-1 2 3 4", "a b-\n1 2 3 4"),  # ASCII control: refused before and after 9e8e0e4
    ("a Ω-1 2 3 4", "a Ω-\n1 2 3 4"),
    ("a ש-1 2 3 4", "a ש-\n1 2 3 4"),
])
def test_a_number_after_unknown_context_keeps_its_own_breaks(payload, shown):
    """The hyphen split before "1" is real, but "1" alone fits the row, so the
    shown break is the payload with a newline after the hyphen, not this one."""
    from quarterdeck.harness.types import _box_matches
    assert not _box_matches(payload, shown, 10, textwrap_ascii=True)
    assert _box_matches(payload.replace("-", "-\n", 1), shown, 10, textwrap_ascii=True)


def test_a_number_after_unknown_context_allows_the_true_first_fit_display():
    from quarterdeck.harness.types import _box_matches
    payload = "a Ω-1 2 3 4"
    assert _box_matches(payload, "a Ω-1 2 3\n4", 10, textwrap_ascii=True)
    assert not _box_matches(payload, "a Ω-1 2 3\n4", 11, textwrap_ascii=True)
