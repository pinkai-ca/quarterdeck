"""The installed drill carries open orders even before the service has delivered them."""
import json
from pathlib import Path
import shutil
import sys

from tests.integration.test_plain_install import DRILL_TIMEOUT, child_environment, command
from tests.integration.test_upgrade_resources import RESOURCE_ROOTS, ROOT, archive, environment, git


PAUSED_SERVICE = '''
import json
from quarterdeck import drill
original = drill.Drill.fresh_all
def fresh_without_old_delivery(self):
    # Finish the current service transaction, then model a delayed next pass.
    # This temporary drill alone owns the service; no delivery is left active.
    drill.service.stop(self.ship.runtime)
    try:
        return original(self)
    finally:
        drill.service.start(self.ship)
drill.Drill.fresh_all = fresh_without_old_delivery
passed, lines = drill.run(None)
print(json.dumps(dict(passed=passed, lines=lines)))
'''


def test_installed_drill_carries_open_orders_before_service_delivery(tmp_path, private_socket_root):
    source = tmp_path / 'source'
    archive(git(ROOT, 'rev-parse', 'HEAD'), source, RESOURCE_ROOTS)
    outside = tmp_path / 'outside'
    outside.mkdir()
    env = child_environment(tmp_path / 'child', private_socket_root)
    builder = environment(tmp_path / 'build-env')
    wheels = tmp_path / 'wheels'
    wheels.mkdir()
    command(builder, '-I', '-c',
            'from setuptools.build_meta import build_wheel; import sys; build_wheel(sys.argv[1])',
            wheels, cwd=source, env=env)
    wheel, = wheels.glob('*.whl')
    prefix = tmp_path / 'installed'
    command(sys.executable, '-I', '-m', 'venv', prefix, cwd=outside, env=env)
    python = prefix / 'bin/python'
    command(python, '-I', '-m', 'pip', 'install', '--no-index', '--no-deps', wheel, cwd=outside, env=env)
    shutil.rmtree(source)
    shutil.rmtree(tmp_path / 'build-env')
    env['PATH'] = str(prefix / 'bin') + ':/usr/bin:/bin'
    result = json.loads(command(python, '-I', '-c', PAUSED_SERVICE, cwd=outside, env=env, timeout=DRILL_TIMEOUT))
    assert result['passed'], '\n'.join(result['lines'])
    for name in ['captain', 'worker-1']:
        line, = (line for line in result['lines'] if line.startswith(f'PASS  {name} fresh '))
        assert 'carried unattempted' in line
        assert any(line.startswith(f'PASS  {name} briefed with its open order') for line in result['lines'])
    assert not list(Path(env['TMPDIR']).glob('qd-drill-*'))
    assert not list(private_socket_root.glob('qd-pt-*'))
