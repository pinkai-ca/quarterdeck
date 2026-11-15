"""A folder bound back into a jail is judged by its real path as well as its own.

A PATH entry, or a folder along a program's link chain, may lead through a symlink into the fleet: a peer's ship folder,
this ship's own folder, or a folder holding them. bwrap follows the link, so a filter that read only the path as written
would bind back what the fence hides: a peer's folder, or this ship's runtime with its seats and tmux folder, where no
fence covers it. Each such folder is dropped. An ordinary folder reached by a link is still bound back.

Real bubblewrap, stand-ins only: a Python probe is the jailed program; the ships are folders in a throwaway project.
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from quarterdeck import sandbox, ship as shipmod

from .test_sandbox import namespaces_allowed

pytestmark = pytest.mark.skipif(not namespaces_allowed(), reason="bubblewrap is not installed, or cannot make a namespace here (inside a jail)")

HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
TEMPLATE = Path(__file__).parents[2] / 'ships' / 'drydock'

PROBE = r"""
import json, os, sys
seen = {}
for place in sys.argv[1:]:
    try:
        seen[place] = sorted(os.listdir(place))
    except OSError as error:
        seen[place] = type(error).__name__
print(json.dumps(seen))
"""


@pytest.fixture
def fleet(tmp_path: Path):
    """A project with this ship (own), a peer (alpha) holding a runtime and its orders, and an office."""
    project = tmp_path / 'project'
    own_folder = project / 'ships' / 'own'
    shutil.copytree(TEMPLATE, own_folder, ignore=shutil.ignore_patterns('runtime'))
    alpha = project / 'ships' / 'alpha'
    (alpha / 'runtime' / 'post' / 'store').mkdir(parents=True)
    (alpha / 'runtime' / 'post' / 'store' / 'stored.json').write_text('{}')
    (alpha / 'ship.toml').write_text('[ship]\nname = "alpha"\n')
    (alpha / 'orders.md').write_text('the peer ship orders')
    (alpha / 'bin').mkdir()
    (project / 'office').mkdir()
    ship = shipmod.load(own_folder)
    shipmod.fence(ship)
    (ship.runtime / 'tmux').mkdir(mode=0o700, exist_ok=True)
    (ship.runtime / 'tmux' / 'stand-in-socket').write_text('a live server listens here')
    own = ship.runtime / 'seats' / 'worker-1'
    (own / 'home').mkdir(parents=True, exist_ok=True)
    (ship.runtime / 'seats' / 'worker-2').mkdir(parents=True, exist_ok=True)
    work = tmp_path / 'work'
    work.mkdir()
    links = tmp_path / 'links'
    links.mkdir()
    return dict(ship=ship, project=project, alpha=alpha, own=own, work=work, links=links)


def planned(fleet, entries: list[Path], program: str = sys.executable) -> tuple[sandbox.Jail, str]:
    path = os.pathsep.join([*map(str, entries), str(Path(sys.executable).parent), '/usr/bin', '/bin'])
    ship, own = fleet['ship'], fleet['own']
    return sandbox.plan(ship, 'worker-1', own, own / 'home', fleet['work'], program, path, home=HOME), path


def probe(jail: sandbox.Jail, path: str, *places: Path) -> dict:
    env = {key: value for key, value in os.environ.items() if not key.startswith(('QD_', 'TMUX'))}
    env['PATH'] = path
    result = subprocess.run(sandbox.command(jail, [sys.executable, '-I', '-c', PROBE, *map(str, places)]), env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def reaches(jail: sandbox.Jail, target: Path) -> list[Path]:
    """The folders the jail binds back whose real path is the target or lies inside it."""
    target = Path(os.path.realpath(target))
    return [folder for folder in jail.read_only if Path(os.path.realpath(folder)).is_relative_to(target)]


def test_a_path_entry_linked_into_a_peers_folder_is_not_bound_back(fleet):
    link = fleet['links'] / 'peer'
    link.symlink_to(fleet['alpha'])
    jail, path = planned(fleet, [link])
    assert link not in jail.read_only and not reaches(jail, fleet['alpha'])
    seen = probe(jail, path, link, link / 'runtime', fleet['alpha'])
    assert seen[str(link)] == 'FileNotFoundError', seen  # nothing of the peer at the link's path
    assert seen[str(link / 'runtime')] == 'FileNotFoundError', seen
    assert seen[str(fleet['alpha'])] == ['ship.toml'], seen  # at its own path the fence shows its ship file alone


def test_a_path_entry_linked_into_this_ships_folder_binds_no_runtime(fleet):
    ship = fleet['ship']
    link = fleet['links'] / 'own'
    link.symlink_to(ship.folder)
    jail, path = planned(fleet, [link])
    assert link not in jail.read_only
    assert not [f for f in jail.read_only
                if Path(os.path.realpath(f)) == ship.folder.resolve() or ship.folder.resolve() in Path(os.path.realpath(f)).parents]
    seen = probe(jail, path, link, link / 'runtime', link / 'runtime' / 'tmux', link / 'runtime' / 'seats',
                 ship.runtime / 'tmux', ship.runtime / 'seats')
    for place in (link, link / 'runtime', link / 'runtime' / 'tmux', link / 'runtime' / 'seats'):
        assert seen[str(place)] == 'FileNotFoundError', (place, seen)
    assert seen[str(ship.runtime / 'tmux')] == [], seen  # at its own path the runtime is fenced as always
    assert 'worker-2' not in seen[str(ship.runtime / 'seats')], seen


@pytest.mark.parametrize('into', ['own-runtime', 'own-tmux', 'peer-runtime', 'ships', 'office'])
def test_a_path_entry_linked_into_any_part_of_the_fleet_is_not_bound_back(fleet, into):
    ship = fleet['ship']
    target = {'own-runtime': ship.runtime, 'own-tmux': ship.runtime / 'tmux',
              'peer-runtime': fleet['alpha'] / 'runtime', 'ships': fleet['project'] / 'ships',
              'office': fleet['project'] / 'office'}[into]
    link = fleet['links'] / into
    link.symlink_to(target)
    jail, path = planned(fleet, [link])
    assert link not in jail.read_only and not reaches(jail, target)
    seen = probe(jail, path, link)
    assert seen[str(link)] == 'FileNotFoundError', seen


def test_a_path_entry_linked_to_a_folder_holding_the_fleet_is_not_bound_back(fleet, tmp_path):
    link = fleet['links'] / 'above'
    link.symlink_to(tmp_path)  # holds the project, and so ships/ and office/, which no fence covers at this path
    jail, path = planned(fleet, [link])
    assert link not in jail.read_only
    places = (link / 'project' / 'ships' / 'alpha', link / 'project' / 'ships' / 'own' / 'runtime')
    seen = probe(jail, path, *places)
    assert seen == {str(place): 'FileNotFoundError' for place in places}, seen


def test_a_program_whose_link_chain_runs_through_a_linked_folder_into_the_fleet_binds_none_of_it(fleet):
    alpha_bin = fleet['alpha'] / 'bin'
    tool = alpha_bin / 'stand-in-tool'
    tool.write_text('#!/bin/sh\necho stand-in\n')
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    (fleet['links'] / 'peer').symlink_to(fleet['alpha'])
    shown = fleet['links'] / 'bin'
    shown.mkdir()
    (shown / 'stand-in-tool').symlink_to(Path('..') / 'peer' / 'bin' / 'stand-in-tool')  # a hop by the linked folder
    jail, path = planned(fleet, [shown], program='stand-in-tool')
    hop = fleet['links'] / 'peer' / 'bin'
    assert hop not in jail.read_only and not reaches(jail, fleet['alpha'])
    seen = probe(jail, path, hop, fleet['links'] / 'peer')
    # links/ is bound back as the prefix above links/bin, so the link inside it resolves, inside, to the peer's own
    # path, where the fence shows its ship file alone: nothing of its orders or runtime.
    assert seen == {str(hop): 'FileNotFoundError', str(fleet['links'] / 'peer'): ['ship.toml']}, seen


def test_an_ordinary_folder_reached_by_a_link_is_still_bound_back(fleet, tmp_path):
    tools = tmp_path / 'tools'
    tools.mkdir()
    (tools / 'stand-in-tool').write_text('#!/bin/sh\necho stand-in\n')
    link = fleet['links'] / 'tools'
    link.symlink_to(tools)
    jail, path = planned(fleet, [link])
    assert link in jail.read_only
    seen = probe(jail, path, link)
    assert seen[str(link)] == ['stand-in-tool'], seen
