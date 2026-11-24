"""Claude Code screen stops observed on the pinned release."""

from .types import Interruption, Screen


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
    launch = read_launch()
    if (launch is not None and launch.version == VERSION and launch.pane == pane.id and launch.pid == pane.pid):
        return question
    return None


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
