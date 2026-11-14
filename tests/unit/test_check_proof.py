"""The check proof is a regular file in that seat's folder, and its bytes never enter the row."""

from __future__ import annotations

import os
from pathlib import Path

from quarterdeck import check


def test_a_link_at_the_proof_name_is_not_a_regular_file_and_its_target_stays_out_of_the_output(tmp_path: Path):
    secret = "operator-file-contents-must-stay-unprinted"
    target = tmp_path / "operator-file"
    target.write_text(secret + "\n")
    proof = tmp_path / "check" / "seat" / "seat.txt"
    proof.parent.mkdir(parents=True)
    proof.symlink_to(target)
    line = "qd-check-0123abcd"
    row = check.proof_check(proof, line)
    rendered = str(row)
    assert row.passed is False and row.detail == "not a regular file"
    assert secret not in rendered and line not in rendered and secret not in row.detail
    held = proof.parent / "as-directory"
    held.mkdir()
    directory = check.proof_check(held, line)
    assert directory.passed is False and directory.detail == "not a regular file"
    assert secret not in str(directory)


def test_a_regular_proof_reports_only_whether_it_matched(tmp_path: Path):
    proof = tmp_path / "seat.txt"
    line = "qd-check-0123abcd"
    proof.write_text(line + "\n")
    matched = check.proof_check(proof, line)
    assert matched.passed is True and matched.detail == "matched" and line not in str(matched)
    planted = "planted-other-line"
    proof.write_text(planted + "\n")
    missed = check.proof_check(proof, line)
    assert missed.passed is False and missed.detail == "did not match"
    assert planted not in str(missed) and line not in str(missed)
    absent = check.proof_check(tmp_path / "absent.txt", line)
    assert absent.passed is False and absent.detail == "did not match"
    proof.write_text(line + "\n" + planted)
    overflow = check.proof_check(proof, line)
    assert overflow.passed is False and overflow.detail == "did not match" and planted not in str(overflow)


def test_a_fifo_at_the_proof_name_is_not_read(tmp_path: Path):
    proof = tmp_path / "seat.txt"
    os.mkfifo(proof)
    line = "qd-check-0123abcd"
    row = check.proof_check(proof, line)
    assert row.passed is False and row.detail == "not a regular file" and line not in str(row)
