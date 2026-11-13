"""Ordinary user-facing warnings and copy facts, without native readiness checks."""

from dataclasses import dataclass, replace
import json
from types import SimpleNamespace

from quarterdeck import cli, copy_state, doctor, install, output, presentation
from tests.unit.own_copy_synthetic import (
    OLD, OLDER, STAMP, fact, inert_copy, snapshot, state, unexpected, vessel,
)


def test_handoff_warning_keeps_caller_available_and_changes_nothing(vessel, monkeypatch, capsys):
    before = snapshot(vessel.folder)
    monkeypatch.setattr(copy_state, 'select', lambda folder: copy_state.Selection(
        warning='synthetic copy unavailable; use qd up, qd sync or qd doctor'))
    monkeypatch.setattr(install, 'recover', unexpected)
    monkeypatch.setattr(install, '_proof', unexpected)
    install.handoff(vessel.folder, ['doctor'])
    error = capsys.readouterr().err
    assert 'continuing with this qd' in error and 'qd sync or qd doctor' in error
    assert snapshot(vessel.folder) == before


def test_handoff_exec_failure_warns_and_returns_to_caller(vessel, monkeypatch, capsys):
    inert_copy(vessel.runtime / 'venv', 'installed')
    state(vessel, fact())
    before = snapshot(vessel.folder)
    monkeypatch.setattr(install, 'running_from', lambda path: False)
    calls = []

    def fail(path, argv):
        calls.append((path, argv))
        raise OSError('synthetic exec failure')

    monkeypatch.setattr(install.os, 'execv', fail)
    install.handoff(vessel.folder, ['doctor'])
    qd = str(vessel.runtime / 'venv/bin/qd')
    assert calls == [(qd, [qd, 'doctor'])]
    error = capsys.readouterr().err
    assert 'installed copy could not start; continuing with this qd' in error
    assert 'qd sync from the checkout or qd doctor' in error
    assert snapshot(vessel.folder) == before


def test_checkout_handoff_remains_on_caller_without_warning(vessel, capsys):
    vessel.file.write_text('[ship]\ncode = "checkout"\n')
    before = snapshot(vessel.folder)
    install.handoff(vessel.folder, ['doctor'])
    assert capsys.readouterr().err == ''
    assert snapshot(vessel.folder) == before


def test_doctor_names_selected_since_previous_and_observed_service(vessel, monkeypatch):
    inert_copy(vessel.runtime / 'venv', 'installed')
    inert_copy(vessel.runtime / 'venv.prev', 'previous')
    state(vessel, fact(OLD), fact(OLDER))
    monkeypatch.setattr(install.servicectl, 'service_identity', lambda runtime: dict(copy=dict(commit=OLD)))
    monkeypatch.setattr(install.servicectl, 'alive', lambda runtime: True)
    monkeypatch.setattr(install, 'recover', unexpected)
    monkeypatch.setattr(install, '_proof', unexpected)
    before = snapshot(vessel.folder)
    result = doctor.framework_check(vessel)
    assert result.passed
    assert f'selected copy {OLD[:7]} since {STAMP[:16]}' in result.detail
    assert f'previous {OLDER[:7]} · qd sync --back returns to it' in result.detail
    assert f'observed service commit {OLD[:7]}' in result.detail
    assert snapshot(vessel.folder) == before


def test_doctor_checkout_reports_checkout_commit(vessel, monkeypatch):
    monkeypatch.setattr(install, 'source_repo', lambda ship: vessel.folder)

    def git(repo, *args):
        assert args == ('rev-parse', '--short', 'HEAD')
        return OLD[:7].encode()

    monkeypatch.setattr(install, 'git', git)
    result = doctor.framework_check(replace(vessel, code='checkout'))
    assert result.passed and result.detail == f'follows the checkout at {OLD[:7]}'


def test_historical_current_reports_limits_without_reproof(vessel, monkeypatch):
    inert_copy(vessel.runtime / 'venv', 'historical')
    state(vessel, fact(), legacy=True)
    monkeypatch.setattr(install.servicectl, 'service_identity', lambda runtime: None)
    monkeypatch.setattr(install.servicectl, 'alive', lambda runtime: False)
    monkeypatch.setattr(install, '_proof', unexpected)
    monkeypatch.setattr(install, 'recover', unexpected)
    before = snapshot(vessel.folder)
    result = doctor.framework_check(vessel)
    assert result.passed
    assert 'historical copy evidence; its own handler has old in-place management' in result.detail
    assert 'service down; no running commit observed' in result.detail
    assert 'no proven previous copy' in result.detail
    assert 'transactional recovery and service identity confirmation are not established' in install.historical_notice(fact())[0]
    assert snapshot(vessel.folder) == before


def test_fast_presentation_yields_when_caller_must_show_copy_warning(vessel, monkeypatch):
    monkeypatch.setattr(copy_state, 'select', lambda *args, **kwargs: copy_state.Selection(
        warning='synthetic copy operation pending'))
    monkeypatch.setattr(install, 'recover', unexpected)
    monkeypatch.setattr(install, '_proof', unexpected)
    before = snapshot(vessel.folder)
    assert presentation.cached_line(vessel.folder) is None
    assert snapshot(vessel.folder) == before


def test_line_and_json_status_include_same_copy_warning(vessel, monkeypatch, capsys):
    monkeypatch.setattr(cli, 'load_ship', lambda args: vessel)
    warning = 'synthetic copy unavailable'
    monkeypatch.setattr(copy_state, 'select', lambda *args, **kwargs: copy_state.Selection(warning=warning))
    monkeypatch.setattr(cli.Tmux, 'default', lambda: object())
    # Supply only the ordinary Facts.line interface, without gathering live evidence.
    @dataclass
    class Facts:
        service: str = 'down'

        def line(self):
            return 'SERVICE DOWN'

    facts = Facts()
    monkeypatch.setattr(cli.watch, 'facts', lambda *args: facts)
    before = snapshot(vessel.folder)
    result = cli.cmd_status(SimpleNamespace(line=True))
    assert result.data['copy_warning'] == warning
    output.emit(result)
    assert capsys.readouterr().out == 'SERVICE DOWN · ' + warning + '\n'
    output.emit(result, json_output=True)
    assert json.loads(capsys.readouterr().out)['copy_warning'] == warning
    assert snapshot(vessel.folder) == before
