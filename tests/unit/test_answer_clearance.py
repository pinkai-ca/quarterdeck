"""An adapter's clearance bound and the generic default, with a synthetic clock."""

from types import SimpleNamespace

import pytest

from quarterdeck import door
from quarterdeck.harness.fake import FakeHarness


@pytest.mark.parametrize("bound, elapsed, outcome", [
    (None, 5.0, "did not clear"),
    (8.0, 7.0, "cleared"),
])
def test_the_door_observes_the_adapter_clearance_bound(monkeypatch, bound, elapsed, outcome):
    clock, answers = [0.0], []

    def sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr(door, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    monkeypatch.setattr(door, "SETTLE_SECONDS", 1.0)
    monkeypatch.setattr(door, "ANSWER_SECONDS", 5.0)
    question = SimpleNamespace(name="example", click="[0] Continue", label_key="0", by_label=True, recurs=False)
    screen = SimpleNamespace(text="[0] Continue", box=None, interruption=question, working=False)
    ready = SimpleNamespace(text="ready", box="", interruption=None, working=False)
    harness = FakeHarness()
    if bound is not None:
        harness.answer_seconds = bound
    harness.screen = lambda raw: screen if raw == "question" else ready
    harness.question_cleared = lambda raw, question: raw == "ready"
    launch = SimpleNamespace(answered=None)

    def record_answer(launched, entry):
        answers.append(entry)
        return launch

    seat = SimpleNamespace(
        harness=harness, tmux=SimpleNamespace(keys=lambda *args: None),
        launched=lambda: launch, record_answer=record_answer,
        read_pane=lambda: (SimpleNamespace(id="%1"), "ready" if clock[0] >= 7.0 else "question"),
    )
    observed = door.Door(seat).answer(screen)
    assert clock[0] == elapsed
    assert answers[-1]["outcome"] == outcome
    assert observed is (ready if outcome == "cleared" else screen)
