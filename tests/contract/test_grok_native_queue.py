"""Synthetic native queue screens; ordinary decoder and handoff contracts only."""

import json
from pathlib import Path

import pytest

from quarterdeck.harness.grok import GrokHarness

FIXTURES = Path(__file__).parents[1] / "fixtures/grok/1.0.77"


def sample(width):
    return json.loads((FIXTURES / f"queue-native-{width}.json").read_text())


def frame(lines):
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize("width", [112, 256])
@pytest.mark.parametrize("count", [1, 2])
def test_measured_queue_counts_and_numbered_previews(width, count):
    data = sample(width)
    harness = GrokHarness()
    screen = harness.screen(frame(data["screens"][str(count)]))
    assert screen.working is True and screen.unknown is None
    assert screen.box is not None and not screen.box.strip()
    assert screen.queued == count
    for payload in data["payloads"][:count]:
        assert harness.queued_waiting(payload, screen)
    assert harness.keys.send_now is None


@pytest.mark.parametrize("width", [112, 256])
def test_new_native_queue_after_the_matched_draft_is_pending_not_delivered(width):
    data = sample(width)
    harness = GrokHarness()
    before = harness.screen(frame(data["draft"]))
    after = harness.screen(frame(data["screens"]["1"]))
    assert before.working is True and before.unknown is None
    assert before.queued == 0
    assert harness.queued_input(data["payloads"][0], before, after)
    assert after.queued == 1
