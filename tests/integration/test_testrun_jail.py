"""``tests/testrun.py run --jail``: from inside the jail, the ssh agent, the desktop's bus, the live tmux folder,
the operator's home and the network are out of reach; and a target's own copy of the tool never builds the jail.

Canaries are made beside the jail's listed paths, never inside the operator's home, and every forbidden variable
is set outside with a synthetic value so its absence inside proves the allowlist, not luck."""
from __future__ import annotations

import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import subprocess
import tempfile
import textwrap

import pytest

from tests.conftest import ROOT, git
from tests.integration.testrun_synthetic import TOOL, commit, make, result, tool
from tests.testrunner import jail

pytestmark = pytest.mark.skipif(shutil.which('bwrap', path=jail.PATH) is None, reason='bubblewrap is not installed')


def namespaces_allowed() -> bool:
    """A jail can be made here: not when this suite itself runs inside one, where AppArmor's unpriv_bwrap
    profile denies the capabilities a second bubblewrap needs."""
    try:
        return subprocess.run(['bwrap', '--ro-bind', '/', '/', 'true'], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


needs_a_jail = pytest.mark.skipif(not namespaces_allowed(), reason='cannot make a namespace here (inside a jail)')

UID = os.getuid()
SECRETS = {name: f'synthetic-{name.lower()}' for name in jail.FORBIDDEN}
SECRETS.update(SYNTH_API_TOKEN='synthetic-token-value', GH_TOKEN='synthetic-gh-token', QD_SHIP='/synthetic/ship',
               QD_SEAT='synthetic-seat', QD_TMUX_SOCKET='/synthetic/socket')

PROBE = r'''
import json, os, socket, subprocess, sys
canary, home, outer_pid, uid = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
seen = {}
def reach(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False
seen["ssh_agent"] = reach(f"/run/user/{uid}/gcr/ssh")
seen["desktop_bus"] = reach(f"/run/user/{uid}/bus")
seen["run_user"] = reach("/run/user")
seen["live_tmux"] = reach(f"/tmp/tmux-{uid}")
seen["canary_file"] = reach(os.path.join(canary, "secret.txt"))
seen["canary_socket"] = reach(os.path.join(canary, "agent.sock"))
try:
    probe = socket.socket(socket.AF_UNIX)
    probe.connect(os.path.join(canary, "agent.sock"))
    seen["canary_connect"] = True
except OSError:
    seen["canary_connect"] = False
try:
    seen["home_entries"] = sorted(os.listdir(home))
except OSError:
    seen["home_entries"] = None
for name in (".ssh", ".config", ".gnupg", ".local", ".claude", ".codex"):
    seen["home/" + name] = reach(os.path.join(home, name))
seen["interfaces"] = sorted(name for _, name in socket.if_nameindex())
try:
    socket.create_connection(("192.0.2.1", 9), timeout=3).close()
    seen["network"] = True
except OSError:
    seen["network"] = False
try:
    socket.getaddrinfo("example.com", 443)
    seen["dns"] = True
except OSError:
    seen["dns"] = False
seen["outer_process"] = reach(f"/proc/{outer_pid}")
seen["gpu"] = reach("/dev/dri")
seen["environment"] = sorted(os.environ)
print(json.dumps(seen))
'''


@pytest.fixture
def canary():
    """A secret file and a listening socket outside every path the jail lists: in the suite's own short socket
    folder (TMUX_TMPDIR, which conftest makes private), so a socket path stays within its native limit."""
    folder = Path(tempfile.mkdtemp(prefix='c', dir=os.environ['TMUX_TMPDIR']))
    (folder / 'secret.txt').write_text('synthetic secret\n')
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(folder / 'agent.sock'))
    listener.listen(1)
    try:
        yield folder
    finally:
        listener.close()
        shutil.rmtree(folder)


def test_the_allowlist_never_names_a_forbidden_variable():
    assert not set(jail.ALLOWED) & set(jail.FORBIDDEN)
    assert not set(jail.FIXED) & set(jail.FORBIDDEN)
    assert not any(name.startswith('QD_') for name in [*jail.ALLOWED, *jail.FIXED])
    env = dict(SECRETS, HOME='/synthetic/home', PATH='/synthetic/bin')
    inside = jail.environment(env)
    assert set(inside) <= set(jail.ALLOWED) | set(jail.FIXED)
    assert inside['PATH'] == jail.PATH and inside['HOME'] == '/synthetic/home'


@needs_a_jail
def test_from_inside_the_jail_the_agent_the_bus_tmux_the_home_and_the_network_are_out_of_reach(tmp_path, canary):
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    probe = scratch / 'probe.py'
    probe.write_text(PROBE)
    home = pwd.getpwuid(UID).pw_dir
    env = dict(os.environ, **SECRETS, HOME=str(scratch / 'home'), TMPDIR=str(scratch / 'tmp'))
    (scratch / 'home').mkdir()
    (scratch / 'tmp').mkdir()
    listed = jail.binds(scratch, user_site=None, git_dir=None, admin=None)
    argv = jail.argv([jail.python(), '-I', probe, canary, home, str(os.getpid()), str(UID)],
                     env=env, cwd=scratch, listed=listed)
    done = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout)
    for name in ('ssh_agent', 'desktop_bus', 'run_user', 'live_tmux', 'canary_file', 'canary_socket',
                 'canary_connect', 'network', 'dns', 'outer_process', 'gpu'):
        assert seen[name] is False, (name, seen)
    for name in ('.ssh', '.config', '.gnupg', '.local', '.claude', '.codex'):
        assert seen['home/' + name] is False, (name, seen)
    leading = {Path(path).relative_to(home).parts[0] for _, path in listed if Path(path).is_relative_to(home)}
    assert seen['home_entries'] is None or set(seen['home_entries']) <= leading, seen['home_entries']
    assert seen['interfaces'] == ['lo']
    assert set(seen['environment']) <= set(jail.ALLOWED) | set(jail.FIXED) | set(jail.SET_BY_BUBBLEWRAP)
    assert not set(seen['environment']) & set(SECRETS)


TARGET_TEST = '''
import os
from pathlib import Path

CANARY = Path({canary!r})

def test_the_jail_holds_around_the_targets_own_tests():
    assert os.environ.get("TESTRUNS_JAILED") == "1"
    assert not (CANARY / "secret.txt").exists()
    for name in {forbidden!r}:
        assert name not in os.environ
'''

EVIL_JAIL = '''
from pathlib import Path
Path({marker!r}).write_text("the target's own jail module was imported")
PATH = "/usr/local/bin:/usr/bin:/bin"
def argv(command, **_):
    return [str(part) for part in command]  # no jail at all
'''


def test_the_tool_refuses_to_jail_the_target_it_lies_in(tmp_path):
    project, worktree = make(tmp_path, {'test_ok.py': 'def test_ok():\n    assert True\n'})
    shutil.copy(TOOL, worktree / 'tests' / 'testrun.py')
    shutil.copytree(TOOL.parent / 'testrunner', worktree / 'tests' / 'testrunner',
                    ignore=shutil.ignore_patterns('__pycache__'))
    commit(worktree, 'a target that carries an honest copy of the tool')
    own = subprocess.run(['/usr/bin/python3', '-I', str(worktree / 'tests' / 'testrun.py'), 'run', '--jail',
                          '-n', '0'], cwd=worktree, capture_output=True, text=True, timeout=300)
    assert own.returncode == 2 and 'lies inside the target' in own.stderr, own.stdout + own.stderr
    assert not (project / '.testruns' / 'results').exists() or not os.listdir(project / '.testruns' / 'results')


@needs_a_jail
def test_a_targets_own_copy_of_the_tool_never_builds_the_jail(tmp_path, canary):
    """A target whose own copy of the tool opens the jail: main's tool, the one invoked, builds the jail, and
    the target's module is never even imported."""
    marker = tmp_path / 'marker'
    project, worktree = make(tmp_path, {'test_jailed.py': TARGET_TEST.format(
        canary=str(canary), forbidden=list(jail.FORBIDDEN))})
    shutil.copy(TOOL, worktree / 'tests' / 'testrun.py')
    shutil.copytree(TOOL.parent / 'testrunner', worktree / 'tests' / 'testrunner',
                    ignore=shutil.ignore_patterns('__pycache__'))
    (worktree / 'tests' / 'testrunner' / 'jail.py').write_text(textwrap.dedent(EVIL_JAIL.format(marker=str(marker))))
    commit(worktree, 'a target that brings its own test tool and an open jail')
    env = dict(os.environ, **SECRETS)
    done = tool('run', '--jail', '--target', str(worktree), '-n', '0', cwd=project, env=env)
    assert done.returncode == 0, done.stdout + done.stderr
    found = result(project)
    assert found['outcome']['status'] == 'passed' and found['evidence']['counts']['passed'] == 2
    assert found['jail']['jailed'] is True
    assert Path(found['jail']['tool']['checkout']) == ROOT == Path(found['runner']['checkout'])
    assert found['jail']['tool']['head'] == git(ROOT, 'rev-parse', 'HEAD')
    assert str(TOOL) in found['runner']['files']
    assert not any(str(path).startswith(str(worktree)) for path in found['runner']['files'])
    assert not marker.exists(), "the target's own jail module must never be imported"
    runs = project / '.testruns' / 'results'
    text = next(runs.iterdir()).joinpath('report.md').read_text()
    assert '- jailed: yes, by the tool at' in text
    for value in SECRETS.values():
        assert value not in text and value not in json.dumps(found)


def test_an_unjailed_run_says_so(tmp_path):
    project, worktree = make(tmp_path, {'test_ok.py': 'def test_ok():\n    assert True\n'})
    done = tool('run', '--target', str(worktree), '-n', '0', cwd=project)
    assert done.returncode == 0, done.stdout + done.stderr
    found = result(project)
    assert found['jail'] == {'jailed': False}
    text = next((project / '.testruns' / 'results').iterdir()).joinpath('report.md').read_text()
    assert '- jailed: no' in text


@pytest.mark.parametrize('lines, named', [
    (['[credential]', '\thelper = store'], 'credential.helper'),
    (['[credential "https://example.invalid"]', '\thelper = cache'], 'credential.https://example.invalid.helper'),
    (['[http]', '\textraheader = AUTHORIZATION: synthetic-header-value'], 'http.extraheader'),
    (['[http "https://example.invalid/"]', '\textraheader = AUTHORIZATION: synthetic-header-value'],
     'http.https://example.invalid/.extraheader'),
    (['[remote "mirror"]', '\turl = https://synthetic-user@example.invalid/r.git'], 'a URL with user info'),
    (['[remote "mirror"]', '\turl = ssh://synthetic-user:synthetic-password@example.invalid/r.git'],
     'a URL with user info'),
    (['[include]', '\tpath = extra.config'], 'credential.helper'),
    (['[includeIf "gitdir:/nowhere/"]', '\tpath = extra.config'], 'credential.helper'),
])
def test_a_credential_in_the_projects_git_config_refuses_the_jail(tmp_path, lines, named):
    """The jail binds .git read-only: a credential in its config, or in a file it includes, refuses the run."""
    project, worktree = make(tmp_path, {'test_ok.py': 'def test_ok():\n    assert True\n'})
    clean = jail.git_credentials(project)
    assert clean == [], clean
    (project / '.git' / 'extra.config').write_text('[credential]\n\thelper = store\n')
    with (project / '.git' / 'config').open('a') as config:
        config.write('\n'.join(lines) + '\n')
    found = jail.git_credentials(project)
    assert any(named in item for item in found), found
    assert not any('synthetic-header-value' in item or 'synthetic-password' in item for item in found)
    done = tool('run', '--jail', '--target', str(worktree), '-n', '0', cwd=project)
    assert done.returncode == 2 and 'could be a credential' in done.stderr and named in done.stderr, done.stderr
    assert 'synthetic-header-value' not in done.stderr and 'synthetic-password' not in done.stderr
    assert not (project / '.testruns' / 'results').exists() or not os.listdir(project / '.testruns' / 'results')


@pytest.mark.parametrize('url', ['git@example.invalid:owner/r.git', 'ssh://git@example.invalid/owner/r.git',
                                 'https://example.invalid/owner/r.git'])
def test_an_ssh_login_or_a_plain_url_is_no_credential(tmp_path, url):
    project, _ = make(tmp_path, {})
    git(project, 'remote', 'add', 'origin', url)
    assert jail.git_credentials(project) == []
