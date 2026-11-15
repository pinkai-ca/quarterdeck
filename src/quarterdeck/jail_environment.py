"""What a jailed program's environment may hold, and the service's, named in one place; and the launcher that starts a
jail from nothing.

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
# tokens, as ..._OUTPUT_TOKEN_MAX) is a number of tokens, not a token; "AUTH" followed by SOCK or SOCKET (the SSH
# agent's SSH_AUTH_SOCK) is a socket's path, not a secret.
SECRET_ENDINGS = ('KEY', 'KEYS', 'TOKEN', 'TOKENS', 'SECRET', 'SECRETS', 'PASS', 'PASSWORD', 'PASSWORDS', 'PASSWD',
                  'PASSPHRASE', 'CREDENTIAL', 'CREDENTIALS', 'CREDS', 'AUTH', 'COOKIE', 'COOKIES', 'JWT')
SECRET_WORDS = ('AUTHORIZATION', 'BEARER', 'PRIVATE', 'SIGNATURE', 'SALT')
COUNTING = ('MAX', 'MIN', 'LIMIT', 'COUNT', 'BUDGET')
SOCKETS = ('SOCK', 'SOCKET')


# Unjailed panes: the ship's own tmux server starts from this second list, never the caller's whole environment, and
# every unjailed seat inherits what the server holds. It is the framework's names the jail also has, the pane's own
# TERM, LANG, TZ and LC_*, and the desktop session's (DESKTOP), which a jail never receives. Filtered by
# looks_secret like the jail's: no secret-named variable reaches any seat, jailed or not.
DESKTOP = ('DISPLAY', 'WAYLAND_DISPLAY', 'XDG_CURRENT_DESKTOP', 'DBUS_SESSION_BUS_ADDRESS', 'SSH_AUTH_SOCK')
DESKTOP_PREFIXES = ('XDG_SESSION_',)
UNJAILED = (*ALLOWED, *FROM_PANE, *DESKTOP)
UNJAILED_PREFIXES = (*FROM_PANE_PREFIXES, *DESKTOP_PREFIXES)

# The service starts from a declared list too: the unjailed panes' (it may start the ship's server, which they
# inherit), what it needs to run (the tmux folder of a test's private namespace, certificates and proxies for its
# readers, the framework's QD_*, git's identity and isolation, Python's own), and the pools' keys the ship file names
# (api_key = "env:NAME"). Never QD_SEAT: the pane that ran qd sync is not the service.
SERVICE = (*UNJAILED, 'TMUX_TMPDIR', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY',
           'ALL_PROXY', 'http_proxy', 'https_proxy', 'no_proxy', 'all_proxy')
SERVICE_PREFIXES = (*UNJAILED_PREFIXES, 'QD_', 'GIT_', 'PYTHON')
NEVER = ('QD_SEAT',)


def declared(environ, names, prefixes, keys=()) -> dict[str, str]:
    """The variables of ``environ`` a list names, by name or prefix, but never QD_SEAT nor a secret-looking name; then
    the ``keys`` named, a ship file's declared pool keys. Names are judged, never values."""
    kept = {name: value for name, value in environ.items()
            if name not in NEVER and (name in names or name.startswith(prefixes)) and not looks_secret(name)}
    kept.update({name: environ[name] for name in keys if name in environ and name not in NEVER})
    return kept


def unjailed_environment(environ) -> dict[str, str]:
    """What the ship's own tmux server starts with, and so what an unjailed seat's pane inherits."""
    return declared(environ, UNJAILED, UNJAILED_PREFIXES)


def service_environment(environ, keys=()) -> dict[str, str]:
    """What the service starts with: its own list, and the pools' declared ``keys``."""
    return declared(environ, SERVICE, SERVICE_PREFIXES, keys)


def looks_secret(name: str) -> bool:
    """Whether a variable's name looks like it holds a secret; the value is never read."""
    words = [word for word in re.split(r'[^A-Z0-9]+', name.upper()) if word]
    for at, word in enumerate(words):
        following = words[at + 1] if at + 1 < len(words) else ''
        if word in ('TOKEN', 'TOKENS') and following in COUNTING or word == 'AUTH' and following in SOCKETS:
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
