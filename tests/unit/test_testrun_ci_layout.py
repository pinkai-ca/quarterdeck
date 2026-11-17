"""CI layout selection, independent-repository retirement and retained layout evidence."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from tests.testrunner import clean, cli, evidence, owned, report
from tests.testrunner.layout import RESULTS, RUNS, Root, identity


def test_jail_and_ci_layout_are_refused_before_constructing_a_runner(tmp_path, capsys):
    target = tmp_path / 'missing-target'
    with patch.object(cli.run, 'Runner') as runner:
        assert cli.main(['run', '--jail', '--ci-layout', '--target', str(target)], []) == 2
        runner.assert_not_called()

    output = capsys.readouterr()
    assert output.out == ''
    assert 'choose either --jail or --ci-layout' in output.err
    assert 'markers needed to verify the CI layout' in output.err


@pytest.mark.parametrize('flag, jail, ci_layout', [('--jail', True, False), ('--ci-layout', False, True)])
def test_each_layout_option_is_forwarded_to_the_runner_alone(flag, jail, ci_layout):
    with patch.object(cli.run, 'Runner') as runner:
        runner.return_value.main.return_value = 0
        assert cli.main(['run', flag], []) == 0
        runner.assert_called_once()
        args, _ = runner.call_args.args
        assert (args.jail, args.ci_layout) == (jail, ci_layout)
        runner.return_value.main.assert_called_once_with()


def test_independent_repository_retirement_keeps_result_and_leaves_no_git_admin(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    subprocess.run(['git', '-c', 'init.templateDir=', 'init', '--quiet', str(project)], check=True)
    root = Root(project)
    run = 'synthetic-run'
    runs_fd = root.child(RUNS)
    scratch_fd = root.child(RUNS, run)
    result_fd = root.child(RESULTS, run)
    scratch = root.path / RUNS / run
    checkout = scratch / 'checkout'
    try:
        token = owned.mark(scratch_fd)
        subprocess.run(['git', '-c', 'init.templateDir=', 'init', '--quiet', str(checkout)], check=True)
        (checkout / '.testruns' / 't').mkdir(parents=True)
        (checkout / '.testruns' / 't' / 'ordinary-product').write_text('test scratch\n')
        kept = root.path / RESULTS / run / 'result.json'
        kept.write_text('{"synthetic": true}\n')
        state = {'scratch': identity(os.fstat(scratch_fd)), 'owner': token,
                 'repository_kind': 'independent', 'checkout': str(checkout),
                 'checkout_identity': identity(checkout.lstat()), 'admin': str(checkout / '.git')}
        removed = clean.retire_run(root, runs_fd, run, state)
        assert removed['outcome'] == 'complete'
        assert removed['git_admin']['removed'] is True
        assert removed['git_admin']['external_admin'] is False
        assert not scratch.exists()
        assert kept.read_text() == '{"synthetic": true}\n'
        assert not (project / '.git' / 'worktrees').exists()
        assert (project / '.git').is_dir()
    finally:
        for fd in (result_fd, scratch_fd, runs_fd):
            os.close(fd)
        root.close()


def test_observed_layout_survives_analysis_and_readable_report(tmp_path):
    layout = {'cwd': '/owned/checkout', 'TMPDIR': '/owned/checkout/.testruns/t',
              'tempdir': '/owned/checkout/.testruns/t', 'testruns_run_present': False,
              'disk_only': True, 'registry_present': False, 'basetemp': None}
    events = [{'kind': 'start', 'role': 'main', 'plugin': 2, 'layout': layout},
              {'kind': 'collected', 'role': 'main', 'nodeids': [], 'complete': True},
              {'kind': 'finish', 'role': 'main', 'exitstatus': 5}]
    (tmp_path / 'main.jsonl').write_text(''.join(json.dumps(event) + '\n' for event in events))
    found = evidence.analyse(tmp_path, 5)
    assert found['complete'] is True
    assert found['session_layouts'] == [{'role': 'main', **layout}]
    result = {'run': 'synthetic-run', 'target': {'top': '/source', 'head': 'synthetic-tip', 'tree': 'synthetic-tree'},
              'runner': {}, 'outcome': {'status': 'no-tests', 'wrapper_exit': 5}, 'mode': 'ci-layout',
              'ci_full_invocation': False, 'evidence': found, 'argv': ['python', '-m', 'pytest', 'probe.py']}
    rendered = report.render(result, '', [])
    assert 'mode: `ci-layout`' in rendered
    assert 'full CI-layout invocation: False' in rendered
    assert 'pytest session layouts:' in rendered and '/owned/checkout/.testruns/t' in rendered
    assert 'no hosted-runner or drill proof' in rendered
