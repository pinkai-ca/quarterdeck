"""A long checkout runs real fixture sockets through its short private anchor."""
import json
import os
from pathlib import Path
import shutil

from tests.conftest import ROOT
from tests.integration.testrun_synthetic import commit, make, result, tool
from tests.testrunner import socket_paths, socket_roots


def test_long_checkout_runs_its_native_socket_and_cleans_up(tmp_path):
    marker = tmp_path / 'socket-proof.json'
    project, target = make(tmp_path, {'test_socket.py': f'''
        import json, os, socket
        from pathlib import Path
        def test_socket(private_socket_root):
            endpoint = private_socket_root / 'qs-12345678/qd-pt-12345678/s-replacement'
            endpoint.parent.mkdir(parents=True)
            try:
                with socket.socket(socket.AF_UNIX) as native:
                    native.bind(str(endpoint))
                    Path({str(marker)!r}).write_text(json.dumps(
                        {{'socket': str(endpoint), 'bytes': len(os.fsencode(endpoint)),
                         'mode': private_socket_root.stat().st_mode & 0o777}}))
            finally:
                endpoint.unlink(missing_ok=True)
                endpoint.parent.rmdir()
                endpoint.parent.parent.rmdir()
    '''})
    shutil.copytree(ROOT / 'src/quarterdeck', target / 'src/quarterdeck',
                    ignore=shutil.ignore_patterns('__pycache__'))
    helpers = target / 'tests/testrunner'
    shutil.copytree(ROOT / 'tests/testrunner', helpers,
                    ignore=shutil.ignore_patterns('__pycache__'))
    for name in ('__init__.py', 'conftest.py'):
        shutil.copyfile(ROOT / 'tests' / name, target / 'tests' / name)
    commit(target, 'Exercise the short socket fixture in a long checkout')
    short = socket_roots.path(project)
    try:
        assert len(os.fsencode(project)) > 31
        done = tool('run', '--target', str(target), '--fast', '-n', '0', cwd=project)
        failures = '\n'.join(path.read_text() for path in (project / '.testruns/results').glob('*/failures/*.txt'))
        assert done.returncode == 0, done.stdout + done.stderr + failures
        proof = json.loads(marker.read_text())
        assert proof['bytes'] + socket_paths.SOCKET_MARGIN <= socket_paths.SOCKET_LIMIT
        assert proof['mode'] == 0o700
        assert proof['socket'].startswith(str(short) + '/')
        assert str(project) not in proof['socket']
        found = result(project)
        assert found['evidence']['counts']['passed'] == 2
        assert found['cleanup']['outcome'] == 'complete'
        assert not Path(proof['socket']).exists()
        assert not Path(proof['socket']).parents[2].exists()
    finally:
        # Only this freshly made synthetic project's own verified root, after the
        # subprocess ended; a nonempty anchor is retained rather than erased.
        if short.exists():
            with socket_roots.opened(project) as fd:
                for name in ('r', 's'):
                    if (short / name).exists():
                        (short / name).rmdir()
                os.unlink(socket_roots.PROJECT, dir_fd=fd)
                short.rmdir()
