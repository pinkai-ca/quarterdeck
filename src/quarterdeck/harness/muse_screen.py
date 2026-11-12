"""The narrow Muse 1.4.15 composer established by offline TUI observations.

No placeholder was observed. Multiline/folded drafts and control menus remain
unmeasured; they never acquire a whole-paste match from this reader.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from .types import Box, Interruption, Screen

SGR = re.compile(r"\x1b\[[0-9;]*m")
RULE = re.compile(r" *─{8,} *")
WORKING = re.compile(r"(?:Thinking|Double checking) \([0-9]+s · esc to interrupt\)")
TRUST = Interruption("Muse workspace trust", r"Do you trust this workspace\?")


def label(text: str) -> str:
    """Recognize static words across line breaks without assuming menu layout."""
    return r"[ \n]+".join(re.escape(word) for word in text.split(" "))


OFFER_TITLE = label("You're using your quota fast")
OFFER_OPTIONS = "|".join(label(text) for text in
                         ("Keep going", "Switch to Spark", "Turn on contributor mode"))
SWITCH_OPTIONS = "|".join(label(text) for text in ("Switch to Spark", "Turn on contributor mode"))
QUOTA_OFFER = Interruption("Muse quota offer: model switch or contributor mode",
                           rf"(?:{SWITCH_OPTIONS})|(?s:(?=.*{OFFER_TITLE})(?=.*(?:{OFFER_OPTIONS})))")
QUOTA_INCOMPLETE = Interruption("Muse quota offer: options unreadable", OFFER_TITLE)
CONTRIBUTOR_ENABLED = Interruption("Muse contributor mode enabled", "|".join(label(text) + r"(?![A-Za-z])" for text in (
    "Contributor mode on",
    "Contributor mode is on, so your content may be used for product improvement. Turn it off in /settings.",
    "Contributor mode is on for this session only: settings could not be saved.",
)))
MODEL_SWITCHED = Interruption("Muse model switch reported: Spark", label("Switched to Spark (saved as default)"))
MODEL_CHOICES = (QUOTA_OFFER, QUOTA_INCOMPLETE, CONTRIBUTOR_ENABLED, MODEL_SWITCHED)


def read(text: str) -> Screen:
    plain = SGR.sub("", text)
    if any(ord(char) < 32 and char != "\n" for char in plain):
        return Screen(plain, None, unknown="unmeasured Muse terminal control")
    if not plain.strip():
        return Screen(plain, None, starting=True)
    for interruption in MODEL_CHOICES:
        if interruption.seen_in(plain):
            # An offered option proves neither selection nor a catalog id.
            # No dismissal key is declared, even when the screen says esc.
            return Screen(plain, None, interruption=interruption)
    if TRUST.seen_in(plain):
        return Screen(plain, None, interruption=TRUST)
    rows = plain.split("\n")
    if not any(row.strip() == "Muse Code 1.4.15" for row in rows):
        return Screen(plain, None, unknown="unmeasured Muse header")
    prompts = [index for index, row in enumerate(rows) if re.match(r" *❯(?: |$)", row)]
    if len(prompts) != 1:
        return Screen(plain, None, unknown="Muse composer is not a single visible row")
    index = prompts[0]
    if (index == 0 or index + 1 >= len(rows) or not RULE.fullmatch(rows[index - 1])
            or not RULE.fullmatch(rows[index + 1]) or rows[index - 1] != rows[index + 1]):
        return Screen(plain, None, unknown="unmeasured Muse composer boundaries")
    footer = "\n".join(rows[index + 2:])
    if not re.search(r"(?:echo|meta) · .+ · Launch overrides", footer):
        return Screen(plain, None, unknown="unmeasured Muse footer")
    content = re.match(r" *❯ ?(.*)$", rows[index])[1]
    working = bool(WORKING.search("\n".join(rows[:index - 1])))
    # These transitional labels cannot establish a ready idle composer.
    if not working and any(label in footer for label in ("steering the running turn", "interrupting run")):
        return Screen(plain, content, unknown="Muse turn transition still in progress")
    return Screen(plain, content, working=working,
                  queued=int("Queued input · ↑ to edit" in plain))


@dataclass(frozen=True)
class VisibleBox(Box):
    def matches(self, shown: str | None, columns=None, slack=0) -> bool:
        # There is no measured wrapping/folding rule. Painted right padding
        # cannot prove a payload's trailing spaces, nor an omitted blank line.
        return (shown is not None and "\n" not in self.text and "\t" not in self.text
                and not self.text.endswith(" ") and shown.rstrip(" ") == self.text)
