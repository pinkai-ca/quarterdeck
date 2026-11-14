"""Ordinary lane selection and presentation; no test process or program starts."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.testrunner import cli, report, run
from tests.testrunner.layout import Refused


def chosen(*arguments):
    return run.selection(cli.parser().parse_args(['run', *arguments]))


@pytest.mark.parametrize('arguments', [(), ('--fast',)])
def test_author_default_is_only_unit_and_contract_at_sixteen(arguments):
    assert chosen(*arguments) == {
        'name': 'fast', 'targets': ['tests/unit', 'tests/contract'], 'workers': 16, 'slots': 2,
    }


def test_signing_requires_an_explicit_whole_choice():
    assert chosen('--whole') == {'name': 'whole', 'targets': ['tests'], 'workers': 8, 'slots': 1}
    assert chosen('tests') == {'name': 'targets', 'targets': ['tests'], 'workers': 8, 'slots': 1}
    assert chosen('tests')['name'] != 'whole'


def test_named_targets_keep_order_node_and_their_default_workers():
    paths = ['tests/unit/example.py::test_sample', 'tests/contract/example.py']
    assert chosen(*paths) == {'name': 'targets', 'targets': paths, 'workers': 8, 'slots': 1}


@pytest.mark.parametrize('lane,workers', [('--fast', 0), ('--fast', 2), ('--fast', 16),
                                          ('--whole', 0), ('--whole', 8)])
def test_worker_override_is_recorded_and_preserved_in_pytest_arguments(lane, workers):
    selected = chosen(lane, '-n', str(workers))
    argv = run.pytest_argv(Path('python'), Path('scratch'), workers, selected['targets'])
    assert selected['workers'] == workers
    assert selected['slots'] == (2 if lane == '--fast' else 1)
    assert argv[-len(selected['targets']):] == selected['targets']
    if workers:
        assert argv[argv.index('-n') + 1] == str(workers)
        assert argv[argv.index('--dist') + 1] == 'worksteal'
    else:
        assert '-n' not in argv


@pytest.mark.parametrize('arguments,maximum', [
    (('--fast', '-n', '17'), 16), (('--fast', '-n', '-1'), 16),
    (('--whole', '-n', '9'), 8), (('tests/unit/example.py', '-n', '9'), 8),
])
def test_lane_limits_name_their_own_worker_range(arguments, maximum):
    with pytest.raises(Refused, match=f'0 to {maximum} workers'):
        chosen(*arguments)


@pytest.mark.parametrize('lane', ['--fast', '--whole'])
def test_complete_lane_cannot_be_narrowed_by_positional_targets(lane):
    with pytest.raises(Refused, match='cannot be combined with named targets'):
        chosen(lane, 'tests/unit/example.py')


def test_lane_flags_are_mutually_exclusive():
    with pytest.raises(SystemExit) as error:
        chosen('--fast', '--whole')
    assert error.value.code == 2


def test_selection_is_pure_and_direct_runner_callers_can_omit_lane():
    args = SimpleNamespace(targets=['tests/unit/example.py'], workers=2)
    before = deepcopy(args)
    assert run.selection(args) == {
        'name': 'targets', 'targets': ['tests/unit/example.py'], 'workers': 2, 'slots': 1,
    }
    assert vars(args) == vars(before)


def test_runner_records_the_resolved_default_before_starting_any_process():
    args = cli.parser().parse_args(['run'])
    runner = run.Runner(args, [])
    assert runner.selection == {'name': 'fast', 'targets': ['tests/unit', 'tests/contract'], 'workers': 16, 'slots': 2}
    assert runner.args.workers == 16


@pytest.mark.parametrize('arguments,label', [
    ((), 'fast: unit, contract'), (('--whole',), 'whole'),
    (('tests/unit/example.py',), 'targets: tests/unit/example.py'),
])
def test_selection_is_the_first_line_before_setup(arguments, label, monkeypatch, capsys):
    def before_setup(path):
        raise RuntimeError('synthetic stop before setup')

    monkeypatch.setattr(run.gitops, 'resolve', before_setup)
    runner = run.Runner(cli.parser().parse_args(['run', *arguments]), [])
    with pytest.raises(RuntimeError, match='synthetic stop before setup'):
        runner.main()
    assert capsys.readouterr().out.splitlines() == ['testrun: ' + label]


@pytest.mark.parametrize('arguments,label', [((), 'fast: unit, contract'), (('--whole',), 'whole')])
def test_report_names_the_lane_and_keeps_the_selection_data(arguments, label):
    selected = chosen(*arguments)
    result = {
        'run': 'example-run', 'selection': selected,
        'target': {'top': '/example/source', 'head': 'example-commit', 'tree': 'example-tree'},
        'runner': {}, 'outcome': {'status': 'passed', 'wrapper_exit': 0},
    }
    before = deepcopy(result)
    rendered = report.render(result, '', [])
    assert f'- selection: **{label}**' in rendered
    assert result == before
    assert result['selection']['targets'] == selected['targets']


def test_old_report_without_a_selection_never_claims_a_whole_signature():
    assert report.selection_label(None) == 'unrecorded selection'


def test_help_names_both_lanes_and_the_unchanged_four_slots(capsys):
    with pytest.raises(SystemExit) as error:
        cli.parser().parse_args(['run', '--help'])
    text = ' '.join(capsys.readouterr().out.split())
    assert error.value.code == 0
    assert '--whole' in text and '--fast' in text
    assert 'Signing runs explicitly choose --whole' in text
    assert '16 workers' in text and '8 workers' in text
    assert 'Four eight-worker slots: fast takes two together' in text
