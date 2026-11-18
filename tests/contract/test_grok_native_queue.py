"""Synthetic native queue screens; ordinary decoder and handoff contracts only."""

import json
from pathlib import Path

import pytest

from quarterdeck.harness.grok import GrokHarness

FIXTURES = Path(__file__).parents[1] / "fixtures/grok/1.0.77"


def sample(width):
    text = (FIXTURES / f"queue-native-{width}.json").read_text()
    if width == "response-256":
        text = text.replace("<answer-id>", "-".join(("ans", "a1b2c3")))
        text = text.replace("<question-id>", "-".join(("que", "d4e5f6")))
    return json.loads(text)


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


@pytest.mark.parametrize("styled", [False, True])
def test_response_queue_without_background_shortcut(styled):
    data = sample("response-256")
    harness = GrokHarness()
    rows = data["screens"]["1"]
    if styled:
        rows = [row.replace("▾ Queued 1", "\x1b[38;2;108;108;108m▾ Queued 1\x1b[39m")
                .replace("#1 ", "\x1b[38;2;108;108;108m#1\x1b[39m ")
                .replace("Ctrl+x:shortcuts", "\x1b[1mCtrl+x\x1b[0m:shortcuts") for row in rows]
    screen = harness.screen(frame(rows))
    assert screen.working is True and screen.unknown is None
    assert screen.box is not None and not screen.box.strip(" ")
    assert screen.queued == 1
    payload = data["payloads"][0]
    assert harness.queued_waiting(payload, screen)
    assert harness.queued_input(payload, harness.screen(frame(data["draft"])), screen)
    assert not harness.queued_waiting(payload.replace("a1b2c3", "f0e1d2", 1), screen)
    assert not harness.queued_waiting(payload + "One more line.\n", screen)
    assert harness.screen(frame(data["screens"]["0"])).working is True
    assert harness.screen(frame(data["screens"]["idle"])).working is False


@pytest.mark.parametrize("change", ["other-hint", "extra-row", "no-list", "missing-entry", "wrong-number",
                                   "missing-separator", "draft", "tall-box", "clipped-box"])
def test_response_queue_hint_requires_the_empty_box_and_complete_list(change):
    data = sample("response-256")
    rows = data["screens"]["1"]
    title = next(i for i, row in enumerate(rows) if "▾ Queued" in row)
    prompt = next(i for i, row in enumerate(rows) if row.startswith("  │ ❯ "))
    if change == "other-hint":
        rows[-1] = rows[-1].replace("Ctrl+x:shortcuts", "Ctrl+x:help")
    elif change == "extra-row":
        rows.append("  Another status row")
    elif change == "no-list":
        del rows[title:prompt - 1]
    elif change == "missing-entry":
        rows[title] = "   ▾ Queued 2"
    elif change == "wrong-number":
        rows[title + 1] = rows[title + 1].replace("#1 ", "#2 ")
    elif change == "missing-separator":
        del rows[prompt - 2]
    elif change == "draft":
        rows[prompt] = rows[prompt][:6] + "draft" + rows[prompt][11:]
    elif change == "tall-box":
        rows.insert(prompt + 1, rows[prompt].replace("❯", " "))
    elif change == "clipped-box":
        rows[prompt] = rows[prompt][:-1]
    harness = GrokHarness()
    screen = harness.screen(frame(rows))
    assert screen.unknown and screen.working is None and screen.box is None and screen.queued == 0
    assert not harness.queued_waiting(data["payloads"][0], screen)
