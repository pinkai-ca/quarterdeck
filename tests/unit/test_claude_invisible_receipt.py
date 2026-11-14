"""Claude Code 2.1.317 drops invisible format characters from a paste; its receipt removes them from both sides.

Measured on 2.1.317 (13 November): a post holding two zero-width spaces (U+200B) was pasted into a running turn,
queued, removed as absorbed mid-turn and attached as a queued command, each row carrying the text without them.
The receipt compared the payload as typed, never matched, and the door shut on a delivery that was taken in.
The program's own sanitiser names the rest: the format characters (category Cf) among its hidden characters,
each removed unless its context keeps it (a joiner inside an emoji, say). The receipt removes exactly those
from the recorded text and from each form alike, and nothing else. Shapes follow that record; all content,
identities and times here are synthetic. The invisible characters are written as escapes, never literally.
"""

import unicodedata

import pytest

from quarterdeck import harness, input_receipt
from quarterdeck.harness import closed_record
from quarterdeck.harness.claude import PASTE_HIDDEN_FORMAT
from .test_claude_draft_return import finish, record, user, working
from .test_claude_tab_receipt import MEASURED, SINCE, absorbed, pasted, request, versioned_input
from .test_closed_record import append, native_input, stamp, world  # noqa: F401 -- the shared closed-launch fixture

ZWSP = "​"
PAYLOAD = (f"qd > synthetic report\nit names its code points ('{ZWSP}/config', a word joiner)\n"
           f"and '{ZWSP}/mcp' is refused for its hidden character\n")
RECORDED = PAYLOAD.replace(ZWSP, "")


def test_the_measured_set_is_format_characters_alone_and_holds_the_zero_width_space():
    assert ZWSP in PASTE_HIDDEN_FORMAT
    assert {unicodedata.category(char) for char in PASTE_HIDDEN_FORMAT} == {"Cf"}
    for kept in ("؀", "۝", "\U000110bd"):  # format characters the sanitiser does not hide
        assert kept not in PASTE_HIDDEN_FORMAT


@pytest.mark.parametrize("chain", ["absorbed", "prompt"])
def test_a_post_with_zero_width_spaces_is_delivered(tmp_path, chain):
    rows = ([*working(), *absorbed(RECORDED), *finish()] if chain == "absorbed"
            else [dict(user(pasted(RECORDED), 2), version=MEASURED)])
    path = record(tmp_path, *rows)
    assert input_receipt.confirmed("claude", path, PAYLOAD, SINCE) == "delivered"


def test_only_queued_it_is_queued(tmp_path):
    path = record(tmp_path, *working(), *absorbed(RECORDED)[:1], request(3))
    assert input_receipt.confirmed("claude", path, PAYLOAD, SINCE) == "queued"


@pytest.mark.parametrize("form", [
    RECORDED.replace("/config", "/confog"),  # one visible character differs
    RECORDED.replace("/mcp", "/mcp."),
    RECORDED.replace("'/config'", "' /config'"),  # a space where the invisible character stood
    RECORDED + "x",
])
def test_a_post_differing_in_a_visible_character_stays_unconfirmed(tmp_path, form):
    path = record(tmp_path, *working(), *absorbed(form), *finish())
    assert input_receipt.confirmed("claude", path, PAYLOAD, SINCE) is None


@pytest.mark.parametrize("kept", [True, False])
def test_a_joiner_the_program_keeps_or_drops_confirms_either_way(tmp_path, kept):
    payload = "a family \U0001F468‍\U0001F469 and a break​here\n"
    recorded = payload.replace("​", "") if kept else payload.replace("​", "").replace("‍", "")
    path = record(tmp_path, *working(), *absorbed(recorded), *finish())
    assert input_receipt.confirmed("claude", path, payload, SINCE) == "delivered"


@pytest.mark.parametrize("char", [
    "؀",  # a format character the sanitiser keeps
    "️",  # a variation selector it hides, but a mark, not a format character
    "͏",  # the combining grapheme joiner: hidden, a mark
    "ㅤ",  # a Hangul filler: hidden, a letter
])
def test_nothing_wider_than_the_measured_format_characters_is_ignored(tmp_path, char):
    payload = f"{ZWSP}synthetic {char}text\n"
    path = record(tmp_path, *working(), *absorbed(payload.replace(ZWSP, "").replace(char, "")), *finish())
    assert input_receipt.confirmed("claude", path, payload, SINCE) is None


def test_a_payload_without_one_is_compared_exactly(tmp_path):
    payload = "synthetic plain text\n"
    path = record(tmp_path, *working(), *absorbed(payload.replace("plain", f"pl{ZWSP}ain")), *finish())
    assert input_receipt.confirmed("claude", path, payload, SINCE) is None


def test_with_a_tab_form_too_it_is_delivered(tmp_path):
    payload = PAYLOAD + "a\tb\n"
    path = record(tmp_path, *working(), *absorbed(payload.replace(ZWSP, "").replace("\t", "    ")), *finish())
    assert input_receipt.confirmed("claude", path, payload, SINCE) == "delivered"


@pytest.mark.parametrize("version", ["2.1.318", None])
def test_an_unmeasured_or_unknown_version_ignores_nothing(tmp_path, version):
    path = record(tmp_path, *working(), *absorbed(RECORDED, version=version), *finish())
    assert input_receipt.confirmed("claude", path, PAYLOAD, SINCE) is None


def test_the_claude_adapter_declares_the_set_only_for_a_payload_holding_one(tmp_path):
    claude = harness.get("claude")
    path = record(tmp_path, request(2))
    assert claude.paste_ignored(PAYLOAD, path, SINCE) == PASTE_HIDDEN_FORMAT
    assert claude.paste_ignored(RECORDED, path, SINCE) == frozenset()


@pytest.mark.parametrize("name", [name for name in harness.NAMES if name != "claude"])
def test_other_adapters_ignore_nothing(tmp_path, name):
    path = record(tmp_path, request(2))
    assert harness.get(name).paste_ignored(PAYLOAD, path, SINCE) == frozenset()


def test_closed_record_counts_the_stripped_form_as_present_for_claude_alone(world):  # noqa: F811
    w = world
    w.live["pane"] = None
    w.proof.data["payload"] = PAYLOAD
    append(w.record, versioned_input(w.name, RECORDED, stamp(11)))
    assert closed_record.not_in_closed_record(w.seat, w.proof) is (w.name != "claude")


def test_closed_record_still_finds_absence(world):  # noqa: F811
    w = world
    w.live["pane"] = None
    w.proof.data["payload"] = PAYLOAD
    append(w.record, native_input(w.name, "Synthetic other input", stamp(11)))
    assert closed_record.not_in_closed_record(w.seat, w.proof)
