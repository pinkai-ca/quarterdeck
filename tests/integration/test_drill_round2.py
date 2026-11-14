"""The temporary drill owns its lifecycle while its matrix sends new orders."""
import json
import sys

from tests.integration.test_plain_install import child_environment, command, scratch

pytest_plugins = ('tests.shared_install',)

ROUND2_RACE = '''
import json
from quarterdeck import drill
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
        return matrix(self, label)
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
    result = json.loads(command(python, '-I', '-c', ROUND2_RACE, cwd=outside, env=env, timeout=240))
    assert result['passed'], json.dumps(result, indent=2)
    assert result['race'] == dict(configured_after_order=False)
