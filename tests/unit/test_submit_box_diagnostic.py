"""Submit diagnostics distinguish qd excerpts from the program's own text."""

from types import SimpleNamespace

import pytest

from quarterdeck.harness.types import Screen
from quarterdeck.pending import _submit_candidate


@pytest.mark.parametrize("text", ["native...", "A" * 79, "A" * 80, "A" * 81, "é" * 120])
def test_submit_refusal_identifies_only_its_own_abbreviation(text):
    seat = SimpleNamespace(name="example", session=SimpleNamespace(human=False))
    proof, reason = _submit_candidate(
        SimpleNamespace(seat=seat), Screen("", box=text), object(), proofs=[])
    assert proof is None
    expected = (repr(text) if len(text) <= 80 else
                f"{text[:77]!r}… (shortened by qd, {len(text)} chars)")
    assert reason == (f"refused: example's box shows {expected}, "
                      "not exactly a pending message of example")
    assert ("shortened by qd" in reason) == (len(text) > 80)
