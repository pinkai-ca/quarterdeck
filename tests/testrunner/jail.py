"""``run --jail``: every step that runs the target's code runs inside bubblewrap, built by listing what goes in.

The jail is built by this module, the copy of the tool that was invoked, never by the target's own copy: a run
whose tool lies inside its target is refused. Nothing of the machine is visible unless named below:

- ``--unshare-all``: no network but a private loopback, a private process space, IPC and host name;
- a cleared environment, refilled from an allowlist of the run's own names and fixed values, so the ssh agent,
  the desktop's bus, tmux, ``QD_*``, ``XDG_RUNTIME_DIR``, the display and every token variable stay out;
- a read-only ``/usr`` with the root's links into it, and the listed files of ``/etc``;
- a private ``/tmp``, a minimal ``/dev`` (bubblewrap's: null, zero, full, random, urandom, tty, pts, shm; no GPU),
  ``/proc`` of the jail's own processes;
- the run's own scratch, writable: its checkout of the target, its venv and an empty HOME;
- read-only: the user site that supplies the test tools, and the project's Git folder, with the run checkout's
  own admin entry writable, so Git works in the checkout;
- nothing else of /home or /run/user.

The time limit and the per-process memory ceiling are the run's own (``--timeout``, ``--memory-mib``); the jail
states both. Bubblewrap shares the machine's kernel: a strong fence for this purpose, not a separate machine.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess

PATH = '/usr/local/bin:/usr/bin:/bin'  # the jail's own PATH: only what /usr holds
SOCKET_DIR = '/tmp/qd-s'  # the suite's tmux sockets, in the jail's private /tmp
ROOT_LINKS = ('bin', 'sbin', 'lib', 'lib32', 'lib64', 'libx32')
ETC = ('alternatives', 'ld.so.cache', 'ld.so.conf', 'ld.so.conf.d', 'localtime', 'passwd', 'group',
       'nsswitch.conf', 'hosts', 'host.conf', 'gai.conf', 'os-release', 'mime.types',
       'ssl/certs/ca-certificates.crt', 'ssl/openssl.cnf')  # public; Debian's pip loads the bundle to start
ALLOWED = ('HOME', 'TMPDIR', 'XDG_CACHE_HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'PIP_CACHE_DIR',
           'PIP_NO_INDEX', 'PIP_NO_INPUT', 'PIP_DISABLE_PIP_VERSION_CHECK', 'PYTHONNOUSERSITE', 'TESTRUNS_RUN',
           'TESTRUNS_USERBASE', 'TESTRUN_EVIDENCE', 'TESTRUNS_MEMORY_MIB', 'OPENBLAS_NUM_THREADS', 'GIT_CONFIG_NOSYSTEM')
# SHELL is fixed, not passed through: tools such as script(1) run their command with it, and only a shell
# under /usr exists inside.
FIXED = {'PATH': PATH, 'LANG': 'C.UTF-8', 'SHELL': '/bin/bash' if Path('/usr/bin/bash').exists() else '/bin/sh',
         'TMUX_TMPDIR': SOCKET_DIR, 'TESTRUNS_SOCKETS': SOCKET_DIR, 'TESTRUNS_JAILED': '1'}
SET_BY_BUBBLEWRAP = ('PWD',)  # bubblewrap's --chdir sets it, to the jail's working folder
# Named so a reader can check them; the allowlist alone already keeps them, and every other name, out.
FORBIDDEN = ('SSH_AUTH_SOCK', 'DBUS_SESSION_BUS_ADDRESS', 'TMUX', 'TMUX_PANE', 'XDG_RUNTIME_DIR', 'DISPLAY',
             'WAYLAND_DISPLAY')


class JailRefused(Exception):
    """The jail cannot be built as listed."""


def program() -> str:
    found = shutil.which('bwrap', path=PATH)
    if found is None:
        raise JailRefused('--jail needs bubblewrap (bwrap) under /usr; install it')
    return found


def python() -> str:
    """The interpreter the jail can see: python3 on the jail's own PATH."""
    found = shutil.which('python3', path=PATH)
    if found is None or not Path(found).resolve().is_relative_to('/usr'):
        raise JailRefused('--jail needs python3 under /usr')
    return found


def tool_outside(tool_files: list[Path], target_top: Path) -> None:
    """The jail comes from the invoked tool; a tool inside its own target would build the target's jail."""
    top = target_top.resolve()
    inside = [str(path) for path in tool_files if path.resolve().is_relative_to(top)]
    if inside:
        raise JailRefused(f'--jail is built by the invoked tool, which lies inside the target {top}; run main\'s '
                          f'copy of tests/testrun.py with --target instead')


CREDENTIAL_KEYS = (re.compile(r'credential\.(.+\.)?helper'), re.compile(r'http\.(.+\.)?extraheader'))
URL_USERINFO = re.compile(r'([a-z][a-z0-9+.-]*)://([^/@\s]+)@', re.I)
SECRET_SCHEMES = ('http', 'https', 'ftp', 'ftps')
INCLUDE_DEPTH = 10


def git_credentials(project: Path) -> list[str]:
    """What in the project's Git config could carry a credential, by key name only, never its value.

    The jail binds .git read-only, so a change from outside could read its config. Read: .git/config, its
    worktree configs, and every file they include, conditionally or not, to a fixed depth. Refused: a credential
    helper, an http extraheader, and a URL with user info: any password, and any user on http, https or ftp.
    The scp-like ``user@host:path`` and ``ssh://user@host`` name an ssh login, which is no credential.
    """
    git_dir = project / '.git'
    files = [git_dir / 'config', git_dir / 'config.worktree', *sorted(git_dir.glob('worktrees/*/config.worktree'))]
    found, seen = [], set()

    def scan(path: Path, depth: int) -> None:
        resolved = path.resolve()
        if resolved in seen or not path.is_file():
            return
        seen.add(resolved)
        if depth > INCLUDE_DEPTH:
            found.append(f'includes nested past {INCLUDE_DEPTH} levels at {path}')
            return
        done = subprocess.run(['git', 'config', '--file', str(path), '--null', '--list'], capture_output=True,
                              timeout=30, env={'PATH': PATH, 'GIT_CONFIG_NOSYSTEM': '1', 'HOME': '/nonexistent'})
        if done.returncode:
            found.append(f'an unreadable config at {path}')
            return
        for item in done.stdout.split(b'\0'):
            if not item:
                continue
            key, _, value = item.decode('utf-8', 'replace').partition('\n')
            key = key.lower()
            if any(pattern.fullmatch(key) for pattern in CREDENTIAL_KEYS):
                found.append(f'{key} in {path}')
            for scheme, userinfo in URL_USERINFO.findall(value):
                if ':' in userinfo or scheme.lower() in SECRET_SCHEMES:
                    found.append(f'a URL with user info in {key} in {path}')
            if key == 'include.path' or (key.startswith('includeif.') and key.endswith('.path')):
                target = Path(value).expanduser()
                scan(target if target.is_absolute() else path.parent / target, depth + 1)

    for file in files:
        scan(file, 0)
    return found


def environment(env: dict) -> dict:
    jailed = {name: env[name] for name in ALLOWED if name in env}
    jailed.update(FIXED)
    return jailed


def binds(scratch: Path, *, user_site: str | None, git_dir: Path | None, admin: Path | None) -> list[tuple[str, str]]:
    """What goes in, as (kind, path): ro, rw. Each path is mounted at the same place inside."""
    listed = [('rw', str(scratch))]
    if git_dir is not None:
        listed.append(('ro', str(git_dir)))
    if admin is not None:
        listed.append(('rw', str(admin)))
    if user_site and Path(user_site).is_dir():
        listed.append(('ro', user_site))
    return listed


def argv(command: list, *, env: dict, cwd: Path, listed: list[tuple[str, str]]) -> list[str]:
    jail = [program(), '--unshare-all', '--die-with-parent', '--new-session', '--clearenv']
    for name, value in sorted(environment(env).items()):
        jail += ['--setenv', name, value]
    jail += ['--ro-bind', '/usr', '/usr']
    for name in ROOT_LINKS:
        path = Path('/') / name
        if path.is_symlink():
            jail += ['--symlink', os.readlink(path), str(path)]
        elif path.is_dir():
            jail += ['--ro-bind', str(path), str(path)]
    for name in ETC:
        jail += ['--ro-bind-try', f'/etc/{name}', f'/etc/{name}']
    jail += ['--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp']
    for kind, path in listed:
        jail += ['--bind' if kind == 'rw' else '--ro-bind', path, path]
    jail += ['--chdir', str(cwd), '--']
    return jail + [str(part) for part in command]


def statement(*, listed, env: dict, timeout: float, memory_mib: int, tool: dict) -> dict:
    """What the result and report say of the jail."""
    try:
        version = subprocess.run([program(), '--version'], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired, JailRefused):
        version = 'unreadable'
    return {'jailed': True, 'bubblewrap': version, 'namespaces': 'unshare-all (no network but loopback)',
            'environment': sorted(environment(env)), 'usr': 'read-only', 'etc': list(ETC), 'tmp': 'private',
            'dev': 'minimal, no GPU', 'binds': [{'kind': kind, 'path': path} for kind, path in listed],
            'timeout_seconds': timeout, 'memory_mib': memory_mib, 'memory_resource': 'RLIMIT_AS, per process',
            'tool': {'checkout': tool.get('checkout'), 'head': tool.get('head'),
                     'matches_head': tool.get('matches_head')}}
