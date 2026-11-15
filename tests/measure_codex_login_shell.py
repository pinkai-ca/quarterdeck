"""Explicit-only live gate: no Codex seat's commands read the login startup files of its HOME. Not suite-discovered.

A throwaway Codex seat is raised through qd's own path: a copy of the ship under /dev/shm (or the given scratch), a
private tmux server of its own, the temporary seat that ``qd check`` uses. The server, and so the seat, gets a decoy
HOME whose .profile exports a secret-looking name and defines a sudo() function. The seat is given one order: run one
command that writes its environment's names, ``type sudo`` and where qd, git and python3 are found to a file. The
gate passes when neither the decoy name nor the sudo helper is in that environment, sudo is the program and not a
function, the tools are found, and no shell snapshot holds the name or the function (or none is written).

It needs Codex signed in on the ship's Codex pool, and spends one short turn. Where no Codex is signed in, the seat
never comes ready and the gate says so and exits 2: not run, never passed. Codex's own updater is off for the run, and
its link and version are read before and after; a change stops the gate with exit 3.

    python3 -I tests/measure_codex_login_shell.py --ship SHIP_FOLDER [--tree FRAMEWORK_TREE] [--scratch FOLDER]

Prints names, counts and verdicts only, never a value.
"""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys

DECOY_NAME = 'QD_DECOY_SECRET_TOKEN'
DECOY_PROFILE = f'''export {DECOY_NAME}=decoy-not-a-secret
export SUDO_ASKPASS=/nonexistent/qd-decoy-askpass
sudo() {{ command sudo -A "$@"; }}
'''
SECRETISH = re.compile('TOKEN|KEY|SECRET|PASS|CREDENTIAL|COOKIE')


def program_identity():
    link = shutil.which('codex')
    version = subprocess.run(['codex', '--version'], capture_output=True, text=True,
                             env={**os.environ, 'DISABLE_AUTOUPDATER': '1'}).stdout.strip() if link else None
    return link, os.path.realpath(link) if link else None, version


def snapshot_names(path: Path):
    text = path.read_text(errors='replace')
    variables = set(re.findall(r'^(?:declare -x |export )([A-Za-z_][A-Za-z0-9_]*)', text, re.M))
    functions = set(re.findall(r'^([A-Za-z_][A-Za-z0-9_:-]*) \(\)', text, re.M))
    return variables, functions


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--ship', required=True, type=Path)
    parser.add_argument('--tree', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--scratch', type=Path, default=Path('/tmp'))
    args = parser.parse_args()
    sys.path.insert(0, str(args.tree / 'src'))
    from quarterdeck import daily_live, durable, harness as harnesses, pending, post, ship as shipmod
    from quarterdeck.check import PATIENCE, ready_for_probe
    from quarterdeck.door import Door
    from quarterdeck.drill import wait_for
    from quarterdeck.harness.base import _version_in_runtime
    from quarterdeck.seat import temporary_seat
    from quarterdeck.ship import SeatSpec

    before = program_identity()
    if before[0] is None:
        print('not run: codex is not on the PATH')
        return 2
    os.environ['DISABLE_AUTOUPDATER'] = '1'
    for key in ('QD_SEAT', 'TMUX', 'TMUX_PANE', 'TMUX_TMPDIR'):
        os.environ.pop(key, None)
    parent = daily_live.ship_root(args.scratch) / f'qd-measure-login-{secrets.token_hex(6)}'
    parent.mkdir(parents=True)
    home = os.environ['HOME']
    tmux, result = None, {}
    try:
        decoy = parent / 'decoy-home'
        decoy.mkdir(mode=0o700)
        (decoy / '.profile').write_text(DECOY_PROFILE)
        folder = parent / args.ship.name
        shutil.copytree(args.ship, folder, symlinks=True, ignore=shutil.ignore_patterns('runtime', '.git', '__pycache__'))
        workspace = parent / 'workspace'
        workspace.mkdir(mode=0o700)
        declaration = shipmod.edit(shipmod.load(folder).file.read_text(), 'ship', 'workspace', str(workspace))
        for name in shipmod.load(folder).seats:
            declaration = shipmod.edit(declaration, f'seats.{name}', 'cwd', str(workspace))
        durable.replace_file(folder / 'ship.toml', declaration)
        os.environ['HOME'] = str(decoy)  # the private server, and every pane on it, starts with the decoy HOME
        try:
            tmux = daily_live.open_private(parent)
            daily_live.start_private(tmux)
        finally:  # the ship and its pool's sign-in are read with the real HOME, as the service reads them
            os.environ['HOME'] = home
        os.environ['QD_SHIP'], os.environ['QD_TMUX_SOCKET'] = str(folder), tmux.socket
        ship = shipmod.load(folder)
        codex = harnesses.get('codex')
        pool = next((p for p in ship.pools.values() if codex.accepts(p) is None), None)
        if pool is None:
            print('not run: the ship declares no Codex pool')
            return 2
        run = secrets.token_hex(2)
        spec = SeatSpec(f'measure-codex-{run}', f'qd-measure-codex-{run}', 'worker', 'codex', (pool.name,), cwd=workspace)
        out = workspace / 'measure.txt'
        command = ("{ env | cut -d= -f1 | sort; echo '--- type'; type sudo 2>&1 | head -1; echo '--- tools'; "
                   f"command -v qd git python3; echo '--- home'; test \"$HOME\" = '{decoy}' && echo decoy; }} > "
                   + str(out))
        orders = []
        with temporary_seat(ship, tmux, spec, orders, 'measure login shell',
                            installed_version=_version_in_runtime(codex, ship.runtime)) as seat:
            seat.up(resume=False)
            door = Door(seat)
            if not wait_for(lambda: ready_for_probe(seat, door), PATIENCE / 3):
                print('not run: the throwaway Codex seat never came ready (is Codex signed in on the pool?);',
                      'the door reads:', door.reading())
                return 2
            store, attempts = post.Store(ship.runtime), post.Attempts(ship.runtime)
            order = store.new('order', 'ship', seat.name, f'Run exactly this one shell command, then stop:\n{command}',
                              subject='measure')
            orders.append(order)
            text = post.typed([order])
            bound = pending.current_identity(seat)
            attempt = attempts.open(order, seat.name, bound['pane'], bound['pid'], bound['launch'])
            proof = pending.Proof.begin(seat, attempt, text)
            delivery = door.deliver(text, proof=proof)
            proof.finish(delivery.result)
            attempts.close(attempt, delivery.result, delivery.detail)
            result['delivery'] = delivery.result
            result['written'] = bool(wait_for(lambda: out.is_file() and '--- home' in out.read_text(), PATIENCE))
            snapshots = sorted((seat.home / 'shell_snapshots').glob('*.sh'))
            result['snapshots'] = len(snapshots)
            for snapshot in snapshots:
                variables, functions = snapshot_names(snapshot)
                result.setdefault('snapshot decoy name', []).append(DECOY_NAME in variables)
                result.setdefault('snapshot sudo helper', []).append('SUDO_ASKPASS' in variables)
                result.setdefault('snapshot sudo()', []).append('sudo' in functions)
        if not result.get('written'):
            print('not run: the command was not run;', json.dumps(result))
            return 2
        names, typed, tools, where = (out.read_text().split('\n--- ', 3) + ['', '', ''])[:4]
        typed, tools, where = (part.split('\n', 1)[-1] for part in (typed, tools, where))  # past each header line
        names = names.split()
        result['the command ran under the decoy HOME'] = where.strip() == 'decoy'
        if not result['the command ran under the decoy HOME']:
            print('not run: the decoy HOME did not reach the command;', json.dumps(result))
            return 2
        result['command decoy name'] = DECOY_NAME in names
        result['command sudo helper'] = 'SUDO_ASKPASS' in names
        result['command secret-looking names'] = [n for n in names if SECRETISH.search(n)]
        result['type sudo is the program'] = typed.startswith('sudo is /')
        result['tools found'] = sorted(Path(line).name for line in tools.split())
    finally:
        os.environ['HOME'] = home
        if tmux is not None and not tmux.close():
            print('private tmux retained:', tmux.held)
        shutil.rmtree(parent, ignore_errors=True)  # unlinks the seat's credential link, never follows it
    after = program_identity()
    print(json.dumps(result, indent=1))
    if after != before:
        print('STOP: codex link or version changed during the run:', before, '->', after)
        return 3
    passed = (not result['command decoy name'] and not result['command sudo helper']
              and result['type sudo is the program'] and {'git', 'python3', 'qd'} <= set(result['tools found'])
              and not any(result.get('snapshot decoy name', []) + result.get('snapshot sudo helper', [])
                          + result.get('snapshot sudo()', [])))
    print('PASSED' if passed else 'FAILED', '· codex', before[2], '· link and version unchanged')
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main())
