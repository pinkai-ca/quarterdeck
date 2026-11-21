"""What a jailed program's environment may hold, and the service's, named in one place; and the launcher that starts a
jail from nothing.

A jail starts from bwrap's ``--clearenv``: nothing of the environment that launched it passes in, since that is the
operator's desktop shell, which carries secrets. Inside, only the names below are set:

- ``ALLOWED``: the framework's own, set at plan time from what the launch knows (sandbox.command);
- ``FROM_PANE`` and every ``LC_*``: values only the pane holds, copied in when the jail starts by this module's
  launcher, which runs outside the jail with an empty environment of its own but PATH;
- the variables the seat's harness declares for its program (Launch.env), so it reaches its local endpoint.

Nothing that ``looks_secret`` passes, whatever list names it: a declared name that looks secret refuses the launch.
Nothing that ``refused`` names reaches any seat, jailed or not, nor the ship's server nor the service, whatever list
names it: SUDO_ASKPASS, dropped.

The launcher also caps the size of any one file the jail writes (``FILE_BYTES``, as ``RLIMIT_FSIZE``), beside the
service's write-bound alarm (write_bound), and gives ``SIGXFSZ`` its default action, which this interpreter ignores.

Standard library only: the launcher runs before each jail starts, and imports nothing of the framework.
"""

from __future__ import annotations

import os
import re
import resource
import shutil
import signal
import sys

CLEAR = '--clearenv'  # bwrap's own: the jail's environment starts empty
# One file a jail writes, at most. Below every folder's bound but the small folders' (write_bound.BOUNDS): no file of a
# seat's comes near it (a hand's largest transcript was well under 100 MiB on 15 Nov). A write past it fails with
# EFBIG, or the writer ends at SIGXFSZ, as measured for Python, Node, Bun and dd.
FILE_BYTES = 1024 ** 3
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


# Names no seat, server or service ever holds, whatever list names them: dropped, not refused, so a list that names
# one still launches. SUDO_ASKPASS would let a seat's sudo raise a password prompt on the operator's screen; root beyond
# the security door goes only through pkexec, after the desk has told the operator what the prompt is for.
REFUSED = ('SUDO_ASKPASS',)


# Unjailed panes: the ship's own tmux server starts from this second list, never the caller's whole environment, and
# every unjailed seat inherits what the server holds. It is the framework's names the jail also has, the pane's own
# TERM, LANG, TZ and LC_*, and the desktop session's (DESKTOP), which a jail never receives. XAUTHORITY is the X
# cookie's path: with DISPLAY it lets an X program reach the desktop through XWayland, and an unjailed seat can read
# the file anyway. Filtered by looks_secret and refused like the jail's: no secret-named variable, and no refused one,
# reaches any seat, jailed or not.
DESKTOP = ('DISPLAY', 'WAYLAND_DISPLAY', 'XDG_CURRENT_DESKTOP', 'DBUS_SESSION_BUS_ADDRESS', 'SSH_AUTH_SOCK',
           'XAUTHORITY')
DESKTOP_PREFIXES = ('XDG_SESSION_',)
UNJAILED = (*ALLOWED, *FROM_PANE, *DESKTOP)
UNJAILED_PREFIXES = (*FROM_PANE_PREFIXES, *DESKTOP_PREFIXES)

# The service starts from a declared list too, named exactly, with no prefix but the unjailed list's own: the
# unjailed panes' list (it may start the ship's server, which they inherit), what it needs to run (a test namespace's
# tmux folder, certificates and proxies for its readers), its own ship (QD_SHIP, QD_DEFAULT_SHIP) and git's identity.
# No pool's key: the launcher reads each from the key file (key_launch). Never QD_SEAT; no other QD_ or GIT_ name, so neither
# GIT_DIR, GIT_WORK_TREE, GIT_INDEX_FILE, GIT_OBJECT_DIRECTORY nor GIT_SSH*. QD_TMUX_SOCKET passes only where it is
# declared (servicectl.start): a test's or the drill's private namespace.
GIT_IDENTITY = ('GIT_AUTHOR_NAME', 'GIT_AUTHOR_EMAIL', 'GIT_COMMITTER_NAME', 'GIT_COMMITTER_EMAIL')
SERVICE = (*UNJAILED, 'TMUX_TMPDIR', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY',
           'ALL_PROXY', 'http_proxy', 'https_proxy', 'no_proxy', 'all_proxy', 'QD_SHIP', 'QD_DEFAULT_SHIP',
           'QD_TEST_CONFIG_HOME',  # a test's scratch for the key file (keyfile.TEST_CONFIG); no real ship sets it
           *GIT_IDENTITY)
SERVICE_PREFIXES = UNJAILED_PREFIXES
NEVER = ('QD_SEAT',)


def declared(environ, names, prefixes, keys=()) -> dict[str, str]:
    """The variables of ``environ`` a list names, by name or prefix, but never QD_SEAT nor a secret-looking name; then
    the ``keys`` named exactly. No caller of the framework passes ``keys``, since no pool's key is carried here; the
    tests pass it to prove that a refused name is dropped from both, whatever names it. Names are judged, never
    values."""
    kept = {name: value for name, value in environ.items()
            if name not in NEVER and (name in names or name.startswith(prefixes)) and not looks_secret(name)
            and not refused(name)}
    kept.update({name: environ[name] for name in keys if name in environ and name not in NEVER and not refused(name)})
    return kept


def unjailed_environment(environ) -> dict[str, str]:
    """What the ship's own tmux server starts with, and so what an unjailed seat's pane inherits."""
    return declared(environ, UNJAILED, UNJAILED_PREFIXES)


def service_environment(environ) -> dict[str, str]:
    """What the service starts with: its own list, and no pool's key."""
    return declared(environ, SERVICE, SERVICE_PREFIXES)


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


def refused(name: str) -> bool:
    """Whether a variable never reaches a seat, the ship's server or the service, whatever list names it (REFUSED).
    Every list passes through here: the jail's, the unjailed panes', the service's and a seat's declared ones."""
    return name in REFUSED


def without_refused(environment) -> dict[str, str]:
    """``environment`` with every refused name dropped."""
    return {name: value for name, value in environment.items() if not refused(name)}


def from_pane(name: str) -> bool:
    return (name in FROM_PANE or name.startswith(FROM_PANE_PREFIXES)) and not looks_secret(name) and not refused(name)


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
    hard = resource.getrlimit(resource.RLIMIT_FSIZE)[1]
    cap = FILE_BYTES if hard == resource.RLIM_INFINITY else min(FILE_BYTES, hard)
    resource.setrlimit(resource.RLIMIT_FSIZE, (cap, cap))
    signal.signal(signal.SIGXFSZ, signal.SIG_DFL)  # a program run from a shell has it so; Python ignores it
    os.execve(program, command, {'PATH': path})
    return 127  # not reached


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
