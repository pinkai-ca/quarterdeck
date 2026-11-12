"""Muse 1.4.15's measured composer, preserving style and every input row."""

from __future__ import annotations

from dataclasses import dataclass
import re

from .types import Box, Interruption, Screen
from .muse_record import FOLDED_INPUT

SGR = re.compile(r"\x1b\[([0-9;]*)m")
RULE = re.compile(r"─{8,}")
TYPED = (204, 211, 219)
MUTED = (103, 108, 116)
PROMPT = (90, 160, 255)
IDLE_HINT = "Type @ to search and insert workspace file paths"
EXPAND_HINT = "Paste again to expand for editing"
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


@dataclass(frozen=True)
class Style:
    foreground: tuple[int, int, int] | None = None
    background: tuple[int, int, int] | None = None


def characters(text: str):
    """The measured truecolor SGR subset; attributes never delete characters."""
    plain, styles, position = [], [], 0
    foreground = background = None
    while position < len(text):
        char = text[position]
        if char != "\x1b":
            if ord(char) < 32 and char != "\n" or ord(char) == 127:
                return None
            plain.append(char)
            styles.append(Style(foreground, background))
            position += 1
            continue
        match = SGR.match(text, position)
        if match is None:
            return None
        try:
            codes = [int(value) if value else 0 for value in match[1].split(";")]
        except ValueError:
            return None
        at = 0
        while at < len(codes):
            code = codes[at]
            if code == 0:
                foreground = background = None
            elif code == 39:
                foreground = None
            elif code == 49:
                background = None
            elif code in (38, 48):
                if codes[at + 1:at + 2] != [2] or len(codes) < at + 5:
                    return None
                rgb = tuple(codes[at + 2:at + 5])
                if any(value < 0 or value > 255 for value in rgb):
                    return None
                if code == 38:
                    foreground = rgb
                else:
                    background = rgb
                at += 4
            elif code not in (1, 2, 3, 4, 9, 22, 23, 24, 29):
                return None
            at += 1
        position = match.end()
    return "".join(plain), styles


class ComposerText(str):
    """Content, prefix-derived width bound and fold hint; not a capacity claim."""

    def __new__(cls, text, columns=None, folded=False):
        value = super().__new__(cls, text)
        value.columns, value.folded = columns, folded
        return value


def read(text: str) -> Screen:
    decoded = characters(text)
    if decoded is None:
        return Screen(text, None, unknown="unmeasured Muse terminal style or control")
    plain, styles = decoded
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
    offsets, offset = [], 0
    for row in rows:
        offsets.append(offset)
        offset += len(row) + 1
    if not any(row.strip() == "Muse Code 1.4.15" for row in rows):
        return Screen(plain, None, unknown="unmeasured Muse header")
    prompts = [index for index, row in enumerate(rows)
               if index > 0 and RULE.fullmatch(rows[index - 1])
               and (row.startswith("❯ ") or row == "❯")
               and styles[offsets[index]].background is None
               and styles[offsets[index]].foreground in (None, PROMPT)]
    if len(prompts) != 1:
        return Screen(plain, None, unknown="Muse composer is not uniquely bound to its rule")
    index = prompts[0]
    bottom = next((stop for stop in range(index + 1, len(rows)) if rows[stop] == rows[index - 1]), None)
    if bottom is None:
        return Screen(plain, None, unknown="unmeasured Muse composer boundaries")
    body, body_styles = [], []
    for number in range(index, bottom):
        row = rows[number]
        prefix = "❯ " if number == index else "  "
        if number == index and row == "❯":
            prefix = "❯"
        if not row.startswith(prefix):
            return Screen(plain, None, unknown="unmeasured Muse continuation row")
        if any(style.background is not None for style in styles[offsets[number]:offsets[number] + len(prefix)]):
            return Screen(plain, None, unknown="Muse composer prefix has an unmeasured background")
        start = offsets[number] + len(prefix)
        content = row[len(prefix):]
        attributes = styles[start:start + len(content)]
        if any(attribute.background is not None for attribute in attributes):
            return Screen(plain, None, unknown="Muse composer has an unmeasured background")
        body.append(content)
        body_styles.append(attributes)
    footer = "\n".join(rows[bottom + 1:])
    if not re.search(r"(?:echo|meta) · .+ · Launch overrides", footer):
        return Screen(plain, None, unknown="unmeasured Muse footer")
    working = bool(WORKING.search("\n".join(rows[:index - 1])))
    folded = len(body) == 1 and FOLDED_INPUT.fullmatch(body[0]) is not None and EXPAND_HINT in footer
    if (not working and len(body) == 1 and body[0] == IDLE_HINT
            and all(style.foreground == MUTED for style in body_styles[0])):
        body = [""]
    elif not folded and any(style.foreground not in (None, TYPED) for attributes in body_styles for style in attributes):
        return Screen(plain, None, unknown="unmeasured Muse composer text or contextual tip")
    content = ComposerText("\n".join(body), len(rows[index - 1]) - 2, folded)
    # These transitional labels cannot establish a ready idle composer.
    if not working and any(label in footer for label in ("steering the running turn", "interrupting run")):
        return Screen(plain, content, unknown="Muse turn transition still in progress")
    return Screen(plain, content, working=working,
                  queued=int("Queued input · ↑ to edit" in plain))


@dataclass(frozen=True)
class VisibleBox(Box):
    def matches(self, shown: str | None, columns=None, slack=0) -> bool:
        if shown is None:
            return False
        if getattr(shown, "folded", False):
            marker = FOLDED_INPUT.fullmatch(shown)
            return marker is not None and counted_characters(self.text) == marker[1]
        measured = getattr(shown, "columns", None)
        if columns is not None and measured is not None and columns != measured:
            return False
        width = columns if columns is not None else measured
        projected = wrapped_rows(self.text, width)
        return projected is not None and "\n".join(projected) == shown


def counted_characters(text: str) -> str | None:
    # S3b establishes counts for its measured ordinary character shapes. Keep
    # unmeasured wide/combining/control forms out until their count is relayed.
    # The canonical post adds a middle dot; its full native count was measured
    # separately. The native marker counts each tab as four, not one. This
    # count does not establish unmeasured interior-tab model/transport behavior.
    return (str(len(text) + 3 * text.count("\t"))
            if all(" " <= char <= "~" or char in "\n\t·" for char in text) else None)


def wrapped_rows(text: str, columns: int | None) -> list[str] | None:
    """ASCII/middle-dot words with the observed word-plus-space budget.

    columns is the prefix-derived W-2 bound, not measured physical capacity.
    At panes 80/120 only, the observed wrap budget is one less: W-3. A move
    requires a nonempty row and no inner break that would fit on that row.
    Final words at W-3/W-2 and all internal-word projections remain unknown.
    """
    if any(not (" " <= char <= "~" or char in "\n·") for char in text):
        return None
    result = []
    for line in text.split("\n"):
        if columns is None:
            result.append(line)
            continue
        budget = columns - 1
        if budget < 1 or any(len(word) >= budget for word in line.split(" ")):
            return None
        if len(line) < budget:
            result.append(line)
            continue
        if columns not in (78, 118):
            return None  # no wrap-budget extrapolation to an unmeasured pane
        if line.startswith(" ") or line.endswith(" ") or "  " in line:
            return None  # repeated/edge-space wrapping was not established
        words, row = line.split(" "), ""
        for index, word in enumerate(words):
            final = index == len(words) - 1
            piece = word if final else word + " "
            end = len(row) + len(piece)
            if final and end in (budget, budget + 1):
                return None  # final placement at W-3/W-2 is unmeasured
            move_limit = budget + 1 if final else budget
            if end > move_limit:
                if not row:
                    return None
                # A final punctuation character is not an inner break. Never
                # project a split when an earlier non-alphanumeric prefix fits.
                prefix = next((word[:at + 1] for at, char in enumerate(word[:-1])
                               if not char.isalnum()), None)
                if prefix is not None and len(row) + len(prefix) <= budget:
                    return None
                result.append(row)
                row = piece
            else:
                row += piece
        result.append(row)
    return result
