"""The bounded first message: byte bounds, the account, the file, and the refusals it may and may not make."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from quarterdeck import carriage, carriage_plan, post, post_threads, refusal
from .test_carriage_capacity import index_rows


def message(store: post.Store, index: int, kind: str = "order", subject: str | None = None, body: str = "synthetic body"):
    return store.new(kind, "desk", "captain", body, subject=subject if subject is not None else f"Synthetic {kind} {index}")


def built(tmp_path, roots, refused=frozenset(), text='', limit=24576, generation='generation', force_files=False):
    store = post.Store(tmp_path / 'runtime')
    rows = post_threads.index_rows(store, tuple(roots), tuple(store.for_seat('captain')), frozenset(refused), seat='captain')
    return carriage_plan.build(rows, handoff=text, header=post.briefed_header('captain', 'captain', list(roots)),
                               directory=tmp_path / 'seat' / 'carried' / generation,
                               limit=limit, force_files=force_files)


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
    order, question = message(store, 0), message(store, 1, 'question')
    amendments = [store.new('answer', 'desk', 'captain', f'Invented amendment {n}.', re=order.id) for n in range(3)]
    result = built(tmp_path, [order, question])
    rows = {row['id']: row for row in index_rows(result)}
    assert result.opening.splitlines()[0] == 'qd > briefed as captain, captain · 1 order open, 1 question open'
    assert rows[order.id]['sender'] == rows[question.id]['sender'] == 'desk'
    assert rows[order.id]['subject'] == 'Synthetic order 0' and rows[question.id]['subject'] == 'Synthetic question 1'
    assert rows[order.id]['amendments'] == [amendment.id for amendment in amendments]
    assert rows[order.id]['thread'] == f'qd thread {order.id}'
    assert 'read that thread whole' in result.opening


def test_a_refused_root_is_named_without_its_subject_or_body(store, tmp_path, monkeypatch):
    secret = message(store, 0, subject='SYNTHETIC REFUSED SUBJECT', body='SYNTHETIC REFUSED BODY')
    monkeypatch.setattr(refusal, 'excluded', lambda runtime, seat: frozenset({secret.id}))
    result = built(tmp_path, [secret], refused={secret.id})
    printed = post_threads.render(post_threads.whole(store, secret.id, 'captain'))
    for text in (result.opening, printed, *result.files.values()):
        assert secret.id in text and refusal.WITHHELD in text
        assert 'SYNTHETIC REFUSED SUBJECT' not in text and 'SYNTHETIC REFUSED BODY' not in text


@pytest.mark.parametrize("budget", [24576, 8192, 2048, 0])
def test_an_account_past_its_budget_names_the_rest_never_dropping_one_silently(store, tmp_path, budget):
    roots = [message(store, n, subject='S' * 400) for n in range(300)]
    if budget == 0:
        with pytest.raises(ValueError, match='positive integer'):
            built(tmp_path, roots, limit=budget)
        return
    result = built(tmp_path, roots, limit=budget)
    rows = index_rows(result)
    assert len(rows) == len({row['id'] for row in rows}) == 300, 'every open thread is indexed once'
    assert {row['id'] for row in rows} == {root.id for root in roots}
    assert carriage.utf8(result.opening) <= budget
    assert all(carriage.utf8(text) <= budget for text in result.files.values())


def test_the_file_holds_exactly_the_complete_self_handoff_and_index_is_separate(store, tmp_path):
    roots = [message(store, 0), message(store, 1, 'question')]
    text = 'qd > synthetic complete self handoff\nexact\ncarried text, ünïcödé'
    result = built(tmp_path, roots, text=text, force_files=True)
    assert result.files['handoff.txt'] == text
    assert 'whole first, before acting; it is your handoff' in result.opening
    assert {row['id'] for row in index_rows(result)} == {root.id for root in roots}
    assert all(root.body not in payload for root in roots for payload in result.files.values())


# -- the file -----------------------------------------------------------------------------------


def test_the_file_is_written_durably_and_privately_under_a_name_of_its_own(store, tmp_path):
    seat = tmp_path / 'seat'
    seat.mkdir()
    carriage.folder_of(seat)
    roots = [message(store, 0)]
    first = built(tmp_path, roots, text='Complete invented self handoff.', generation='first', force_files=True)
    second = built(tmp_path, roots, text='Complete invented self handoff.', generation='second', force_files=True)
    carriage_plan.publish(first)
    carriage_plan.publish(second)
    assert first.directory != second.directory and first.directory.parent == second.directory.parent == seat / carriage.FOLDER
    assert stat.S_IMODE(first.directory.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in first.directory.iterdir())
    assert {path.name: path.read_text() for path in first.directory.iterdir()} == first.files
    assert not [path for path in first.directory.parent.iterdir() if path.name.startswith('.')], 'no unpublished temporary remains'


@pytest.mark.parametrize("damage", ["link", "file"])
def test_a_carried_folder_that_is_not_a_plain_folder_refuses_the_launch(store, tmp_path, damage):
    seat, elsewhere = tmp_path / 'seat', tmp_path / 'elsewhere'
    seat.mkdir()
    elsewhere.mkdir()
    if damage == 'link':
        (seat / carriage.FOLDER).symlink_to(elsewhere)
    else:
        (seat / carriage.FOLDER).write_text('synthetic')
    with pytest.raises(OSError) as raised:
        carriage.folder_of(seat)
    assert 'SYNTHETIC BODY' not in str(raised.value)
    assert list(elsewhere.iterdir()) == [], 'nothing is written through a link'
