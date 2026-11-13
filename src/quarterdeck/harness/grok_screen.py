"""Grok Build 1.0.77's fullscreen composer, measured with and without color.

SGR belongs to the adapter: callers receive ordinary Screen text. A dim or muted
span alone never makes text a placeholder. The one established argument hint
also requires the model picker, its command prefix and its exact native text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .types import Interruption, Screen, _cells

SGR = re.compile(r"\x1b\[([0-9;]*)m")
FOOTER = re.compile(
    r"^  ╰─+ (Grok [0-9.]+(?: Fast)?) \((low|medium|high|xhigh)\) · (always-approve) ─╯ *$"
)
MODEL_HINT = "<model> [window] [effort]"
COMMANDS = Interruption("commands", r"┌─ Commands ─.*\[✗\].*┐", "Escape")
MODEL_PICKER = Interruption("model picker", r"Grok [0-9.]+ \(current\)", "C-c")
# The first-run question on keeping coding data for training, measured on 1.0.77 (M1 to M3, 12 and 13
# November, 112 x 35): one blank row, the title row with its two choices, the body, one blank row, then the
# composer's top border. Its choices are bracketed buttons with no key; the program asks for SGR mouse
# reports while it shows. The Admiral's answer is no: the framework clicks [Opt out], chosen by its text,
# and never [Opt in] (ord-c304).
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
TRAINING_NEAR = 8  # rows above the composer's top border in which an altered question is looked for
PASTE_FOLD_LINES = 4
HINT_STATES = {
    ("Shift+Tab:mode", "Ctrl+x:shortcuts"): False,
    ("Enter:send", "Alt+Enter:newline", "Shift+Tab:mode", "Ctrl+x:shortcuts"): False,
    ("Shift+Tab:mode", "Ctrl+c:cancel", "Ctrl+x:shortcuts"): True,
    ("Shift+Tab:mode", "Ctrl+c:cancel", "Ctrl+b:send to bg", "Ctrl+x:shortcuts"): True,
    # At 80 columns the working draft drops Ctrl+x, while retaining cancel.
    ("Enter:queue", "Alt+Enter:newline", "Shift+Tab:mode", "Ctrl+c:cancel"): True,
    ("Enter:send now", "Shift+Tab:mode", "Ctrl+c:cancel", "Ctrl+b:send to bg"): True,
    ("Enter:send now", "Shift+Tab:mode", "Ctrl+c:cancel", "Ctrl+;:queue"): True,
}


def paste_lines(text: str) -> int:
    """1.0.77 counts LF lines, excluding exactly one final LF terminator."""
    return text.count("\n") + (not text.endswith("\n")) if text else 0


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


def hint_working(rows: list[str]) -> bool | None:
    """Only measured hint tuples; unknown is never an inferred idle state."""
    shown = [row.strip(" ") for row in rows if row.strip(" ")]
    if len(shown) != 1:
        return None
    if re.fullmatch(r"Grok Build\s+1\.0\.77", shown[0]):
        return False
    return HINT_STATES.get(tuple(part.strip(" ") for part in shown[0].split("│")))


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
        if _cells(row[:end]) != right:
            return Screen(plain, box=None, unknown="Grok composer row width is unmeasured", shows=shows)
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
        token = content.rstrip(" ")
        folded = (len(body) == 1 and re.fullmatch(r"\[Pasted: [1-9][0-9]* lines\]", token)
                  and len(attributes) >= len(token)
                  and all(not attribute.faint and attribute.background == (17, 17, 17)
                          and attribute.foreground == ((65, 65, 65) if i in (0, len(token) - 1)
                                                       else (200, 200, 200))
                          for i, attribute in enumerate(attributes[:len(token)])))
        if folded:
            # Keep the native chip for the payload-derived Box comparison.
            # It proves only the visual submission contract, never delivery.
            continue
        for char, attribute in zip(content, attributes):
            if not char.isspace() and (attribute.faint or attribute.foreground not in
                                      (None, (225, 225, 225), (122, 162, 247))
                                      or attribute.background not in (None, (20, 20, 20))):
                return Screen(plain, box=None, unknown="unmeasured Grok composer text styling", shows=shows)
    box = "\n".join(content for content, _ in body)
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
    working = hint_working(rows[footer_at + 1:])
    if working is None:
        return Screen(plain, box=None, unknown="unmeasured Grok composer hint", shows=shows)
    return Screen(plain, box=box, working=working, shows=shows)


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
