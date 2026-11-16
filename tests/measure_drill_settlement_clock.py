"""Explicit-only main/candidate installed-drill trials; only their child has a 25% CPU quota.

The external test-tool timeout, scope stop and cleanup are never throttled. No load loops.
Select this file with -n 0 only after the engineer grants the machine's load turn.
"""

import ast
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

import pytest

from tests import shared_install
from tests.integration import test_drill_blind as blind
from tests.integration import test_upgrade_resources as resources
from tests.integration.test_plain_install import scratch  # noqa: F401

pytest_plugins = ('tests.shared_install',)
BASE = '410df31bd8632828ad60f8a32ab99e1ec3ed67bf'

OBSERVE = r'''
import json, pathlib, sys, time
from quarterdeck import drill
group = pathlib.Path('/proc/self/cgroup').read_text().strip().split('::', 1)[1]
quota = (pathlib.Path('/sys/fs/cgroup') / group.lstrip('/') / 'cpu.max').read_text().split()
assert int(quota[0]) / int(quota[1]) == .25, quota
print('DRILL_CLOCK_SCOPE ' + json.dumps(dict(group=group, quota=quota)), file=sys.stderr, flush=True)
original = drill.Drill.fresh_after_delivery
def observed(self, seat, order):
    began, cpu = time.monotonic(), time.process_time()
    clock = getattr(self, 'settlement_clock', None)
    before = clock() if clock else None
    result = original(self, seat, order)
    print('DRILL_CLOCK_CALL ' + json.dumps(dict(seat=seat.name,
        guard_seconds=self.patience * drill.SETTLEMENT_WINDOWS,
        wall_seconds=time.monotonic()-began, cpu_seconds=time.process_time()-cpu,
        injected=clock is not None, clock_before=before, clock_after=clock() if clock else None,
        success=result[0] is not None, reason=result[1])), file=sys.stderr, flush=True)
    return result
drill.Drill.fresh_after_delivery = observed
'''


@pytest.fixture(scope='session')
def main_wheel(tmp_path_factory):
    # Reuse the exact wheel fixture, pinning only its source ref while it builds.
    original = resources.git
    def pinned(repo, *args):
        return BASE if args == ('rev-parse', 'HEAD') else original(repo, *args)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(resources, 'git', pinned)
        generator = shared_install.shared_wheel.__wrapped__(tmp_path_factory)
        wheel = next(generator)
    assert wheel.ref == BASE
    try:
        yield wheel
    finally:
        with pytest.raises(StopIteration):
            next(generator)


def baseline_script():
    source = resources.git(resources.ROOT, 'show', BASE + ':tests/integration/test_drill_blind.py')
    for item in ast.parse(source).body:
        if isinstance(item, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'FINISHED_BLIND'
                                               for t in item.targets):
            return ast.literal_eval(item.value)
    raise AssertionError('baseline installed script missing')


@pytest.mark.parametrize(('variant', 'scratch'), [
    ('main', 'run-temporary'), ('main', 'inside-checkout'), ('main', 'run-temporary'),
    ('candidate', 'run-temporary'), ('candidate', 'inside-checkout'), ('candidate', 'run-temporary'),
], indirect=['scratch'])
def test_installed_blind_under_quota(variant, scratch, private_socket_root, shared_wheel,
                                     main_wheel, monkeypatch, capsys):
    wheel = main_wheel if variant == 'main' else shared_wheel
    script = baseline_script() if variant == 'main' else blind.FINISHED_BLIND
    monkeypatch.setattr(blind, 'FINISHED_BLIND', script)
    original = blind.command
    unit = 'qd-drill-clock-' + uuid.uuid4().hex + '.scope'
    bus = {**os.environ, 'XDG_RUNTIME_DIR': f'/run/user/{os.getuid()}',
           'DBUS_SESSION_BUS_ADDRESS': f'unix:path=/run/user/{os.getuid()}/bus'}
    observations = []
    def control(*args):
        return subprocess.run(['systemctl', '--user', *args], env=bus, capture_output=True,
                              text=True, timeout=30)
    def command(*args, cwd, env, timeout=180):
        if tuple(args[1:3]) != ('-I', '-c') or args[3] != script:
            return original(*args, cwd=cwd, env=env, timeout=timeout)
        began = time.monotonic()
        argv = ['systemd-run', '--user', '--scope', '--quiet', '--unit=' + unit,
                '-p', 'CPUQuota=25%', '-p', 'RuntimeMaxSec=300', '--', '/usr/bin/env', '-i',
                *(f'{key}={value}' for key, value in sorted(env.items())),
                str(args[0]), '-I', '-c', OBSERVE + '\n' + script]
        try:
            done = subprocess.run(argv, cwd=cwd, env=bus, capture_output=True, text=True, timeout=timeout)
            observations.extend(line for line in done.stderr.splitlines() if line.startswith('DRILL_CLOCK_'))
            assert done.returncode == 0, (done.returncode, done.stdout, done.stderr)
            return done.stdout
        finally:
            # A parent watchdog and user manager, both outside the limited scope.
            control('stop', unit)
            control('reset-failed', unit)
            shown = control('show', unit, '--property=ActiveState', '--property=ControlGroup')
            properties = dict(line.split('=', 1) for line in shown.stdout.splitlines() if '=' in line)
            assert properties.get('ActiveState') in (None, '', 'inactive', 'failed'), shown.stdout
            groups = {properties.get('ControlGroup')} - {None, ''}
            groups.update(json.loads(line.split(' ', 1)[1])['group'] for line in observations
                          if line.startswith('DRILL_CLOCK_SCOPE '))
            for group in groups:
                procs = Path('/sys/fs/cgroup') / group.lstrip('/') / 'cgroup.procs'
                assert not procs.exists() or not procs.read_text().strip()
            observations.append('DRILL_CLOCK_PARENT ' + json.dumps(dict(
                variant=variant, ref=wheel.ref, unit=unit, wall_seconds=time.monotonic()-began,
                scope_empty=True, safety_seconds=300, supervisor_outside_scope=True)))
    monkeypatch.setattr(blind, 'command', command)
    outcome, failure = 'passed', None
    try:
        blind.test_installed_drill_carries_a_finished_blind_order(scratch, private_socket_root, wheel)
    except AssertionError as error:
        outcome, failure = 'failed', str(error)
        if variant != 'main' or 'outer settlement guard expired' not in failure:
            raise
    finally:
        with capsys.disabled():
            for line in observations:
                print(line, flush=True)
            print('DRILL_CLOCK_RESULT ' + json.dumps(dict(variant=variant, ref=wheel.ref,
                outcome=outcome, failure=failure)), flush=True)
