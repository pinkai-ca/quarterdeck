"""Grok Build 1.0.77's fullscreen composer, measured with and without color.

SGR belongs to the adapter: callers receive ordinary Screen text. A dim or muted
span alone never makes text a placeholder. The one established argument hint
also requires the model picker, its command prefix and its exact native text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .types import Box, Interruption, Screen, _cells

SGR = re.compile(r"\x1b\[([0-9;]*)m")
FOOTER = re.compile(
    # The remaining weekly allowance is optional native footer text, not a quota wall.
    r"^  ╰─+ (?:Weekly limit left: (?:100|[1-9]?[0-9])% · )?"
    r"(Grok [0-9.]+(?: Fast)?) \((low|medium|high|xhigh)\) · (always-approve) ─╯ *$"
)
# The xAI API-key launch and short draft show this model id without an effort.
XAI_KEY_FOOTER = re.compile(r"  ╰─+ (grok-4\.20-0309-non-reasoning) · always-approve ─╯ *")
XAI_KEY_BANNER = "Logged in with API key"
XAI_KEY_VERSION = "Grok Build  1.0.77"
XAI_KEY_DRAFT_HINT = re.compile(r"  Enter:send  │  Alt\+Enter:newline  │  Shift\+Tab:mode  │  Ctrl\+x:shortcuts *")
XAI_KEY_IDLE_HINT = "  Shift+Tab:mode  │  Ctrl+x:shortcuts"
XAI_KEY_WORKING_HINT = "  Shift+Tab:mode  │  Ctrl+c:cancel  │  Ctrl+x:shortcuts"
MODEL_HINT = "<model> [window] [effort]"
COMMANDS = Interruption("commands", r"┌─ Commands ─.*\[✗\].*┐", "Escape")
MODEL_PICKER = Interruption("model picker", r"Grok [0-9.]+ \(current\)", "C-c")
# The first-run question on keeping coding data for training, measured on 1.0.77 (M1 to M3, 12 and 13
# November, 112 x 35): one blank row, the title row with its two choices, the body, one blank row, then the
# composer's top border. Its choices are bracketed buttons with no key; the program asks for SGR mouse
# reports while it shows. The Admiral's answer is no: the framework clicks [Opt out], chosen by its text,
# and never [Opt in].
TRAINING_TITLE = "Help improve Grok"
TRAINING_TITLE_ROW = re.compile(r"  Help improve Grok +\[Opt out\] \[Opt in\] *")
TRAINING_BODY = ("Off by default. Opt-in to allow SpaceXAI to retain coding data, e.g., prompts, traces, & metrics, "
                 "for training and debugging purposes. Change anytime via settings. Read Terms and Privacy Policy.")
TRAINING_QUESTION = Interruption(
    "first-run training-data question (Help improve Grok) for the account holder",
    r"Help improve Grok", None, click="[Opt out]")
# The same question in any other form: named and held for the account holder, and nothing is pressed.
TRAINING_ALTERED = Interruption(
    "first-run training-data question (Help improve Grok) for the account holder, not as measured; nothing pressed",
    r"Help improve Grok", None)
FRESH = "Grok composer with no hint row: idle only after this launch answered its training question"
TRAINING_NEAR = 8  # rows above the composer's top border in which an altered question is looked for
PASTE_FOLD_LINES = 4
PASTE_LABEL = re.compile(r"\[Pasted: ([1-9][0-9]*) lines\]")
XAI_KEY_PANE_ROWS = 24
XAI_KEY_BODY_ROWS = 10  # 1.0.77 at 80 × 24, including the final blank row
# A queued response without the foreground-tool shortcut, measured at 256 columns.
RESPONSE_QUEUE_HINT = ("Enter:send now", "Shift+Tab:mode", "Ctrl+c:cancel", "Ctrl+;:queue", "Ctrl+x:shortcuts")
UNKNOWN_HINT = "unmeasured Grok composer hint"
QUEUE_DETAIL = "native Grok queue shows the submitted envelope; awaiting exact record receipt"
QUEUE_LOST = "pending delivery; native Grok queue entry cannot be verified; never resent"
HINT_STATES = {
    ("Shift+Tab:mode", "Ctrl+x:shortcuts"): False,
    ("Enter:send", "Alt+Enter:newline", "Shift+Tab:mode", "Ctrl+x:shortcuts"): False,
    # The same idle draft where the program's environment names a VTE terminal new enough for Shift+Enter
    # (VTE_VERSION 8400, inherited from the operator's terminal), measured on 13 November at 80 x 24 after a
    # folded paste: Shift+Enter joins the newline key and Ctrl+x is not drawn.
    ("Enter:send", "Shift+Enter/Alt+Enter:newline", "Shift+Tab:mode"): False,
    ("Shift+Tab:mode", "Ctrl+c:cancel", "Ctrl+x:shortcuts"): True,
    ("Shift+Tab:mode", "Ctrl+c:cancel", "Ctrl+b:send to bg", "Ctrl+x:shortcuts"): True,
    # At 80 columns the working draft drops Ctrl+x, while retaining cancel.
    ("Enter:queue", "Alt+Enter:newline", "Shift+Tab:mode", "Ctrl+c:cancel"): True,
    # At 256 columns the same working draft also draws Ctrl+x and a chip whose text is
    # "Enter Enter:send now". Measured on 14 November from a crew pane's paste screen.
    # The chip is drawn; the immediate send-now action stays unproved.
    ("Enter:queue", "Alt+Enter:newline", "Shift+Tab:mode", "Ctrl+c:cancel",
     "Enter Enter:send now", "Ctrl+x:shortcuts"): True,
    ("Enter:send now", "Shift+Tab:mode", "Ctrl+c:cancel", "Ctrl+b:send to bg"): True,
    ("Enter:send now", "Shift+Tab:mode", "Ctrl+c:cancel", "Ctrl+;:queue"): True,
    # Foreground tool with a native queue, measured at 112 and 256 columns.
    ("Enter:send now", "Shift+Tab:mode", "Ctrl+c:cancel", "Ctrl+b:send to bg", "Ctrl+;:queue"): True,
    ("Enter:send now", "Shift+Tab:mode", "Ctrl+c:cancel", "Ctrl+b:send to bg", "Ctrl+;:queue",
     "Ctrl+x:shortcuts"): True,
    ("Enter:queue", "Alt+Enter:newline", "Shift+Tab:mode", "Ctrl+c:cancel",
     "Enter Enter:send now"): True,
    ("Enter:queue", "Alt+Enter:newline", "Shift+Tab:mode", "Ctrl+c:cancel", "Ctrl+b:send to bg",
     "Ctrl+x:shortcuts"): True,
}


def paste_lines(text: str) -> int:
    """1.0.77 counts LF lines, excluding exactly one final LF terminator."""
    return text.count("\n") + (not text.endswith("\n")) if text else 0


def queue_preview(text: str) -> str:
    lines = paste_lines(text)
    return text.split("\n", 1)[0] + (f" (+{lines - 1} lines)" if lines > 1 else "")


def lf_layout(text: str) -> bool:
    """LF observations establish no other control or line-separator layout."""
    return not any((ord(char) < 32 and char != "\n") or ord(char) == 127
                   or char in "\x85\u2028\u2029" for char in text)


def training_altered(rows: list[str], top: int) -> bool:
    """The question's title with a bracketed choice on its row, near above the composer, but not as measured.

    A transcript can quote the words; a quotation is not drawn with the choices just above the composer.
    """
    return any(TRAINING_TITLE in row and ("[Opt out]" in row or "[Opt in]" in row)
               for row in rows[max(0, top - TRAINING_NEAR):top])


def training_question(rows: list[str], top: int) -> bool:
    """Exactly the measured block directly above the composer's top border, with its words exact.

    The transcript above can quote the words; only this block, once in the frame, is the question.
    """
    text = "\n".join(rows)
    if any(text.count(word) != 1 for word in (TRAINING_TITLE, "[Opt out]", "[Opt in]")):
        return False
    title = next(i for i, row in enumerate(rows) if TRAINING_TITLE in row)
    body = rows[title + 1:top - 1]
    return (0 < title < top - 2 and not rows[title - 1].strip() and not rows[top - 1].strip()
            and TRAINING_TITLE_ROW.fullmatch(rows[title]) is not None
            and all(row.startswith("  ") and row.strip() for row in body)
            and " ".join(row.strip() for row in body) == TRAINING_BODY)


def hint_working(rows: list[str], *, native_queue: bool = False) -> bool | None:
    """Only measured hint tuples; unknown is never an inferred idle state."""
    shown = [row.strip(" ") for row in rows if row.strip(" ")]
    if len(shown) != 1:
        return None
    if re.fullmatch(r"Grok Build\s+1\.0\.77", shown[0]):
        return False
    hint = tuple(part.strip(" ") for part in shown[0].split("│"))
    if hint == RESPONSE_QUEUE_HINT and native_queue:
        return True
    return HINT_STATES.get(hint)


def queue_entries(text: str, *, complete_previews: bool = False) -> tuple[str, ...] | None:
    """The complete native list immediately above the composer, not transcript echoes.

    1.0.77 at 112/256 columns shows a blank row, `   ▾ Queued N`, N numbered
    previews, and a blank row before the composer's top border. Previews are
    display evidence only; no preview supplies the recorded input body.
    """
    rows = text.splitlines()
    footer = next((i for i in range(len(rows) - 1, -1, -1) if FOOTER.fullmatch(rows[i])), None)
    if footer is None:
        return None
    top = next((i for i in range(footer - 1, -1, -1) if re.fullmatch(r"  ╭─+╮ *", rows[i])), None)
    if top is None or top < 2 or rows[top - 1].strip():
        return None
    index, found = top - 2, []
    while index >= 0:
        match = re.fullmatch(r"     #([1-9][0-9]*) (\S.*)", rows[index].rstrip(" "))
        if match is None:
            break
        if complete_previews:
            width = _cells(match[0])
            if width is None or width >= rows[top].index("╮"):
                return None  # no complete preview proved where the row reaches the frame edge
        found.append((match[1], match[2]))
        index -= 1
    title = re.fullmatch(r"   ▾ Queued ([1-9][0-9]*) *", rows[index]) if index >= 0 else None
    if title is None:
        return None if found or index >= 0 and "Queued" in rows[index] else ()
    found.reverse()
    if (index == 0 or rows[index - 1].strip() or str(len(found)) != title[1]
            or any(number != str(position) for position, (number, _) in enumerate(found, 1))):
        return None
    return tuple(preview for _, preview in found)


def visible_queue(screen: Screen | None) -> tuple[str, ...] | None:
    """Read queue evidence independently of an unread composer hint."""
    if screen is None or screen.interruption or screen.starting:
        return None
    if screen.unknown == UNKNOWN_HINT and screen.working is None and screen.box is None:
        rows = screen.text.splitlines()
        footer = next((i for i in range(len(rows) - 1, -1, -1) if FOOTER.fullmatch(rows[i])), None)
        if footer is None or hint_working(rows[footer + 1:], native_queue=True) is not True:
            return None
    elif screen.unknown or screen.working is not True or screen.box is None or screen.box.strip():
        return None
    return queue_entries(screen.text, complete_previews=True)


def matches_queue(payload: str, screen: Screen | None, before: Screen | None = None) -> bool:
    """One exact preview, or one new exact entry appended since the matched paste."""
    entries = visible_queue(screen)
    preview = queue_preview(payload)
    if entries is None or entries.count(preview) != 1:
        return False
    if before is None:
        return True
    previous = queue_entries(before.text, complete_previews=True)
    return (before.working is True and not before.unknown and not before.interruption
            and previous is not None and entries == (*previous, preview))


@dataclass(frozen=True)
class Style:
    foreground: tuple[int, int, int] | None = None
    background: tuple[int, int, int] | None = None
    faint: bool = False


def indexed(index: int) -> tuple[int, int, int]:
    """An xterm 256-colour index from 16: the 6x6x6 cube, then the 24-step grey ramp."""
    if index >= 232:
        grey = 8 + 10 * (index - 232)
        return grey, grey, grey
    index -= 16
    steps = (0, 95, 135, 175, 215, 255)
    return steps[index // 36], steps[index // 6 % 6], steps[index % 6]


# Measured on 1.0.77 at 256 columns with truecolor and with the xterm palette.
# A whole chip uses one palette: the brackets, the label and the background.
FOLDED_CHIP_STYLES = (
    (Style((65, 65, 65), (17, 17, 17)), Style((200, 200, 200), (17, 17, 17))),
    (Style(indexed(238), indexed(233)), Style(indexed(251), indexed(233))),
)
INDEXED_DRAFT_STYLE = Style(indexed(254), indexed(233))


def characters(text: str) -> tuple[str, list[Style]] | None:
    """Decode the SGR subset tmux emits; other terminal controls stay unknown."""
    plain, styles, position = [], [], 0
    foreground, background, faint = None, None, False
    while position < len(text):
        if text[position] != "\x1b":
            char = text[position]
            if ord(char) < 32 and char != "\n":
                return None
            plain.append(char)
            styles.append(Style(foreground, background, faint))
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
                foreground, background, faint = None, None, False
            elif code == 2:
                faint = True
            elif code == 22:
                faint = False
            elif code == 39:
                foreground = None
            elif code == 49:
                background = None
            elif code in (38, 48):
                if codes[at + 1:at + 2] == [2] and len(codes) >= at + 5:
                    rgb = tuple(codes[at + 2:at + 5])
                    if any(value > 255 for value in rgb):
                        return None
                    if code == 38:
                        foreground = rgb
                    else:
                        background = rgb
                    at += 4
                elif codes[at + 1:at + 2] == [5] and len(codes) >= at + 3 and 16 <= codes[at + 2] <= 255:
                    # Measured on 1.0.77 under a tmux that offers no truecolor (M1 to M3): the 256-colour
                    # palette, read as xterm's cube and grey ramp. The 16 system colours stay unknown:
                    # their values belong to the terminal.
                    rgb = indexed(codes[at + 2])
                    if code == 38:
                        foreground = rgb
                    else:
                        background = rgb
                    at += 2
                else:
                    return None  # system colours and other palettes have not been measured
            elif code not in (1, 3, 4, 7, 9, 23, 24, 27, 29):
                return None
            at += 1
        position = match.end()
    return "".join(plain), styles


SCROLLED = "Grok composer is scrolled; the full draft is not visible"


def preview_line(line: str, columns: int) -> str | None:
    """The panel leaves short lines whole and cuts long lines with an ellipsis."""
    width = _cells(line)
    if width is None:
        return None
    if width <= columns:
        return line.rstrip(" ")
    cells = 0
    for index, char in enumerate(line):
        cells += _cells(char)
        if cells >= columns:
            return line[:index] + "…"
    return None


@dataclass(frozen=True)
class FoldPreview:
    columns: int
    preview_columns: int
    count: int
    rows: tuple[str, ...]

    def matches(self, box: Box, columns: int | None, slack: int, _height: int | None) -> bool:
        if (columns != self.columns or paste_lines(box.text) != self.count
                or not box.matches(f"[Pasted: {self.count} lines]", columns, slack)):
            return False
        lines = box.text.removesuffix("\n").split("\n")
        visible = lines if self.count <= 6 else lines[:3] + lines[-3:]
        return tuple(preview_line(line, self.preview_columns) for line in visible) == self.rows


@dataclass(frozen=True)
class ScrolledDraft:
    columns: int
    tail: tuple[str, ...]

    def matches(self, box: Box, columns: int | None, slack: int, height: int | None) -> bool:
        if (columns != self.columns or height != XAI_KEY_PANE_ROWS
                or not lf_layout(box.text) or paste_lines(box.text) >= PASTE_FOLD_LINES):
            return False
        # The overflow renderer gives two additional cells to the scrollbar and its space.
        payload = box.text.rstrip("\n")
        expected = Box(payload, textwrap_ascii=box.textwrap_ascii).wrapped_rows(self.columns - 2, slack)
        if expected is None:
            return False
        expected.extend([""] * (len(box.text) - len(payload)))
        return len(expected) > len(self.tail) and tuple(expected[-len(self.tail):]) == self.tail


@dataclass(frozen=True, kw_only=True)
class KeyedDraft(Screen):
    draft_evidence: FoldPreview | ScrolledDraft


def scrolled_composer(body: list[tuple[str, list[Style]]], right: int, *, keyed: bool) -> bool:
    """The captured scrollbar occupies its own track two cells inside the right border."""
    if keyed:
        if len(body) != XAI_KEY_BODY_ROWS:
            return False
        thumb = "▁▂▆█"
    else:
        if right not in (109, 253) or len(body) != 15:
            return False
        thumb = "▀▂▃▄█🮂🮃"
    track = []
    for content, attributes in body:
        if len(content) < 2 or not content.endswith(" ") or _cells(content[:-2]) != right - 8:
            return False
        index = len(content) - 2  # two cells inside the right border
        char, style = content[index], attributes[index]
        track.append(char)
        if char == " ":
            if style != Style((225, 225, 225), (20, 20, 20), False):
                return False
        elif char not in thumb or style != Style((60, 60, 65), (20, 20, 20), False):
            return False
        for position, (char, style) in enumerate(zip(content, attributes)):
            if position != index and not char.isspace() and (
                    style.faint or style.foreground != (225, 225, 225)
                    or style.background != (20, 20, 20)):
                return False
    if keyed:
        # In the measured bottom viewport the thumb reaches the last row.
        return re.fullmatch(r" *[▁▂▆]?█+", "".join(track)) is not None
    return any(char != " " for char in track)


COMPOSER_TOP = re.compile(r"  ╭─+╮ *")  # the top of Grok's own composer frame, as read() finds it


def composer_rows(rows: list[str]) -> tuple[int, int] | None:
    """The rows of Grok's own composer frame, its top to its footer border, found as read() finds them: the last
    footer, and the nearest such top above it. None where no footer is drawn, or no such top stands above it."""
    footer_at = next((i for i in range(len(rows) - 1, -1, -1) if FOOTER.fullmatch(rows[i])), None)
    if footer_at is None:
        return None
    top = next((i for i in range(footer_at - 1, -1, -1) if COMPOSER_TOP.fullmatch(rows[i])), None)
    return None if top is None else (top, footer_at)


def xai_key_composer(rows: list[str]) -> tuple[int, str, str] | None:
    """Find the measured footer and the state named by the row below it."""
    last = next((i for i in range(len(rows) - 1, -1, -1) if rows[i].strip(" ")), -1)
    if last < 2 or rows[last - 1].strip(" "):
        return None
    footer_at = last - 2
    footer = XAI_KEY_FOOTER.fullmatch(rows[footer_at])
    if footer is None:
        return None
    if XAI_KEY_DRAFT_HINT.fullmatch(rows[last]):
        return footer_at, footer[1], "draft"
    banner = rows[last].rstrip(" ")
    if banner == XAI_KEY_IDLE_HINT:
        return footer_at, footer[1], "idle"
    if banner == XAI_KEY_WORKING_HINT:
        return footer_at, footer[1], "working"
    edge = len(rows[footer_at].rstrip(" "))
    if len(banner) != edge:
        return None
    label = banner.lstrip(" ")
    if (label == f"{XAI_KEY_BANNER}  │  {XAI_KEY_VERSION}"
            or label == XAI_KEY_BANNER and xai_version_box(rows[:footer_at], edge + 2)):
        return footer_at, footer[1], "idle"
    return None


def xai_version_box(rows: list[str], width: int) -> bool:
    """Wide startup puts the version in a separate centered 120-cell menu."""
    height, menu_width = 16, 120
    if width < menu_width:
        return False
    left = " " * ((width - menu_width + 1) // 2)
    rule = "─" * (menu_width - 2)
    for index in range(len(rows) - height + 1):
        if rows[index].rstrip(" ") != left + "╭" + rule + "╮":
            continue
        body = [row.rstrip(" ") for row in rows[index + 1:index + height - 1]]
        if (rows[index + height - 1].rstrip(" ") == left + "╰" + rule + "╯"
                and all(row.startswith(left + "│") and row.endswith("│")
                        and _cells(row) == len(left) + menu_width for row in body)
                and re.search(r"  " + re.escape(XAI_KEY_VERSION) + r" +│$", body[1])):
            return True
    return False


def xai_key_fold_preview(rows: list[str], style: list[Style], offsets: list[int],
                         top: int, right: int, count_text: str) -> FoldPreview | None:
    """The native panel has all lines up to six, then three on each side of the omission."""
    try:
        count = int(count_text)
    except ValueError:
        return None
    if count < PASTE_FOLD_LINES:
        return None
    start = top - min(count, 7) - 3
    if start < 1 or rows[start - 1].strip(" ") or rows[top - 1].strip(" "):
        return None
    panel = [row.rstrip(" ") for row in rows[start:top - 1]]
    # The native panel sits nine cells inside the composer on the left and ten on the right.
    left, width = 2 + 9, right + 1 - 10
    indent = " " * left
    bottom = indent + "╰─ paste again or double-click to expand "
    if (width <= len(bottom) + 1 or panel[0] != indent + "╭" + "─" * (width - left - 2) + "╮"
            or panel[-1] != bottom + "─" * (width - len(bottom) - 1) + "╯"):
        return None
    border, label = FOLDED_CHIP_STYLES[0]  # the keyed panel was measured in truecolor
    preview = []
    for index, row in enumerate(panel, start):
        body = start < index < top - 2
        if body and (not row.startswith(indent + "│") or not row.endswith("│") or _cells(row) != width):
            return None
        offset = offsets[index]
        if style[offset + left] != border or style[offset + len(row) - 1] != border:
            return None
        if not body:
            continue
        text = row[left + 1:-1].rstrip(" ")
        omitted = count > 6 and index == start + 4
        if omitted and text != f"⋮ ({count - 6} more lines)":
            return None
        expected = border if omitted else label
        if any(attribute != expected for attribute in style[offset + left + 1:offset + left + 1 + len(text)]):
            return None
        if not omitted:
            preview.append(text)
    return FoldPreview(right - 7, width - left - 2, count, tuple(preview))


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
    keyed = None
    if footer_at is None:
        keyed = xai_key_composer(rows)
        if keyed is None:
            return Screen(plain, box=None, unknown="no fullscreen composer drawn as Grok 1.0.77 draws it")
        footer_at, model, keyed_state = keyed
        shows = dict(model=model)
    else:
        footer = FOOTER.fullmatch(rows[footer_at])
        shows = dict(model=footer[1], effort=footer[2])
    if keyed is None and COMMANDS.seen_in(plain) and "Enter select  |  Esc close" in plain:
        return Screen(plain, box=None, interruption=COMMANDS, shows=shows)
    # An unrecognized rectangular modal is never an ordinary composer, even if
    # the program has left its footer visible underneath.
    if any("┌" in row or "└" in row for row in rows[:footer_at]):
        return Screen(plain, box=None, unknown="unmeasured Grok modal", shows=shows)
    top = next((i for i in range(footer_at - 1, -1, -1)
                if COMPOSER_TOP.fullmatch(rows[i])), None)
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
        if _cells(row[:end]) != right:
            return Screen(plain, box=None, unknown="Grok composer row width is unmeasured", shows=shows)
        content = row[len(prefix):end]
        begin = offsets[index] + len(prefix)
        body.append((content, style[begin:begin + len(content)]))
    if scrolled_composer(body, right, keyed=keyed is not None):
        height = len(rows) - plain.endswith("\n")
        if (keyed is not None and keyed_state == "draft"
                and height == XAI_KEY_PANE_ROWS and footer_at == height - 4
                and top > 0 and not rows[top - 1].strip(" ") and not training_altered(rows, top)):
            tail = tuple(content[:-2].rstrip(" ") for content, _ in body)
            box = "\n".join(content for content, _ in body)
            evidence = ScrolledDraft(right - 7, tail)
            return KeyedDraft(plain, box=box, working=False, shows=shows, draft_evidence=evidence)
        return Screen(plain, box=None, unknown=SCROLLED, shows=shows)
    picker = (keyed is None and len(body) == 1 and body[0][0].startswith("/model ")
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
    fold_preview = None
    for content, attributes in body:
        token = content.rstrip(" ")
        folded_label = PASTE_LABEL.fullmatch(token) if len(body) == 1 else None
        if folded_label:
            palettes = FOLDED_CHIP_STYLES if keyed is None else FOLDED_CHIP_STYLES[:1]
            styled_chip = any(attributes[0] == bracket and attributes[len(token) - 1] == bracket
                              and all(attribute == label for attribute in attributes[1:len(token) - 1])
                              for bracket, label in palettes)
            if keyed is not None:
                if keyed_state == "draft" and styled_chip:
                    fold_preview = xai_key_fold_preview(rows, style, offsets, top, right, folded_label[1])
                if fold_preview is None:
                    return Screen(plain, box=None, unknown="unmeasured folded Grok API-key draft", shows=shows)
            if styled_chip:
                # The native chip participates in the payload-derived Box
                # comparison. Delivery still requires the record receipt.
                continue
        for char, attribute in zip(content, attributes):
            if (not char.isspace() and attribute != INDEXED_DRAFT_STYLE
                    and (attribute.faint or attribute.foreground not in (None, (225, 225, 225), (122, 162, 247))
                         or attribute.background not in (None, (20, 20, 20)))):
                return Screen(plain, box=None, unknown="unmeasured Grok composer text styling", shows=shows)
    box = "\n".join(content for content, _ in body)
    if keyed is not None:
        draft = keyed_state == "draft"
        single = len(body) == 1 and bool(box.strip(" ")) == draft
        # A complete wrapped draft keeps its text rows and at most one
        # final blank for the established wrap and LF check.
        text_rows = body if body[-1][0].strip(" ") else body[:-1]
        multiline = (draft and len(body) <= XAI_KEY_BODY_ROWS and len(text_rows) >= 2
                     and all(content.strip(" ") for content, _ in text_rows))
        if ((single or multiline) and top > 0 and not rows[top - 1].strip(" ")
                and not training_altered(rows, top)):
            if keyed_state == "working":
                return Screen(plain, box=None, working=True, shows=shows)
            if fold_preview is not None:
                return KeyedDraft(plain, box=box, working=False, shows=shows, draft_evidence=fold_preview)
            return Screen(plain, box=box, working=False, shows=shows)
        return Screen(plain, box=None, unknown="unmeasured Grok API-key composer", shows=shows)
    if training_question(rows, top):
        # Named, never an unknown dialog. The hint row under the footer is read as on any other screen: M4
        # drew the working hint under the question while the launch's turn ran, then the idle hint. Nothing
        # under the footer at all is the fresh form M1 to M3 measured, with no turn yet. Any other hint row
        # leaves the question named but not pressable: no box is read.
        below = rows[footer_at + 1:]
        working = hint_working(below) if any(row.strip(" ") for row in below) else False
        if working is None:
            return Screen(plain, box=None, interruption=TRAINING_QUESTION, shows=shows,
                          unknown="unmeasured Grok composer hint under the training question")
        return Screen(plain, box=box, working=working, interruption=TRAINING_QUESTION, shows=shows)
    if training_altered(rows, top):
        return Screen(plain, box=box, interruption=TRAINING_ALTERED, shows=shows)
    below = rows[footer_at + 1:]
    if not any(row.strip(" ") for row in below) and len(body) == 1 and not box.strip():
        # The fresh form M1 to M3 measured after the press, before any turn: the empty one-row composer,
        # the footer, and nothing at all under it. Unknown to this reader; the Door reads it idle only in
        # a launch whose record shows the training question answered and cleared.
        return Screen(plain, box=None, unknown=FRESH, shows=shows, fresh=TRAINING_QUESTION.name)
    queued = queue_entries(plain)
    working = hint_working(below, native_queue=bool(queued) and len(body) == 1 and not box.strip(" "))
    if working is None:
        return Screen(plain, box=None, unknown=UNKNOWN_HINT, shows=shows)
    return Screen(plain, box=box, working=working, shows=shows, queued=len(queued or ()))


PROMPT = "  │ ❯ "


def empty_cell(screen: Screen) -> tuple[int, int] | None:
    """The prompt's first cell in an ordinary composer whose one row is only padding.

    Grok 1.0.77 draws typed spaces exactly as its padding, so the screen alone cannot tell them apart. Measured
    on 13 November at 80 x 24 and 112 x 35: the visible terminal cursor sits at the prompt's first cell, column 6
    of the prompt row, in the empty composer, and two typed spaces move it to column 8. The Door reads the box
    empty only where the cursor, read with the frame, sits on this cell; anywhere else, or unread, the padding
    stays text in the box.

    The fresh one-row composer, before any turn (``screen.fresh``), draws the same prompt row, so its first cell
    is named too: in a person's own session the Door reads that form idle only with the cursor there.
    """
    fresh = (screen.fresh is not None and screen.box is None and screen.unknown == FRESH
             and screen.interruption is None)
    if not fresh and (screen.box is None or screen.box == "" or screen.box.strip(" ") or "\n" in screen.box
                      or screen.unknown is not None or screen.interruption is not None or screen.fresh is not None):
        return None
    rows = [index for index, row in enumerate(screen.text.split("\n")) if row.startswith(PROMPT)]
    return (len(PROMPT), rows[0]) if len(rows) == 1 else None


def question_cleared(text: str) -> bool:
    """After the click: none of the question's words remain, and the empty one-row composer is drawn.

    Measured after the press in M2 and M3: the composer and its footer, with no hint row under it yet. This
    confirms only that the question went; it is no reading of readiness, which still needs a measured hint.
    """
    decoded = characters(text)
    if decoded is None:
        return False
    plain = decoded[0]
    if any(word in plain for word in (TRAINING_TITLE, "[Opt out]", "[Opt in]")):
        return False
    rows = plain.split("\n")
    footer_at = next((i for i in range(len(rows) - 1, -1, -1) if FOOTER.fullmatch(rows[i])), None)
    if footer_at is None or footer_at < 2 or not re.fullmatch(r"  ╭─+╮ *", rows[footer_at - 2]):
        return False
    prompt = rows[footer_at - 1].rstrip(" ")
    return (prompt.startswith("  │ ❯ ") and prompt.endswith("│")
            and not prompt[len("  │ ❯ "):-1].strip())
