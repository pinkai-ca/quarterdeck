"""Ordinary check-mode dispatch and outcome scope; no program or door is started."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import check, cli
from quarterdeck.drill import Check
from quarterdeck.ship import ShipError


@pytest.mark.parametrize('minimal', [False, True])
@pytest.mark.parametrize('passed', [False, True])
def test_cli_forwards_only_the_explicit_mode_and_keeps_failure_exit(tmp_path, monkeypatch, minimal, passed):
    ship, tmux = SimpleNamespace(runtime=tmp_path), object()
    calls = []
    monkeypatch.setattr(cli, 'load_ship', lambda args: ship)
    monkeypatch.setattr(cli.Tmux, 'for_ship', lambda value: tmux)

    def selected(actual_ship, harness, actual_tmux, **options):
        calls.append((actual_ship, harness, actual_tmux, options))
        return [Check('synthetic task', passed)]

    monkeypatch.setattr(cli.check, 'check', selected)
    args = cli.build_parser().parse_args(['check', 'codex', '--model', 'synthetic-model']
                                        + (['--minimal'] if minimal else []))
    result = cli.cmd_check(args)
    assert calls == [(ship, 'codex', tmux, {'model': 'synthetic-model', **({'minimal': True} if minimal else {})})]
    assert args.exit_code == (0 if passed else 1)
    assert 'synthetic task' in result[0]


@pytest.mark.parametrize('minimal', [False, True])
def test_mode_uses_its_own_outcomes_and_preserves_failed_step(tmp_path, monkeypatch, minimal):
    ship, tmux = SimpleNamespace(runtime=tmp_path), object()
    publications, calls = [], []
    failed = check.ProbeCheck('synthetic file mismatch', False, 'invented detail', 'file')
    monkeypatch.setattr(check.check_outcome, 'publish', lambda runtime, name, event:
                        publications.append((runtime, name, event)))

    def selected(*arguments, **options):
        calls.append((arguments, options))
        return [failed], '1.0'

    monkeypatch.setattr(check, '_check', selected)
    result = check.check(ship, 'fake', tmux, model='synthetic-model', minimal=minimal)
    assert calls[0][0][:3] == (ship, 'fake', tmux)
    assert calls[0][0][4] == 'synthetic-model'
    assert calls[0][1] == ({'minimal': True} if minimal else {})
    expected_runtime = tmp_path / 'minimal-checks' if minimal else tmp_path
    assert [runtime for runtime, _, _ in publications] == [expected_runtime, expected_runtime]
    assert publications[-1][2]['outcome'] == 'failed'
    assert publications[-1][2]['reason'] == 'file'
    assert failed in result
    if minimal:
        assert result[0].name == 'minimal check mode'
        assert result[0].detail == 'one short write-line task and report'
    else:
        assert result == [failed]


def test_minimal_keeps_the_existing_empty_model_error(tmp_path):
    with pytest.raises(ShipError, match='nonempty model identifier'):
        check.check(SimpleNamespace(runtime=tmp_path), 'fake', object(), model=' ', minimal=True)


def test_minimal_task_contains_only_the_short_task_and_needs_no_laws_file(tmp_path, monkeypatch):
    monkeypatch.setattr(check.standing, 'FRAMEWORK_DIR', tmp_path / 'absent-standing-orders')
    result = check._task_body(Path('synthetic-result.txt'), 'synthetic-line', minimal=True)
    assert result == ('Write the file synthetic-result.txt containing exactly one line: synthetic-line\n'
                      'Then post your report on this order with the body: written')
    assert not (tmp_path / 'absent-standing-orders').exists()


def test_full_task_keeps_the_existing_folding_text_and_laws(tmp_path, monkeypatch):
    (tmp_path / 'laws.md').write_text('  Synthetic law text.\n')
    monkeypatch.setattr(check.standing, 'FRAMEWORK_DIR', tmp_path)
    result = check._task_body(Path('synthetic-result.txt'), 'synthetic-line')
    assert result == (
        'Write the file synthetic-result.txt containing exactly one line: synthetic-line\n'
        'Then post your report on this order with the body: written\n\n'
        'The rest of this order is the laws you already run under, repeated so that the message is long '
        'enough to fold in your box; nothing in it changes the task.\n\nSynthetic law text.')
