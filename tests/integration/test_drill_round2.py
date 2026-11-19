"""The temporary drill owns its lifecycle while its matrix sends new orders."""
import json
from pathlib import Path
import sys

from tests.integration.test_plain_install import DRILL_TIMEOUT, child_environment, command, scratch

pytest_plugins = ('tests.shared_install',)

ROUND2_RACE = '''
import json
from quarterdeck import drill, input_receipt, post
from quarterdeck.harness.synthetic_control import events
matrix = drill.Drill.matrix
observation = {}
def racing_matrix(self, label):
    if label != 'round 2':
        return matrix(self, label)
    drill.service.stop(self.ship.runtime)
    original_new = self.store.new
    worker = next(seat for seat in self.seats() if seat.name == 'worker-1')
    arrived = []
    started = False
    def new(*args, **kwargs):
        nonlocal started
        message = original_new(*args, **kwargs)
        if message.kind == 'order' and message.to == worker.name and message.subject == 'drill round 2':
            arrived.append(message)
            if len(arrived) == 2:
                self.store.new = original_new
                # Force the legal scheduling window: after the service's no-open-
                # order snapshot, the matrix arrivals precede its automatic fresh.
                automatic = worker.spec.fresh == 'after-order'
                observation['configured_after_order'] = automatic
                if automatic:
                    worker.fresh()
                    observation['launch_receipts'] = [self.attempts.latest(worker.name).get(o.id) for o in arrived]
                drill.service.start(self.ship)
                started = True
        return message
    self.store.new = new
    patience = self.patience
    self.patience = 5
    try:
        matrix(self, label)
        keyboard = events(worker.home)
        observation['worker_orders'] = {}
        for order in arrived:
            forms = post.delivered_forms(order)
            kinds = [event['event'] for event in keyboard
                     if any(input_receipt.same_text(event['text'], form) for form in forms)]
            observation['worker_orders'][order.sender] = {
                'submitted': kinds.count('submitted'),
                'accepted': kinds.count('accepted') + kinds.count('consumed'),
                'closed': self.store.closing_report(order) is not None,
            }
    finally:
        self.patience = patience
        self.store.new = original_new
        if not started:
            drill.service.start(self.ship)
drill.Drill.matrix = racing_matrix
passed, lines = drill.run(None)
print(json.dumps(dict(passed=passed, lines=lines, race=observation)))
'''


def test_installed_matrix_keeps_its_explicit_lifecycle_after_new_round_orders(scratch, private_socket_root, shared_wheel):
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
    result = json.loads(command(python, '-I', '-c', ROUND2_RACE, cwd=outside, env=env, timeout=DRILL_TIMEOUT))
    assert result['passed'], json.dumps(result, indent=2)
    assert result['race'] == dict(configured_after_order=False, worker_orders={
        sender: dict(submitted=1, accepted=1, closed=True) for sender in ('captain', 'desk')})
    assert not list(Path(env['TMPDIR']).glob('qd-drill-*'))
    assert not list(private_socket_root.glob('qd-pt-*'))
