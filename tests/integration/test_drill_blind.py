"""The installed drill can carry an open order after its old transport finished blind."""
import json
from pathlib import Path
import sys

from tests.integration.test_plain_install import DRILL_TIMEOUT, child_environment, command, scratch

pytest_plugins = ('tests.shared_install',)


FINISHED_BLIND = '''
import json
from quarterdeck import drill, pending, post
original_all = drill.Drill.fresh_all
original_one = drill.Drill.fresh_after_delivery
observation = {}
def stopped_all(self):
    # The drill owns this temporary service. Its finished transport can leave
    # a blind result with a cancelled journal; no acceptance is claimed.
    drill.service.stop(self.ship.runtime)
    try:
        return original_all(self)
    finally:
        drill.service.start(self.ship)
def after_blind(self, seat, order):
    if seat.name != 'worker-1':
        return original_one(self, seat, order)
    bound = pending.current_identity(seat)
    assert bound is not None
    attempt = self.attempts.open(order, seat.name, bound['pane'], bound['pid'], bound['launch'])
    proof = pending.Proof.begin(seat, attempt, post.typed([order]), before_paste=True)
    proof.save(phase='cancelled', resolution='synthetic finished pre-paste transport')
    attempt = self.attempts.close(attempt, 'blind', 'synthetic finished pre-paste transport')
    with drill.lifecycle.claim(self.ship.runtime, seat.name):
        observation.update(result=attempt.result, phase=proof.data['phase'],
                           lifecycle_free=True, confirmed=proof.confirmed(seat), seeded=True)
    clock = self.settlement_clock
    guard = self.settlement_guard
    self.settlement_guard = .8  # The finished state is already observed; no latency is being tested.
    # Wait for settled evidence; scheduling/IO time cannot consume this test's guard.
    # The parent command timeout still catches a genuinely stuck drill.
    self.settlement_clock = lambda: 0.0
    try:
        return original_one(self, seat, order)
    finally:
        self.settlement_guard = guard
        self.settlement_clock = clock
drill.Drill.fresh_all = stopped_all
drill.Drill.fresh_after_delivery = after_blind
passed, lines = drill.run(None)
print(json.dumps(dict(passed=passed, lines=lines, blind=observation)))
'''


def test_installed_drill_carries_a_finished_blind_order(scratch, private_socket_root, shared_wheel):
    outside = scratch / 'outside'
    outside.mkdir()
    env = child_environment(scratch / 'child', private_socket_root)
    prefix = scratch / 'installed'
    command(sys.executable, '-I', '-m', 'venv', prefix, cwd=outside, env=env)
    python = prefix / 'bin/python'
    command(python, '-I', '-m', 'pip', 'install', '--no-index', '--no-deps', shared_wheel.path,
            cwd=outside, env=env)
    assert not shared_wheel.source.exists()
    assert not shared_wheel.build_environment.exists()
    env['PATH'] = str(prefix / 'bin') + ':/usr/bin:/bin'
    result = json.loads(command(python, '-I', '-c', FINISHED_BLIND, cwd=outside, env=env, timeout=DRILL_TIMEOUT))
    assert result['blind'] == dict(result='blind', phase='cancelled', lifecycle_free=True,
                                   confirmed=None, seeded=True)
    assert result['passed'], '\n'.join(result['lines'])
    assert any(line.startswith('PASS  worker-1 fresh ') and 'carried after finished blind attempt' in line
               for line in result['lines'])
    assert any(line.startswith('PASS  worker-1 briefed with its open order') for line in result['lines'])
    assert not list(Path(env['TMPDIR']).glob('qd-drill-*'))
    assert not list(private_socket_root.glob('qd-pt-*'))
