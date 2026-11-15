"""A jailed seat's program holds nothing of the environment that launched it, only the jail's allowlist.

The service and the ship's tmux server inherit the operator's desktop shell, which carries secrets; the jail used to
pass that environment through. Here the throwaway newcomer's own private tmux server is seeded with decoy variables,
secret-named and not, as the desktop's would be, and a jailed desk (pi, then OpenCode, stand-in programs) is brought
up. The jailed program's environment, read from /proc outside the jail, holds only the names the launch set after
bwrap's --clearenv and the pane's kept TERM, LANG, TZ and LC_*: no decoy, and nothing that looks secret. Names only:
no value is printed or compared but the decoys' absence.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from quarterdeck import cli

from .test_jailed_cwd_at_launch import desk_of, qd
from .test_sandbox import namespaces_allowed
from .test_setup_jailed_office_desk import answers, jailed, newcomer  # noqa: F401  the newcomer fixture

pytestmark = pytest.mark.skipif(not namespaces_allowed(), reason="bubblewrap is not installed, or cannot make a namespace here (inside a jail)")

DECOYS = ('QD_DECOY_OPENAI_API_KEY', 'QD_DECOY_ELEVENLABS_KEY', 'QD_DECOY_JMAP_TOKEN', 'QD_DECOY_CLIENT_SECRET',
          'QD_DECOY_SESSION_SECRET', 'QD_DECOY_CODEX_PASS', 'LC_QD_DECOY_TOKEN', 'QD_DECOY_HARMLESS')
# What each harness declares for its program (Launch.env); a new name here is a change to read.
HARNESS = {'pi': {'PI_CODING_AGENT_DIR', 'PI_SKIP_VERSION_CHECK'},
           'opencode': {'HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME', 'XDG_STATE_HOME',
                        'OPENCODE_DISABLE_AUTOUPDATE', 'OPENCODE_DISABLE_PROJECT_CONFIG', 'OPENCODE_CONFIG',
                        'OPENCODE_CONFIG_DIR', 'OPENCODE_CONFIG_CONTENT', 'OPENCODE_PERMISSION', 'QD_WORKSPACE',
                        'OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX', 'OPENCODE_EXPERIMENTAL_NATIVE_LLM'}}


def environ_names(pid: int) -> dict[str, str]:
    data = Path(f'/proc/{pid}/environ').read_bytes()
    return dict(item.split('=', 1) for item in data.decode(errors='replace').split('\0') if '=' in item)


def descendants(root: int) -> list[int]:
    children: dict[int, list[int]] = {}
    for entry in Path('/proc').iterdir():
        if entry.name.isdigit():
            try:
                fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            except OSError:
                continue
            children.setdefault(int(fields[1]), []).append(int(entry.name))
    found, queue = [], [root]
    while queue:
        for child in children.get(queue.pop(), []):
            found.append(child)
            queue.append(child)
    return found


def jailed_program(pane_pid: int) -> int:
    """The stand-in program inside the jail: it execs ``sleep 300``."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        for pid in descendants(pane_pid):
            try:
                if Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')[:2] == [b'sleep', b'300']:
                    return pid
            except OSError:
                continue
        time.sleep(0.1)
    raise AssertionError('the jailed program never started')


@pytest.mark.parametrize('program', ['pi', 'opencode'])
def test_a_jailed_program_holds_only_the_allowlist_on_a_host_seeded_with_decoys(newcomer, capsys, program):
    for name in DECOYS:  # the ship's tmux server's own environment, as the desktop's reaches it
        subprocess.run(['tmux', '-S', newcomer.tmux.socket, 'set-environment', '-g', name, 'decoy-value'], check=True)
    code = cli.main(['setup', '--answers', str(answers(newcomer, program))])
    out = capsys.readouterr()
    assert code == 0, out.err[-3000:] + out.out[-3000:]
    office = newcomer.project / 'office'
    launched = desk_of(office, newcomer).launched()
    assert jailed(launched.argv)
    inside = environ_names(jailed_program(launched.pid))
    names = set(inside)
    print('inside:', ' '.join(sorted(names)))  # names only
    assert not names & set(DECOYS), sorted(names & set(DECOYS))
    from quarterdeck import jail_environment  # after the decoys: the jail's own list is checked once they are out
    assert jail_environment.CLEAR in launched.argv
    set_by_launch = {launched.argv[at + 1] for at, word in enumerate(launched.argv) if word == '--setenv'}
    assert not [name for name in names if jail_environment.looks_secret(name)]
    shell = {'PWD'}  # the stand-in is a sh script, and sh sets PWD itself, inside the jail
    kept = {name for name in names if jail_environment.from_pane(name)}
    assert names <= set_by_launch | kept | shell, sorted(names - set_by_launch - kept - shell)
    framework = set(jail_environment.ALLOWED) | {'HOME'}
    assert set_by_launch <= framework | HARNESS[program], sorted(set_by_launch - framework - HARNESS[program])
    assert {'PATH', 'HOME', 'QD_SHIP', 'QD_SEAT', 'TERM'} <= names, 'what a program needs is still there'
    assert inside['QD_SEAT'] == 'desk' and Path(inside['QD_SHIP']) == office
    outside = environ_names(launched.pid)  # the pane's own process, the launcher or the relay it became
    assert set(outside) == {'PATH'}, sorted(outside)


def test_a_declared_name_that_looks_secret_refuses_the_launch(newcomer, capsys, monkeypatch):
    """A keyed endpoint's key would enter the jail as QD_ENDPOINT_KEY: refused, and nothing launched."""
    from quarterdeck import endpoint
    code = cli.main(['setup', '--answers', str(answers(newcomer, 'pi'))])
    capsys.readouterr()
    assert code == 0
    office = newcomer.project / 'office'
    assert qd(capsys, office, 'down', 'desk')[0] == 0
    monkeypatch.setattr(endpoint, 'key', lambda pool: {endpoint.KEY_VARIABLE: 'decoy-key'})
    code, said = qd(capsys, office, 'up', 'desk')
    assert code != 0 and 'QD_ENDPOINT_KEY, which looks secret' in said and 'decoy-key' not in said, said[-2000:]
    assert not (office / 'runtime' / 'seats' / 'desk' / 'launch.json').exists()
