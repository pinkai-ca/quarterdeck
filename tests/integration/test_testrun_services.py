"""A test run stops every ship service its tests start before it deletes anything, however the test ends.

Synthetic projects only. Their test starts a stand-in service as servicectl starts one: a process of its own session
whose command line runs `--ship <folder> service`, recorded in the run's services folder (TESTRUN_SERVICE_RECORDS)
with the interpreter and a stand-in package whose servicectl.stop is the ship's own stop. The run's cleanup must stop
it, by that stop while the ship exists and then by pid and start, and confirm it gone; one that still ran would hold
the cleanup with nothing removed. Each stand-in writes its pid and start outside the run, to a folder of this test's
own, so the test can prove it has ended.
"""
import json
import os
import signal
import textwrap
from pathlib import Path

import pytest

from tests.integration.testrun_synthetic import make, result, results, scratch, tool, wait_for

BEHAVIOURS = {
    # Ends at SIGTERM, as a service does.
    'calm': 'import signal, sys, time\nsignal.signal(signal.SIGTERM, lambda *a: sys.exit(0))\nwhile True:\n    time.sleep(0.1)\n',
    # Takes two seconds to end after SIGTERM.
    'slow': ('import signal, sys, time\nsignal.signal(signal.SIGTERM, lambda *a: (time.sleep(2), sys.exit(0)))\n'
             'while True:\n    time.sleep(0.1)\n'),
    # Ignores SIGTERM and spins at a full core, as the leftover did.
    'spin': 'import signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nwhile True:\n    pass\n',
}

OWN_STOP = '''
import os, signal, time
from pathlib import Path
def stop(runtime):
    pid = int((Path(runtime) / 'pid').read_text())
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return 'service was not running'
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return 'service stopped by its own stop'
        time.sleep(0.1)
    return 'service still ending'
'''


def synthetic(evidence: Path, behaviour: str, *, record: bool = True, ending: str = 'pass') -> str:
    """A synthetic test that starts one stand-in service and ends as ``ending`` says."""
    return textwrap.dedent(f'''
        import json, os, signal, subprocess, sys, time
        from pathlib import Path

        def start(tmp_path):
            ship = tmp_path / 'ship'
            (ship / 'runtime').mkdir(parents=True)
            package = tmp_path / 'package' / 'quarterdeck'
            package.mkdir(parents=True)
            (package / '__init__.py').write_text('')
            (package / 'servicectl.py').write_text({OWN_STOP!r})
            process = subprocess.Popen([sys.executable, '-c', {BEHAVIOURS[behaviour]!r}, '--ship', str(ship), 'service'],
                                       start_new_session=True)
            for _ in range(100):
                fields = Path(f'/proc/{{process.pid}}/stat').read_text().rsplit(')', 1)[1].split()
                if fields[0] != 'Z':
                    break
            start = fields[19]
            (ship / 'runtime' / 'pid').write_text(str(process.pid))
            Path({str(evidence)!r}).write_text(json.dumps(dict(pid=process.pid, start=start)))
            if {record!r}:
                folder = Path(os.environ['TESTRUN_SERVICE_RECORDS'])
                folder.mkdir(parents=True, exist_ok=True)
                (folder / f'{{process.pid}}-{{start}}.json').write_text(json.dumps(dict(
                    ship=str(ship), runtime=str(ship / 'runtime'), pid=process.pid, start=start, unit=None,
                    python=sys.executable, package=str(tmp_path / 'package'))))
            return process

        def test_starts_a_service(tmp_path):
            process = start(tmp_path)
            ending = {ending!r}
            if ending == 'fail':
                assert False, 'synthetic failure with its service running'
            if ending == 'interrupted':
                time.sleep(600)
            if ending == 'teardown-without-wait':
                os.kill(process.pid, signal.SIGTERM)  # its teardown signals and returns at once
    ''')


def running(evidence: Path) -> bool:
    data = json.loads(evidence.read_text())
    try:
        fields = Path(f"/proc/{data['pid']}/stat").read_text().rsplit(')', 1)[1].split()
    except OSError:
        return False
    return fields[19] == data['start'] and fields[0] not in ('Z', 'X', 'x')


def end_it(evidence: Path) -> None:
    """The test's own safety: never leave its stand-in running, whatever the tool did."""
    if evidence.exists() and running(evidence):
        os.kill(json.loads(evidence.read_text())['pid'], signal.SIGKILL)


@pytest.mark.parametrize(('behaviour', 'ending', 'record'), [
    ('calm', 'pass', True),
    ('calm', 'fail', True),
    ('slow', 'teardown-without-wait', True),
    ('spin', 'pass', False),
])
def test_the_run_stops_a_service_its_test_left_however_the_test_ended(tmp_path, behaviour, ending, record):
    evidence = tmp_path / 'service.json'
    project, worktree = make(tmp_path, {'test_service.py': synthetic(evidence, behaviour, record=record, ending=ending)})
    try:
        done = tool('run', '--target', str(worktree), '-n', '0', cwd=project)
        assert done.returncode == (1 if ending == 'fail' else 0), done.stdout + done.stderr
        assert not running(evidence), 'the service ended before the run did'
        cleanup = result(project)['cleanup']
        assert cleanup['outcome'] == 'complete', cleanup
        [entry] = cleanup['services']['entries']
        data = json.loads(evidence.read_text())
        assert (entry['pid'], entry['start']) == (data['pid'], data['start'])
        if behaviour == 'spin':
            assert entry['outcome'] == 'stopped' and entry['killed'] and entry['found_by'] == 'its command line'
            assert entry['own_stop'] is None, 'no record: no own stop to run'
        elif behaviour == 'slow':
            assert entry['outcome'] in ('stopped', 'ended')
        else:
            assert entry['outcome'] == 'stopped' and entry['own_stop'] == 'service stopped by its own stop'
            assert not entry.get('killed'), 'its own stop was enough'
        assert scratch(project) == []
    finally:
        end_it(evidence)


def test_an_interrupted_run_still_stops_its_service(tmp_path):
    evidence = tmp_path / 'service.json'
    project, worktree = make(tmp_path, {'test_service.py': synthetic(evidence, 'spin', ending='interrupted')})
    try:
        done = tool('run', '--target', str(worktree), '-n', '0', '--timeout', '20', cwd=project)
        assert done.returncode != 0
        assert not running(evidence), 'the interrupted run still ended its service'
        cleanup = result(project)['cleanup']
        assert cleanup['outcome'] == 'complete', cleanup
        [entry] = cleanup['services']['entries']
        assert entry['outcome'] == 'stopped'
    finally:
        end_it(evidence)
