"""The tool owns its descriptor budget; deep walks finish or retain a plain depth refusal."""
import json
import os
from pathlib import Path
import resource
import subprocess
import sys

import pytest

from tests.testrunner import owned, resources
from tests.testrunner.layout import DIRECTORY, identity
from tests.unit.test_testrun_scratch_walk import deep


@pytest.mark.parametrize('previous,expected', [
    ((1024, 524288), (8192, 524288)),
    ((1024, 4096), (4096, 4096)),
    ((16384, 524288), (16384, 524288)),
    ((1024, resource.RLIM_INFINITY), (8192, resource.RLIM_INFINITY)),
    ((resource.RLIM_INFINITY, resource.RLIM_INFINITY), (resource.RLIM_INFINITY, resource.RLIM_INFINITY)),
])
def test_open_file_policy_preserves_hard_and_higher_soft_limits(monkeypatch, previous, expected):
    current, calls = [previous], []
    def get(kind):
        assert kind == resource.RLIMIT_NOFILE
        return current[0]
    def set_limit(kind, pair):
        assert kind == resource.RLIMIT_NOFILE
        calls.append(pair)
        current[0] = pair
    monkeypatch.setattr(resource, 'getrlimit', get)
    monkeypatch.setattr(resource, 'setrlimit', set_limit)
    assert resources.apply_open_files() == dict(resource='RLIMIT_NOFILE', requested_soft=8192,
                                               soft=expected[0], hard=expected[1])
    assert calls == ([] if previous == expected else [expected])


def test_resource_policy_loads_and_applies_without_a_package(tmp_path):
    standalone = tmp_path / 'standalone_resources.py'
    standalone.write_bytes(Path(resources.__file__).read_bytes())
    code = '''
import json, resource, runpy, sys
policy = runpy.run_path(sys.argv[1])
resource.setrlimit(resource.RLIMIT_NOFILE, (1024, resource.getrlimit(resource.RLIMIT_NOFILE)[1]))
memory = policy['apply_from_environment']()
print(json.dumps(dict(memory=memory, open_files=policy['apply_open_files']())))
'''
    done = subprocess.run([sys.executable, '-B', '-I', '-c', code, str(standalone)],
                          capture_output=True, text=True, timeout=10)
    assert done.returncode == 0, done.stdout + done.stderr
    result = json.loads(done.stdout)
    assert 0 < result['memory'] <= resources.DEFAULT_MEMORY_MIB * resources.MIB
    assert result['open_files']['soft'] == 8192
    assert result['open_files']['hard'] == resource.getrlimit(resource.RLIMIT_NOFILE)[1]


def tree(tmp_path, levels):
    root = tmp_path / 'tree'
    root.mkdir()
    fd = os.open(root, DIRECTORY)
    try:
        expected, token = identity(os.fstat(fd)), owned.mark(fd)
    finally:
        os.close(fd)
    deep(root, levels, 'd', b'synthetic bottom')
    return root, expected, token


def test_retirement_removes_a_tree_past_the_interpreters_recursion(tmp_path):
    root, expected, token = tree(tmp_path, 1500)
    parent = os.open(tmp_path, DIRECTORY)
    before = len(os.listdir('/proc/self/fd'))
    try:
        result = owned.retire(parent, root.name, expected, root, token)
        assert result['outcome'] == 'complete', result
        assert result['removed'] == dict(files=2, dirs=1501, links=0,
                                        bytes=len(token) + len(b'synthetic bottom'))
        assert not root.exists()
        assert len(os.listdir('/proc/self/fd')) == before
    finally:
        os.close(parent)


def test_descriptor_exhaustion_names_depth_and_limit_and_closes_both_walks(tmp_path):
    root, expected, token = tree(tmp_path, 200)
    code = '''
import json, os, resource, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from tests.testrunner import owned, scratch
from tests.testrunner.layout import DIRECTORY, Refused
root = Path(sys.argv[2])
resource.setrlimit(resource.RLIMIT_NOFILE, (64, resource.getrlimit(resource.RLIMIT_NOFILE)[1]))
parent = os.open(root.parent, DIRECTORY)
before = len(os.listdir('/proc/self/fd'))
try:
    scratch.usage(root)
except Refused as error:
    measurement = str(error)
else:
    raise AssertionError('a tree deeper than the available descriptors was accepted')
assert len(os.listdir('/proc/self/fd')) == before
result = owned.retire(parent, root.name, json.loads(sys.argv[3]), root, sys.argv[4])
assert len(os.listdir('/proc/self/fd')) == before
os.close(parent)
print(json.dumps(dict(measurement=measurement, retirement=result)))
'''
    done = subprocess.run([sys.executable, '-B', '-I', '-c', code, str(Path(__file__).resolve().parents[2]),
                           str(root), json.dumps(expected), token], capture_output=True, text=True, timeout=20)
    assert done.returncode == 0, done.stdout + done.stderr
    result = json.loads(done.stdout)
    for reason in (result['measurement'], result['retirement']['reason']):
        assert 'tree depth ' in reason and 'RLIMIT_NOFILE soft limit 64' in reason
        assert 'maximum recursion depth' not in reason and 'Too many open files' not in reason
    assert result['retirement']['outcome'] == 'held'
    assert result['retirement']['removed'] == dict(files=0, dirs=0, links=0, bytes=0)
    assert (root / owned.MARKER).read_text() == token
    parent = os.open(tmp_path, DIRECTORY)
    try:
        assert owned.retire(parent, root.name, expected, root, token)['outcome'] == 'complete'
    finally:
        os.close(parent)
