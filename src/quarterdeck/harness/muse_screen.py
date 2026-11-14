"""Muse 1.4.20's measured composer, preserving style and every input row."""

from __future__ import annotations

from dataclasses import dataclass
import re

from .types import Box, Interruption, Screen
from .muse_record import FOLDED_INPUT

SGR = re.compile(r"\x1b\[([0-9;]*)m")
RULE = re.compile(r"─{8,}")
TYPED = 252
MUTED = 242
PROMPT = 75
IDLE_HINT = "Type @ to search and insert workspace file paths"
EXPAND_HINT = "Paste again to expand for editing"
WORKING = re.compile(r"(?:Thinking|Working|Double checking) \([0-9]+s · esc to interrupt\)")
CONTRIBUTOR_OFF = "Contributor mode is off, so your content won't be used for product improvement."
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
    foreground: tuple[int, int, int] | int | None = None
    background: tuple[int, int, int] | int | None = None


def characters(text: str):
    """The measured SGR forms; attributes never delete characters."""
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
                if codes[at + 1:at + 2] == [5] and len(codes) >= at + 3:
                    color = codes[at + 2]
                    if color not in (75, 237, 242, 244, 246, 247, 251, 252):
                        return None
                    if code == 38:
                        foreground = color
                    else:
                        background = color
                    at += 2
                elif codes[at + 1:at + 2] == [2] and len(codes) >= at + 5:
                    rgb = tuple(codes[at + 2:at + 5])
                    if any(value < 0 or value > 255 for value in rgb):
                        return None
                    if code == 38:
                        foreground = rgb
                    else:
                        background = rgb
                    at += 4
                else:
                    return None
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
        return Screen(text, None, working=None, unknown="unmeasured Muse terminal style or control")
    plain, styles = decoded
    if not plain.strip():
        return Screen(plain, None, working=None, starting=True)
    for interruption in MODEL_CHOICES:
        if interruption.seen_in(plain):
            # An offered option proves neither selection nor a catalog id.
            # No dismissal key is declared, even when the screen says esc.
            return Screen(plain, None, working=None, interruption=interruption)
    if TRUST.seen_in(plain):
        return Screen(plain, None, working=None, interruption=TRUST)
    rows = plain.split("\n")
    offsets, offset = [], 0
    for row in rows:
        offsets.append(offset)
        offset += len(row) + 1
    if not any(row.strip() == "Muse Code 1.4.20" for row in rows):
        return Screen(plain, None, working=None, unknown="unmeasured Muse header")
    prompts = [index for index, row in enumerate(rows)
               if index > 0 and RULE.fullmatch(rows[index - 1])
               and (row.startswith("❯ ") or row == "❯")
               and styles[offsets[index]].background is None
               and styles[offsets[index]].foreground in (None, PROMPT)]
    if len(prompts) != 1:
        return Screen(plain, None, working=None, unknown="Muse composer is not uniquely bound to its rule")
    index = prompts[0]
    if len(rows[index - 1]) != 120:
        return Screen(plain, None, working=None, unknown="Muse 1.4.20 composer width not measured")
    bottom = next((stop for stop in range(index + 1, len(rows)) if rows[stop] == rows[index - 1]), None)
    if bottom is None:
        return Screen(plain, None, working=None, unknown="unmeasured Muse composer boundaries")
    body, body_styles = [], []
    for number in range(index, bottom):
        row = rows[number]
        prefix = "❯ " if number == index else "  "
        if number == index and row == "❯":
            prefix = "❯"
        if not row.startswith(prefix):
            return Screen(plain, None, working=None, unknown="unmeasured Muse continuation row")
        if any(style.background is not None for style in styles[offsets[number]:offsets[number] + len(prefix)]):
            return Screen(plain, None, working=None, unknown="Muse composer prefix has an unmeasured background")
        start = offsets[number] + len(prefix)
        content = row[len(prefix):]
        attributes = styles[start:start + len(content)]
        if any(attribute.background is not None for attribute in attributes):
            return Screen(plain, None, working=None, unknown="Muse composer has an unmeasured background")
        body.append(content)
        body_styles.append(attributes)
    footer = "\n".join(rows[bottom + 1:])
    if not re.search(r"^  \S[^\n]* · [^\n]+ · Launch overrides", footer, re.MULTILINE):
        return Screen(plain, None, working=None, unknown="unmeasured Muse footer")
    working = bool(WORKING.search("\n".join(rows[:index - 1])))
    folded = len(body) == 1 and FOLDED_INPUT.fullmatch(body[0]) is not None and EXPAND_HINT in footer
    if not folded and any(style.foreground not in (None, TYPED) for attributes in body_styles for style in attributes):
        return Screen(plain, None, working=None, unknown="unmeasured Muse composer text or contextual tip")
    content = ComposerText("\n".join(body), len(rows[index - 1]) - 2, folded)
    # These transitional labels cannot establish a ready idle composer.
    if not working and any(label in footer for label in ("steering the running turn", "interrupting run")):
        return Screen(plain, content, working=None, unknown="Muse turn transition still in progress")
    return Screen(plain, content, working=working,
                  queued=int("Queued input · ↑ to edit" in plain))


def empty_cell(screen: Screen) -> tuple[int, int] | None:
    """The measured two-cell prompt, for Door's cursor-bracketed padding proof.

    Spaces alone remain text until the same owned frame positively shows its
    visible cursor at the first input cell; the parser never strips a draft.
    """
    if (screen.unknown is not None or screen.interruption is not None or screen.starting
            or screen.working is not False or not screen.box
            or any(char != ' ' for char in screen.box)):
        return None
    rows = screen.text.split('\n')
    prompts = [index for index, row in enumerate(rows)
               if index > 0 and rows[index - 1] == '─' * 120 and row.startswith('❯ ')]
    if len(prompts) != 1:
        return None
    index = prompts[0]
    if index + 1 >= len(rows) or rows[index + 1] != '─' * 120:
        return None  # only the measured single empty composer row
    return 2, index


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


def input_shape_reason(text: str) -> str | None:
    if '\n' in text or '\r' in text or '\t' in text:
        return 'Muse 1.4.20 multiline/final-LF/tab paste projection unsupported; no input'
    # The current echo draft proves a 55-cell ordinary single row. No old
    # version's wrap threshold or collapsed count is borrowed for longer text.
    if len(text) > 55 or any(not (' ' <= char <= '~') for char in text):
        return 'Muse 1.4.20 folded/wrapped or non-ASCII paste projection unsupported; no input'
    return None


def counted_characters(text: str) -> str | None:
    return None  # collapsed counts have not been remeasured at 1.4.20


def wrapped_rows(text: str, columns: int | None) -> list[str] | None:
    if input_shape_reason(text) is not None or columns != 118:
        return None
    return [text]
