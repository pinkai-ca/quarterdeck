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


def read(text: str) -> Screen:
    plain = SGR.sub("", text)
    if any(ord(char) < 32 and char != "\n" for char in plain):
        return Screen(plain, None, unknown="unmeasured Muse terminal control")
    if not plain.strip():
        return Screen(plain, None, starting=True)
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
