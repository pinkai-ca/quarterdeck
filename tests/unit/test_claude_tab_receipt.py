"""Claude Code 2.1.317 records a paste in one changed form; its receipt reads that one form.

Two changes are measured. A TAB is recorded as four spaces, and a literal opening wrapper tag in the text,
``<pasted_content``, is escaped as ``<\\pasted_content`` inside the program's own wrapper.

Measured on 2.1.317: a payload pasted into a running turn was queued, removed as absorbed mid-turn and
attached as a queued command, each carrying the text with every TAB as four spaces, wherever the TAB
stood. The receipt compared the payload as typed, never matched, and the delivery stayed unconfirmed.
Queue rows carry no version; the program's own ``version`` field on the record's other rows names it.
Shapes follow that record; all content, identities and times here are synthetic.
"""

from datetime import timedelta

import pytest

from quarterdeck import harness, input_receipt
from quarterdeck.harness import closed_record
from .test_claude_draft_return import AT, finish, queue, record, row, user, working
from .test_closed_record import append, native_input, stamp, world  # noqa: F401 -- the shared closed-launch fixture

MEASURED = "2.1.317"
PAYLOAD = "qd > synthetic answer\n--- a/synthetic-name.py\tfirst stamp\n+++ b/name.py\tsecond stamp\nend of answer\n"
RECORDED = PAYLOAD.replace("\t", "    ")
SINCE = AT + timedelta(seconds=1)


def pasted(text: str) -> str:
    return f'<pasted_content id="synthetic">\n{text.rstrip(chr(10))}\n</pasted_content id="synthetic">'


def request(second: float, version: str | None = MEASURED):
    """A versioned row of the running turn, as the program writes before each model call."""
    return row("api-request", second, **({"version": version} if version is not None else {}))


def absorbed(text: str, second: float = 2, version: str | None = MEASURED):
    """Tonight's running-turn chain: enqueue, remove absorbed mid-turn, the queued_command attachment joined by its ids."""
    ids = dict(commandUuid="synthetic-command", deliveryId="synthetic-delivery")
    attachment = row("attachment", second - 0.002, attachment=dict(
        type="queued_command", prompt=pasted(text), source_uuid=ids["commandUuid"], delivery_id=ids["deliveryId"],
        commandMode="prompt", origin=dict(kind="human"), humanTurn=True),
        **({"version": version} if version is not None else {}))
    return [queue(pasted(text), second), queue(pasted(text), second + 7, "remove", reason="absorbed_mid_turn", **ids),
            attachment, request(second + 7.02, version)]


def test_claude_records_a_tab_as_four_spaces_on_the_measured_version_only(tmp_path):
    claude = harness.get("claude")
    path = record(tmp_path, request(2))
    assert claude.recorded_paste("a\tb\t\tc", path, SINCE) == "a    b        c"
    assert claude.recorded_paste("no tab  here\n", path, SINCE) == "no tab  here\n"


@pytest.mark.parametrize("name", [name for name in harness.NAMES if name != "claude"])
def test_other_adapters_record_the_payload_unchanged(tmp_path, name):
    path = record(tmp_path, request(2))
    assert harness.get(name).recorded_paste(PAYLOAD, path, SINCE) == PAYLOAD


def test_tonights_absorbed_chain_confirms_as_it_did(tmp_path):
    """Pinned: enqueue, its remove absorbed mid-turn with commandUuid and deliveryId, the attachment joined by them."""
    chain = absorbed("Synthetic answer without a tab")
    remove, attachment = chain[1], chain[2]
    assert (remove["commandUuid"], remove["deliveryId"]) == (attachment["attachment"]["source_uuid"],
                                                             attachment["attachment"]["delivery_id"])
    path = record(tmp_path, *working(), *chain, *finish())
    assert input_receipt.confirmed("claude", path, "Synthetic answer without a tab", SINCE) == "delivered"


def test_a_tab_payload_absorbed_into_a_running_turn_is_delivered(tmp_path):
    path = record(tmp_path, *working(), *absorbed(RECORDED), *finish())
    assert input_receipt.confirmed("claude", path, PAYLOAD, SINCE) == "delivered"


def test_a_tab_payload_starting_a_turn_is_delivered(tmp_path):
    path = record(tmp_path, dict(user(pasted(RECORDED), 2), version=MEASURED))
    assert input_receipt.confirmed("claude", path, PAYLOAD, SINCE) == "delivered"


def test_a_tab_payload_only_queued_is_queued_not_delivered(tmp_path):
    path = record(tmp_path, *working(), queue(pasted(RECORDED), 2), request(3))
    assert input_receipt.confirmed("claude", path, PAYLOAD, SINCE) == "queued"


def test_the_payload_as_typed_still_confirms(tmp_path):
    path = record(tmp_path, *working(), *absorbed(PAYLOAD), *finish())
    assert input_receipt.confirmed("claude", path, PAYLOAD, SINCE) == "delivered"


@pytest.mark.parametrize("versions", [
    ("2.1.318",),  # another version: not measured
    (None,),  # no versioned row since the paste
    (MEASURED, "2.1.318"),  # mixed: the program changed under the record
])
def test_an_unmeasured_or_unknown_version_has_no_tab_form(tmp_path, versions):
    rows = absorbed(RECORDED, version=versions[0]) + [request(9.5, version) for version in versions[1:]]
    path = record(tmp_path, *working(), *rows, *finish())
    assert input_receipt.confirmed("claude", path, PAYLOAD, SINCE) is None


def test_a_version_before_the_paste_does_not_count(tmp_path):
    path = record(tmp_path, request(0.5), *working(), *absorbed(RECORDED, version=None), *finish())
    assert input_receipt.confirmed("claude", path, PAYLOAD, SINCE) is None


@pytest.mark.parametrize("form", [
    PAYLOAD.replace("\t", " "),  # one space: not the measured form
    PAYLOAD.replace("\t", "  "),
    PAYLOAD.replace("\t", "        "),  # tab stops of eight
    PAYLOAD.replace("\t", "    ", 1),  # one TAB rendered, the other kept
    PAYLOAD.replace("\t", "    ", 1).replace("\t", ""),  # one TAB rendered, the other dropped
    RECORDED.replace("second stamp", "other stamp"),  # different words
    RECORDED + "a further line",
    "prefix " + RECORDED,
])
def test_any_other_rendering_stays_unconfirmed(tmp_path, form):
    path = record(tmp_path, *working(), *absorbed(form), *finish())
    assert input_receipt.confirmed("claude", path, PAYLOAD, SINCE) is None


def test_the_four_space_form_before_the_paste_is_stale(tmp_path):
    path = record(tmp_path, *working(), *absorbed(RECORDED, second=0.5), *finish())
    assert input_receipt.confirmed("claude", path, PAYLOAD, SINCE + timedelta(seconds=0.1)) is None


def versioned_input(name, text, at):
    entry = native_input(name, text, at)
    return dict(entry, version=MEASURED) if name == "claude" else entry


def test_closed_record_counts_the_four_space_form_as_present_for_claude_alone(world):  # noqa: F811
    w = world
    w.live["pane"] = None
    w.proof.data["payload"] = PAYLOAD
    append(w.record, versioned_input(w.name, RECORDED, stamp(11)))
    # Claude 2.1.317 records the payload so: present, never "not in its closed record". Other readers keep absence.
    assert closed_record.not_in_closed_record(w.seat, w.proof) is (w.name != "claude")


def test_closed_record_still_finds_absence_of_a_tab_payload(world):  # noqa: F811
    w = world
    w.live["pane"] = None
    w.proof.data["payload"] = PAYLOAD
    append(w.record, versioned_input(w.name, "Synthetic other input", stamp(11)))
    assert closed_record.not_in_closed_record(w.seat, w.proof)


TAGGED = "qd > synthetic question\nthe <pasted_content> wrapper is unwrapped first\nend of question\n"
TAGGED_RECORDED = TAGGED.replace("<pasted_content", "<\\pasted_content")


def test_claude_escapes_a_quoted_opening_tag_on_the_measured_version_only(tmp_path):
    claude = harness.get("claude")
    path = record(tmp_path, request(2))
    assert claude.recorded_paste("a <pasted_content> b\tc", path, SINCE) == "a <\\pasted_content> b    c"
    (tmp_path / "other").mkdir()
    other = record(tmp_path / "other", request(2, "2.1.318"))
    assert claude.recorded_paste("a <pasted_content> b", other, SINCE) == "a <pasted_content> b"


@pytest.mark.parametrize("chain", ["prompt", "absorbed"])
def test_a_payload_quoting_the_wrapper_tag_is_delivered(tmp_path, chain):
    rows = ([dict(user(pasted(TAGGED_RECORDED), 2), version=MEASURED)] if chain == "prompt"
            else [*working(), *absorbed(TAGGED_RECORDED), *finish()])
    path = record(tmp_path, *rows)
    assert input_receipt.confirmed("claude", path, TAGGED, SINCE) == "delivered"


def test_both_forms_together_are_delivered(tmp_path):
    payload = TAGGED + "a\tb\n"
    path = record(tmp_path, *working(), *absorbed(payload.replace("\t", "    ").replace("<pasted_content", "<\\pasted_content")),
                  *finish())
    assert input_receipt.confirmed("claude", path, payload, SINCE) == "delivered"


@pytest.mark.parametrize("form", [
    TAGGED.replace("<pasted_content", "<\\\\pasted_content"),  # escaped twice
    TAGGED.replace("<pasted_content", "&lt;pasted_content"),  # another escaping
    TAGGED.replace("<pasted_content", "pasted_content"),  # the bracket dropped
])
def test_any_other_rendering_of_the_tag_stays_unconfirmed(tmp_path, form):
    path = record(tmp_path, *working(), *absorbed(form), *finish())
    assert input_receipt.confirmed("claude", path, TAGGED, SINCE) is None


def test_a_quoted_closing_tag_has_no_measured_form(tmp_path):
    payload = "a quoted </pasted_content> closing tag\n"
    path = record(tmp_path, *working(), *absorbed(payload.replace("</pasted_content", "<\\/pasted_content")), *finish())
    assert input_receipt.confirmed("claude", path, payload, SINCE) is None


def test_an_unmeasured_version_leaves_the_tag_unescaped(tmp_path):
    path = record(tmp_path, *working(), *absorbed(TAGGED_RECORDED, version="2.1.318"), *finish())
    assert input_receipt.confirmed("claude", path, TAGGED, SINCE) is None

