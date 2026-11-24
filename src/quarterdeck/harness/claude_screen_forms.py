"""Claude Code screen forms observed on the pinned release."""

import re

from .types import Interruption, Screen, _cells


VERSION = '2.1.331'
SIGNED_OUT_PANE = (120, 40)
SIGNED_IN_PANE = (120, 50)
RULE = '─' * 120
TRUST = Interruption('trust dialog', r'(?m)^ Quick safety check:', key=None)
LOGIN = Interruption('login', r'(?m)Not logged in · run /login$', key=None)
TRANSCRIPT = Interruption('detailed transcript is open', r'(?m)^  Showing detailed transcript · ')
ACCOUNT_QUESTIONS = ((TRUST, 'account-trust'), (LOGIN, 'account-login'))
TRUST_ROWS = (
    "Quick safety check: Is this a project you created or one you trust? (Like your own code, a well-known open source",
    "project, or work from your team). If not, take a moment to review what's in this folder first.",
    "Claude Code'll be able to read, edit, and execute files here.",
    'Security guide',
    '❯ No, exit',
    'Yes, I trust this folder',
    'Enter to confirm · Esc to cancel',
)
LOGIN_NOTICE = 'Not logged in · run /login'
LOGIN_ROW = ('  ⏵⏵ auto mode on (shift+tab to cycle) · install gh for PR status · ← for agents'
             '            ' + LOGIN_NOTICE)
TRANSCRIPT_ROW = ('  Showing detailed transcript · ctrl+o to toggle · ↑/↓ to scroll · v to open in vi · ? for shortcuts'
                  '            verbose')
RETRY_HINT = 'Resend your message to try again'
RETRY_HINT_ROW = f'❯\u00a0{RETRY_HINT}'
STYLED_RETRY_HINT = f'\x1b[39m❯\u00a0\x1b[7mR\x1b[0;2m{RETRY_HINT[1:]}\x1b[0m'
# The composer rule starts with at least ten rule cells. A named session ends the
# top rule with a space-delimited label and one final rule cell.
RETRY_TOP_RULE = re.compile(r'─{10,}(?: [^\s─](?:[^─]*[^\s─])? ─)?')
DISPLAY_MARKUP = re.compile(r'\x1b\[[0-9;]*m|\x1b\]8;[^\x1b\r\n]*\x1b\\')


def _same_launch(launch, pane) -> bool:
    return launch is not None and (launch.version, launch.pane, launch.pid) == (VERSION, pane.id, pane.pid)


def interruption(screen: Screen, pane, read_launch) -> Interruption | None:
    """Recognize a stop in the measured pane, bound to the current launch."""
    if screen.interruption is not None:
        return None
    size = getattr(pane, 'width', None), getattr(pane, 'height', None)
    if size == SIGNED_OUT_PANE:
        question = signed_out_stop(screen)
    elif size == SIGNED_IN_PANE and screen.box is None and 'Showing detailed transcript' in screen.text:
        rows = screen.text.rstrip().splitlines()
        question = TRANSCRIPT if rows[-2:] == [RULE, TRANSCRIPT_ROW] else None
    else:
        return None
    if question is None:
        return None
    return question if _same_launch(read_launch(), pane) else None


def empty_box(screen: Screen, pane, read_launch, read_control) -> bool:
    """The idle hint has a reverse first letter, dim remainder, and hidden cursor at the first cell."""
    width, height = getattr(pane, 'width', None), getattr(pane, 'height', None)
    if (screen.box != RETRY_HINT or screen.working is not False or screen.interruption is not None or screen.queued
            or type(width) is not int or width < len(RETRY_HINT_ROW)):
        return False
    launch = read_launch()
    if not _same_launch(launch, pane):
        return False
    frame = read_control()
    if (frame is None or frame.cursor is None or frame.cursor_visible is not False
            or frame.pane != pane or read_launch() != launch):
        return False
    column, row = frame.cursor
    styled = frame.text.splitlines()
    if column != 2 or len(styled) != height or not 0 < row < len(styled) - 1 or styled[row] != STYLED_RETRY_HINT:
        return False
    plain = [DISPLAY_MARKUP.sub('', line).rstrip(' ') for line in styled]
    top = plain[row - 1]
    return (_cells(top) == width and RETRY_TOP_RULE.fullmatch(top) is not None and plain[row + 1] == '─' * width
            and plain == [line.rstrip(' ') for line in screen.text.splitlines()])


def signed_out_stop(screen: Screen) -> Interruption | None:
    if screen.box is None and 'Quick safety check:' in screen.text:
        rows = [row.strip() for row in screen.text.splitlines() if row.strip()]
        if (len(rows) < 3 or rows[:2] != [RULE, 'Accessing workspace:']
                or not rows[2].startswith('/') or tuple(rows[3:]) != TRUST_ROWS):
            return None
        return TRUST
    if screen.box == '' and LOGIN_NOTICE in screen.text:
        rows = screen.text.rstrip().splitlines()
        if len(rows) < 5:
            return None
        top, prompt, bottom, footer, effort = rows[-5:]
        if (not top.startswith('─' * 10) or not top.endswith('─') or prompt.rstrip() != '❯' or bottom != RULE
                or footer.rstrip() != LOGIN_ROW
                or effort.strip() != '◐ medium · /effort'):
            return None
        return LOGIN
    return None
