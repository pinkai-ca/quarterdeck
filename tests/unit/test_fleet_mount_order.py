"""Ordinary command construction: inspect mount order without running a jail."""

from dataclasses import replace

import pytest

from quarterdeck.sandbox import Jail, command


def position(args, *parts):
    wanted = [str(part) for part in parts]
    return next(i for i in range(len(args)) if args[i:i+len(wanted)] == wanted)


def jail_for(tmp_path, *, office=False):
    project = tmp_path / 'project'
    ship = project / 'office' if office else project / 'ships' / 'alpha'
    runtime = ship / 'runtime'
    seat = runtime / 'seats' / 'worker'
    peer = project / 'ships' / 'beta'
    return Jail(home=tmp_path/'home', seat_home=seat/'home', seat_folder=seat,
                workspace=project, ship_folder=ship, runtime=runtime,
                writable=(runtime/'post',), declarations=(peer/'ship.toml',))


@pytest.mark.parametrize('office', [False, True])
@pytest.mark.parametrize('workspace', ['.', 'work/deep', 'ships/templates/example', 'office/work'])
def test_workspace_precedes_both_fleet_containers_and_their_child_mounts(tmp_path, office, workspace):
    jail = jail_for(tmp_path, office=office)
    project = tmp_path/'project'
    for root in (project/'ships', project/'office'):
        root.mkdir(parents=True)
    jail = replace(jail, workspace=project/workspace)
    args = command(jail, ['synthetic-program'])
    writable = position(args, '--bind', jail.workspace, jail.workspace)
    for root in (project/'ships', project/'office'):
        fence = position(args, '--ro-bind', root, root)
        assert writable < fence
    own = max(i for i in range(len(args)) if args[i:i+3] == ['--ro-bind', str(jail.ship_folder), str(jail.ship_folder)])
    container = project/'office' if office else project/'ships'
    assert position(args, '--ro-bind', container, container) < own
    assert own < position(args, '--bind', jail.seat_folder, jail.seat_folder)
    assert own < position(args, '--bind-try', jail.runtime/'post', jail.runtime/'post')
    peer = project/'ships/beta'
    assert position(args, '--ro-bind', project/'ships', project/'ships') < position(args, '--tmpfs', peer)


@pytest.mark.parametrize('office', [False, True])
def test_missing_fleet_containers_with_writable_parent_have_readonly_placeholders(tmp_path, office):
    jail = jail_for(tmp_path, office=office)
    project = tmp_path/'project'
    args = command(jail, ['synthetic-program'])
    writable = position(args, '--bind', jail.workspace, jail.workspace)
    for root in (project/'ships', project/'office'):
        assert writable < position(args, '--tmpfs', root) < position(args, '--remount-ro', root)
    peer = project/'ships/beta'
    assert position(args, '--remount-ro', project/'ships') < position(args, '--tmpfs', peer)
    assert not project.exists()


def test_missing_office_below_a_readonly_parent_needs_no_mountpoint(tmp_path):
    jail = jail_for(tmp_path)
    jail.ship_folder.mkdir(parents=True)
    workspace = tmp_path/'project/work'
    workspace.mkdir()
    jail = replace(jail, workspace=workspace, declarations=())
    args = command(jail, ['synthetic-program'])
    office = tmp_path/'project/office'
    assert not office.exists()
    assert str(office) not in args
    ships = tmp_path/'project/ships'
    assert position(args, '--bind', workspace, workspace) < position(args, '--ro-bind', ships, ships)


def test_declaration_sorts_at_its_parent_before_child_mounts(tmp_path):
    jail = jail_for(tmp_path)
    peer = jail.declarations[0].parent
    child = peer/'a-tool'
    jail = replace(jail, read_only=(child,))
    args = command(jail, ['synthetic-program'])
    parent = position(args, '--tmpfs', peer)
    declaration = position(args, '--ro-bind', peer/'ship.toml', peer/'ship.toml')
    remount = position(args, '--remount-ro', peer)
    assert parent < declaration < remount < position(args, '--ro-bind-try', child, child)


def test_standalone_mounts_keep_parent_before_child_without_fleet_containers(tmp_path):
    jail = jail_for(tmp_path)
    ship = tmp_path/'standalone'
    runtime = ship/'runtime'
    seat = runtime/'seats/worker'
    jail = replace(jail, ship_folder=ship, runtime=runtime, seat_folder=seat,
                   seat_home=seat/'home', writable=(runtime/'post',), declarations=())
    args = command(jail, ['synthetic-program'])
    assert position(args, '--ro-bind', ship, ship) < position(args, '--bind', seat, seat)
    assert '--ro-bind-try' not in args
