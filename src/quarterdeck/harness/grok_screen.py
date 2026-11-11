"""Grok Build 1.0.77's fullscreen composer, measured with and without color.

SGR belongs to the adapter: callers receive ordinary Screen text. A dim or muted
span alone never makes text a placeholder. The one established argument hint
also requires the model picker, its command prefix and its exact native text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .types import Interruption, Screen

SGR = re.compile(r"\x1b\[([0-9;]*)m")
FOOTER = re.compile(
    r"^  ╰─+ (Grok [0-9.]+(?: Fast)?) \((low|medium|high|xhigh)\) · (always-approve) ─╯ *$"
)
MODEL_HINT = "<model> [window] [effort]"
COMMANDS = Interruption("commands", r"┌─ Commands ─.*\[✗\].*┐", "Escape")
MODEL_PICKER = Interruption("model picker", r"Grok [0-9.]+ \(current\)", "C-c")


@dataclass(frozen=True)
class Style:
    foreground: tuple[int, int, int] | None = None
    faint: bool = False


def characters(text: str) -> tuple[str, list[Style]] | None:
    """Decode the SGR subset tmux emits; other terminal controls stay unknown."""
    plain, styles, position = [], [], 0
    foreground, faint = None, False
    while position < len(text):
        if text[position] != "\x1b":
            char = text[position]
            if ord(char) < 32 and char != "\n":
                return None
            plain.append(char)
            styles.append(Style(foreground, faint))
            position += 1
            continue
        match = SGR.match(text, position)
        if match is None:
            return None
        codes = [int(value) if value else 0 for value in match[1].split(";")]
        at = 0
        while at < len(codes):
            code = codes[at]
            if code == 0:
                foreground, faint = None, False
            elif code == 2:
                faint = True
            elif code == 22:
                faint = False
            elif code == 39:
                foreground = None
            elif code in (38, 48):
                if codes[at + 1:at + 2] == [2] and len(codes) >= at + 5:
                    rgb = tuple(codes[at + 2:at + 5])
                    if any(value > 255 for value in rgb):
                        return None
                    if code == 38:
                        foreground = rgb
                    at += 4
                else:
                    return None  # indexed palettes have not been measured
            elif code not in (1, 3, 4, 7, 9, 23, 24, 27, 29, 49):
                return None
            at += 1
        position = match.end()
    return "".join(plain), styles


def read(text: str) -> Screen:
    decoded = characters(text)
    if decoded is None:
        return Screen(text, box=None, unknown="unmeasured Grok terminal styling or control sequence")
    plain, style = decoded
    if not plain.strip():
        return Screen(plain, box=None, starting=True)
    rows = plain.split("\n")
    offsets, offset = [], 0
    for row in rows:
        offsets.append(offset)
        offset += len(row) + 1
    footer_at = next((i for i in range(len(rows) - 1, -1, -1) if FOOTER.fullmatch(rows[i])), None)
    if footer_at is None:
        return Screen(plain, box=None, unknown="no fullscreen composer drawn as Grok 1.0.77 draws it")
    footer = FOOTER.fullmatch(rows[footer_at])
    shows = dict(model=footer[1], effort=footer[2])
    if COMMANDS.seen_in(plain) and "Enter select  |  Esc close" in plain:
        return Screen(plain, box=None, interruption=COMMANDS, shows=shows)
    # An unrecognized rectangular modal is never an ordinary composer, even if
    # the program has left its footer visible underneath.
    if any("┌" in row or "└" in row for row in rows[:footer_at]):
        return Screen(plain, box=None, unknown="unmeasured Grok modal", shows=shows)
    top = next((i for i in range(footer_at - 1, -1, -1)
                if re.fullmatch(r"  ╭─+╮ *", rows[i])), None)
    if top is None or footer_at - top < 2:
        return Screen(plain, box=None, unknown="Grok composer boundary incomplete", shows=shows)
    right = rows[top].index("╮")
    if right < 10 or len(rows[footer_at].rstrip(" ")) != right + 1:
        return Screen(plain, box=None, unknown="unmeasured Grok composer width", shows=shows)
    body = []
    for index in range(top + 1, footer_at):
        row = rows[index]
        prefix = "  │ ❯ " if index == top + 1 else "  │   "
        if not row.startswith(prefix) or not row.endswith("│" + rows[top][right + 1:]):
            return Screen(plain, box=None, unknown="Grok composer row incomplete or overlaid", shows=shows)
        # Use the border suffix rather than character offsets: wide user text
        # occupies terminal cells, not necessarily one Python character each.
        end = len(row) - len(rows[top][right + 1:]) - 1
        content = row[len(prefix):end]
        begin = offsets[index] + len(prefix)
        body.append((content, style[begin:begin + len(content)]))
    picker = (len(body) == 1 and body[0][0].startswith("/model ")
              and MODEL_PICKER.seen_in(plain)
              and any(re.fullmatch(r"  ─{8,}.*", row) for row in rows[:top]))
    if picker:
        content, attributes = body[0]
        tail = content[len("/model "):].rstrip(" ")
        hint_styles = attributes[len("/model "):len("/model ") + len(MODEL_HINT)]
        hint = (tail == MODEL_HINT and len(hint_styles) == len(MODEL_HINT)
                and all(s.faint or s.foreground == (108, 108, 108) for s in hint_styles))
        # Only this measured hint is excluded from the actual draft. A literal
        # bright copy stays content, including when it spells the hint exactly.
        box = "/model " if hint else content
        return Screen(plain, box=box, interruption=MODEL_PICKER, shows=shows)
    for content, attributes in body:
        for char, attribute in zip(content, attributes):
            if not char.isspace() and (attribute.faint or attribute.foreground not in
                                      (None, (225, 225, 225), (122, 162, 247))):
                return Screen(plain, box=None, unknown="unmeasured Grok composer text styling", shows=shows)
    box = "\n".join(content for content, _ in body)
    if len(body) > 1 and not body[-1][0].strip(" "):
        # The native measurements so far establish no trailing pad-row rule.
        # An explicit empty input line may paint identically: retain every row
        # and hold instead of silently converting either case to a shorter draft.
        return Screen(plain, box=box, unknown="Grok trailing blank input row or unmeasured padding", shows=shows)
    tail = "\n".join(rows[footer_at + 1:])
    if not ("Shift+Tab:mode" in tail and "Ctrl+x:shortcuts" in tail
            or re.search(r"Grok Build\s+1\.0\.77", tail)):
        return Screen(plain, box=None, unknown="unmeasured Grok composer hint", shows=shows)
    return Screen(plain, box=box, shows=shows)
