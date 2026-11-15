"""The suite's socket budget, shared by admission and the session fixture.

Linux pathname Unix sockets allow 107 bytes plus the terminating zero. Keep the
fixture's existing twelve-byte margin for generated endpoints; named probes must
also fit. Lengths are filesystem bytes, not Unicode character counts.
"""
from __future__ import annotations

import os
from pathlib import Path

from .layout import ROOT_NAME, RUN_SOCKETS, Refused

SOCKET_LIMIT = 107
SOCKET_MARGIN = 12
SESSION_PREFIX = 'u-'


def lengths(anchor: Path, *, uid: int | None = None) -> tuple[int, int]:
    """Longest generated endpoint and legacy named probe under this anchor."""
    uid = os.getuid() if uid is None else uid
    sample = anchor / (SESSION_PREFIX + '12345678')
    generated = sample / 'qs-12345678/qd-pt-12345678/s-replacement'
    named = [sample / f'tmux-{uid}/qd-test-9999999999-abcdef-replacement',
             sample / f'tmux-{uid}/qd-synthetic-attribution-no-server']
    return len(os.fsencode(generated)), max(len(os.fsencode(path)) for path in named)


def project_limit(*, uid: int | None = None) -> int:
    """Maximum absolute project path bytes for its fixed run socket anchor."""
    sample = Path('/p')
    generated, named = lengths(sample / ROOT_NAME / RUN_SOCKETS, uid=uid)
    prefix = len(os.fsencode(sample))
    return min(SOCKET_LIMIT - SOCKET_MARGIN - (generated - prefix),
               SOCKET_LIMIT - (named - prefix))


def check(anchor: Path) -> None:
    generated, named = lengths(anchor)
    if generated + SOCKET_MARGIN > SOCKET_LIMIT or named > SOCKET_LIMIT:
        raise Refused(
            f'test socket anchor {anchor} gives sockets of {generated} bytes (named probes {named}); '
            f'the native Unix socket path limit is {SOCKET_LIMIT} bytes and the suite keeps a margin '
            f'of {SOCKET_MARGIN}; the project path is too long for its .testruns/{anchor.name} anchor; '
            f'the longest checkout/project path is {project_limit()} bytes '
            '(absolute filesystem path, including separators; a linked worktree uses its Git common project)')


def preflight(top: Path, project: Path, *, jailed: bool = False) -> None:
    """This suite declares its socket layout by carrying this shared helper.

    Other projects tested by the tool need not use our fixture or make sockets at
    all. Check paths without importing or executing anything from the target.
    """
    if not (top / 'tests/testrunner/socket_paths.py').is_file():
        return
    if jailed:
        from .jail import SOCKET_DIR
        anchor = Path(SOCKET_DIR)
    else:
        anchor = project / ROOT_NAME / RUN_SOCKETS
    check(anchor)
