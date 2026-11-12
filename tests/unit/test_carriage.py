"""The bounded first message: byte bounds, the account, the file, and the refusals it may and may not make."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from quarterdeck import carriage, post, refusal
from quarterdeck.ship import ShipError


def message(store: post.Store, index: int, kind: str = "order", subject: str | None = None, body: str = "synthetic body"):
    return store.new(kind, "desk", "captain", body, subject=subject if subject is not None else f"Synthetic {kind} {index}")


def built(tmp_path: Path, roots, refused=frozenset(), others: int = 0, text: str = "synthetic carried text") -> carriage.Carriage:
    return carriage.Carriage(text, list(roots), post.briefed_header("captain", "captain", list(roots)),
                             tuple(roots), frozenset(refused), others)


@pytest.fixture
def store(tmp_path: Path) -> post.Store:
    return post.Store(tmp_path / "runtime")


# -- bytes, never characters -------------------------------------------------------------------


def test_the_bounds_are_the_kernels_with_wide_margins():
    assert carriage.ARGUMENT_LIMIT == 131_072
    assert carriage.INLINE_BYTES == 32_768 and carriage.WORD_BYTES == 65_536
    assert carriage.argument_limit() == 32 * os.sysconf("SC_PAGE_SIZE")


def test_sizes_count_utf8_bytes_with_the_nul_and_pointers():
    largest, index, total = carriage.sizes(["ab", "é" * 10], {"K": "v"})
    assert (largest, index) == (21, 1), "ten two-byte characters and the NUL"
    assert total == 3 + 21 + 4 + carriage.POINTER * (2 + 1 + 2)


@pytest.mark.parametrize("extra,expected", [(0, None), (1, "word 1")])
def test_a_soft_bound_is_crossed_at_one_byte_past_half_the_limit(extra, expected):
    word = "é" * ((carriage.WORD_BYTES - 1) // 2) + "x" * ((carriage.WORD_BYTES - 1) % 2 + extra)
    assert carriage.utf8(word) + 1 == carriage.WORD_BYTES + extra
    found = carriage.misfit(["program", word], {})
    assert found is None if expected is None else expected in found


@pytest.mark.parametrize("extra", [0, 1])
def test_the_hard_bound_is_the_kernels_own_and_names_only_position_and_sizes(extra):
    word = "secret-synthetic-text " * 10_000
    word = word[: carriage.argument_limit() - 1 + extra]
    reason = carriage.refused(["env", "PATH=/x", "program", word], {"TOKEN": "synthetic-value"})
    if extra == 0:
        assert reason is None, "exactly the kernel's limit, NUL included, is allowed"
    else:
        assert "word 3" in reason and str(carriage.argument_limit() + 1) in reason
        assert "secret" not in reason and "synthetic-value" not in reason and "TOKEN" not in reason


def test_a_brief_past_the_soft_bound_is_never_a_refusal():
    brief = "b" * (carriage.WORD_BYTES + 10)
    assert carriage.misfit(["program", brief], {}) is not None
    assert carriage.refused(["program", brief], {}) is None


# -- the account ------------------------------------------------------------------------------


def test_subjects_are_cut_on_whole_characters_with_controls_escaped_and_a_marker():
    text = "ünïcödé " * 100 + "\x1b[31m\n\t‮"
    cut = carriage.clean(text, carriage.SUBJECT_BYTES)
    assert carriage.utf8(cut) <= carriage.SUBJECT_BYTES and cut.endswith(carriage.CUT)
    cut.encode("utf-8")  # whole characters only
    shown = carriage.clean("a\x1b[31m\nb\t‮c", 200)
    assert shown == "a\\u001b[31m\\u000ab\\u0009\\u202ec"
    assert "\n" not in shown and "\x1b" not in shown and carriage.CUT not in shown


def test_one_line_per_open_order_and_question_with_its_full_id(store, tmp_path):
    order, question = message(store, 0), message(store, 1, "question")
    path = tmp_path / "carried.txt"
    text = carriage.account(built(tmp_path, [order, question], others=3), path, 24_576)
    lines = text.splitlines()
    assert lines[0] == "qd > briefed as captain, captain · 1 order open, 1 question open"
    assert str(path) in lines[1] and "read" in lines[2] and "whole" in lines[2] and "before you act" in lines[2]
    assert f"qd > open order from desk · {order.id} · Synthetic order 0" in lines
    assert f"qd > open question from desk · {question.id} · Synthetic question 1" in lines
    assert any("3 amendments" in line for line in lines) and lines[-1] == "qd > end of account"


def test_a_refused_root_is_named_without_its_subject_or_body(store, tmp_path):
    secret = message(store, 0, subject="SYNTHETIC REFUSED SUBJECT", body="SYNTHETIC REFUSED BODY")
    kept = built(tmp_path, [secret], refused={secret.id})
    for text in (carriage.account(kept, tmp_path / "f", 24_576), carriage.contents(kept, "captain", "stamp")):
        assert secret.id in text and refusal.WITHHELD in text
        assert "SYNTHETIC REFUSED SUBJECT" not in text and "SYNTHETIC REFUSED BODY" not in text


@pytest.mark.parametrize("budget", carriage.ACCOUNT_BUDGETS)
def test_an_account_past_its_budget_names_the_rest_never_dropping_one_silently(store, tmp_path, budget):
    roots = [message(store, i, subject="S" * 400) for i in range(300)]
    text = carriage.account(built(tmp_path, roots), tmp_path / "f", budget)
    shown = [line for line in text.splitlines() if line.startswith("qd > open order")]
    rest = [line for line in text.splitlines() if line.startswith("qd > and ")]
    named = int(rest[0].split()[3]) if rest else 0
    assert len(shown) + named == 300, "every open order is listed or counted"
    assert sum(carriage.utf8(line) + 1 for line in shown) <= budget
    full = carriage.contents(built(tmp_path, roots), "captain", "stamp")
    assert all(root.id in full for root in roots), "the file begins with the complete list"


def test_the_file_holds_the_complete_list_then_exactly_the_text_then_an_end_line(store, tmp_path):
    roots = [message(store, 0), message(store, 1, "question")]
    text = "qd > briefed as captain\nexact\ncarried text, ünïcödé"
    found = carriage.contents(built(tmp_path, roots, text=text), "captain", "20400101T000000Z")
    head, rest = found.split("qd > the carried messages follow", 1)
    assert all(root.id in head for root in roots)
    assert f": {len(text.encode())} bytes\n{text}\nqd > end of the carried messages for captain at launch 20400101T000000Z\n" in rest


# -- the file -----------------------------------------------------------------------------------


def test_the_file_is_written_durably_and_privately_under_a_name_of_its_own(store, tmp_path):
    seat = tmp_path / "seat"
    seat.mkdir()
    kept = built(tmp_path, [message(store, 0)])
    first, second = carriage.write(seat, "captain", kept), carriage.write(seat, "captain", kept)
    assert first != second and first.parent == second.parent == seat / carriage.FOLDER, "no launch rewrites another's"
    assert stat.S_IMODE(first.stat().st_mode) == 0o600 and stat.S_IMODE(first.parent.stat().st_mode) == 0o700
    assert first.read_text(encoding="utf-8") == carriage.contents(kept, "captain", first.name.split("-")[0])
    assert not [p for p in first.parent.iterdir() if p.name.startswith(".")], "no temporary left beside it"


@pytest.mark.parametrize("damage", ["link", "file"])
def test_a_carried_folder_that_is_not_a_plain_folder_refuses_the_launch(store, tmp_path, damage):
    seat, elsewhere = tmp_path / "seat", tmp_path / "elsewhere"
    seat.mkdir()
    elsewhere.mkdir()
    if damage == "link":
        (seat / carriage.FOLDER).symlink_to(elsewhere)
    else:
        (seat / carriage.FOLDER).write_text("synthetic")
    kept = built(tmp_path, [message(store, 0, body="SYNTHETIC BODY")], text="SYNTHETIC CARRIED TEXT")
    with pytest.raises(ShipError, match="could not be written") as raised:
        carriage.write(seat, "captain", kept)
    assert "SYNTHETIC" not in str(raised.value) and "never put whole into one argument" in str(raised.value)
    assert list(elsewhere.iterdir()) == [], "nothing is written through a link"
