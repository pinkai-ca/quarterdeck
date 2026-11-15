"""Explicit-only live proof, using a transient copy of the daily user unit.

Requires DAILY_LIVE_ORIGIN and DAILY_LIVE_QD. Only Antigravity may be checked;
the normal minimal check is its entire native allowance. Not suite-discovered.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import uuid


def unit_worker():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    from types import SimpleNamespace
    from quarterdeck import cli, daily_live
    from quarterdeck.ship import ShipError

    assert 'QD_SEAT' not in os.environ
    assert 'TMUX' not in os.environ and 'TMUX_PANE' not in os.environ
    runs = []
    original_run, original_start = daily_live.run, daily_live.start_private
    def start(server):
        original_start(server)
        assert server.sessions() == []
    def invoke(origin, program, root, qd):
        if program != 'antigravity':
            raise ShipError('live proof permits only Antigravity')
        result = daily_live.invoke(origin, program, root, qd)
        runs.append(dict(program=program, check_exit=result.exit_code,
                         tmux_missing='tmux new-session: error connecting' in result.output,
                         retained=bool(result.retained),
                         copy_removed=not Path(result.place).exists(),
                         namespace_removed=bool(result.socket) and not Path(result.socket).parent.exists()))
        return result
    def run(origin):
        return original_run(origin, runner=invoke, qd=os.environ['DAILY_LIVE_QD'])
    daily_live.start_private, daily_live.run = start, run
    args = SimpleNamespace(ship=os.environ['QD_SHIP'], units=False)
    lines = cli.cmd_daily_live(args)
    result = dict(exit_code=args.exit_code, report_filed=lines[0].endswith('· stored'), checks=runs)
    print('DAILY_LIVE_SYSTEMD ' + json.dumps(result, sort_keys=True), flush=True)
    return args.exit_code


def test_daily_live_through_user_systemd(capsys):
    origin, qd = os.environ['DAILY_LIVE_ORIGIN'], os.environ['DAILY_LIVE_QD']
    unit = 'qd-daily-live-proof-' + uuid.uuid4().hex
    command = ['systemd-run', '--user', '--wait', '--collect', '--pipe', '--quiet',
               '--unit=' + unit, '--property=Type=oneshot', '--property=TimeoutStartSec=infinity',
               '--property=UnsetEnvironment=QD_SEAT TMUX TMUX_PANE',
               '--setenv=QD_SHIP=' + origin, '--setenv=DAILY_LIVE_QD=' + qd,
               sys.executable, '-I', str(Path(__file__).resolve()), '--systemd-worker']
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=900)
    except BaseException:
        subprocess.run(['systemctl', '--user', 'stop', unit], capture_output=True, timeout=30)
        raise
    samples = [json.loads(line.removeprefix('DAILY_LIVE_SYSTEMD ')) for line in done.stdout.splitlines()
               if line.startswith('DAILY_LIVE_SYSTEMD ')]
    with capsys.disabled():
        print('DAILY_LIVE_UNIT_RESULT ' + json.dumps(dict(unit_exit=done.returncode, samples=samples)), flush=True)
    assert done.returncode == 0, done.stderr
    assert len(samples) == 1
    result = samples[0]
    assert result['exit_code'] == 0 and result['report_filed']
    assert len(result['checks']) == 1, 'Antigravity was held or another program needed a check'
    check = result['checks'][0]
    assert check['program'] == 'antigravity'
    assert not check['tmux_missing'] and not check['retained']
    assert check['copy_removed'] and check['namespace_removed']


if __name__ == '__main__':
    assert sys.argv[1:] == ['--systemd-worker']
    sys.exit(unit_worker())
