"""What a jailed program's environment may hold, named in one place, and the launcher that starts a jail from nothing.

A jail starts from bwrap's ``--clearenv``: nothing of the environment that launched it passes in, since that is the
operator's desktop shell, which carries secrets. Inside, only the names below are set:

- ``ALLOWED``: the framework's own, set at plan time from what the launch knows (sandbox.command);
- ``FROM_PANE`` and every ``LC_*``: values only the pane holds, copied in when the jail starts by this module's
  launcher, which runs outside the jail with an empty environment of its own but PATH;
- the variables the seat's harness declares for its program (Launch.env), so it reaches its local endpoint.

Nothing that ``looks_secret`` passes, whatever list names it: a declared name that looks secret refuses the launch.

Standard library only: the launcher runs before each jail starts, and imports nothing of the framework.
"""

from __future__ import annotations

import os
import re
import shutil
import sys

CLEAR = '--clearenv'  # bwrap's own: the jail's environment starts empty
ALLOWED = ('PATH', 'HOME', 'USER', 'LOGNAME', 'XDG_RUNTIME_DIR', 'TMPDIR', 'TMP', 'TEMP', 'QD_SHIP', 'QD_SEAT',
           'BASH_FUNC_qd%%',  # this ship's qd for the seat's shells (law: can-post)
           'GIT_AUTHOR_NAME', 'GIT_COMMITTER_NAME', 'GIT_AUTHOR_EMAIL', 'GIT_COMMITTER_EMAIL')
FROM_PANE = ('TERM', 'LANG', 'TZ')
FROM_PANE_PREFIXES = ('LC_',)

# A name looks secret when one of its words (split at anything but a letter or digit, any case) ends with one of
# these, or is one of the whole words below. "TOKEN" followed by a word of counting (a program's cap on its output
# tokens, as ..._OUTPUT_TOKEN_MAX) is a number of tokens, not a token.
SECRET_ENDINGS = ('KEY', 'KEYS', 'TOKEN', 'TOKENS', 'SECRET', 'SECRETS', 'PASS', 'PASSWORD', 'PASSWORDS', 'PASSWD',
                  'PASSPHRASE', 'CREDENTIAL', 'CREDENTIALS', 'CREDS', 'AUTH', 'COOKIE', 'COOKIES', 'JWT')
SECRET_WORDS = ('AUTHORIZATION', 'BEARER', 'PRIVATE', 'SIGNATURE', 'SALT')
COUNTING = ('MAX', 'MIN', 'LIMIT', 'COUNT', 'BUDGET')


def looks_secret(name: str) -> bool:
    """Whether a variable's name looks like it holds a secret; the value is never read."""
    words = [word for word in re.split(r'[^A-Z0-9]+', name.upper()) if word]
    for at, word in enumerate(words):
        following = words[at + 1] if at + 1 < len(words) else ''
        if word in ('TOKEN', 'TOKENS') and following in COUNTING:
            continue
        if word in SECRET_WORDS or word.endswith(SECRET_ENDINGS):
            return True
    return False


def from_pane(name: str) -> bool:
    return (name in FROM_PANE or name.startswith(FROM_PANE_PREFIXES)) and not looks_secret(name)


def pane_settings(environ) -> list[str]:
    """bwrap's ``--setenv`` words for the pane's own values that a jail keeps: TERM, LANG, TZ and LC_*."""
    return [word for name, value in sorted(environ.items()) if from_pane(name) and '\0' not in value
            for word in ('--setenv', name, value)]


def main(argv: list[str]) -> int:
    """``-- <command>``: exec the command (bwrap, or an endpoint seat's relay around it) with nothing of this
    environment but PATH, and the pane's kept values set inside the jail just after bwrap's ``--clearenv``."""
    if len(argv) < 2 or argv[0] != '--' or CLEAR not in argv[1:]:
        print('jail_environment: usage: -- <bwrap command with --clearenv>', file=sys.stderr)
        return 2
    command = argv[1:]
    at = command.index(CLEAR) + 1
    command[at:at] = pane_settings(os.environ)
    path = os.environ.get('PATH', os.defpath)
    program = shutil.which(command[0], path=path) or command[0]
    os.execve(program, command, {'PATH': path})
    return 127  # not reached


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
