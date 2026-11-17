"""qd set --adopt writes the model and effort a seat runs, and nothing else the reading carries."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import cli, watch
from quarterdeck import ship as shipmod
from quarterdeck.harness.grok import GrokHarness
from quarterdeck.harness.grok_models import EFFORT_REFUSAL, MODEL_REFUSAL
from quarterdeck.ship import ShipError


FIXTURES = Path(__file__).parents[1] / 'fixtures/grok/1.0.77'


def screen(warnings: bool):
    rows = json.loads((FIXTURES / 'idle-mono.json').read_text())['lines']
    rows = [row.replace(' Grok 4.7 (high)', '─ Grok 4.7 (low)') for row in rows]
    if warnings:  # the program's measured refusal words, above the composer
        rows[:0] = ['     ' + EFFORT_REFUSAL, '', '     ' + MODEL_REFUSAL, '']
    return GrokHarness().screen('\n'.join(rows))


def adopted(drydock, monkeypatch, shown):
    ship = shipmod.load(drydock)
    monkeypatch.setattr(watch, 'Door', lambda seat: SimpleNamespace(screen=lambda: shown))
    seat = SimpleNamespace(name='worker-1', spec=ship.seat('worker-1'), record=lambda: None,
                           harness=GrokHarness())
    return cli.adopt(ship, seat), (drydock / 'ship.toml').read_text(), shipmod.load(drydock).seat('worker-1')


def test_adopt_with_the_model_refusal_on_screen_writes_only_model_and_effort(drydock, monkeypatch):
    shown = screen(True)
    assert shown.shows['selection_warning'] == MODEL_REFUSAL + '; ' + EFFORT_REFUSAL
    before = (drydock / 'ship.toml').read_text()
    lines, text, spec = adopted(drydock, monkeypatch, shown)
    assert [line.split(' = ')[0] for line in lines] == ['ship.toml: seats.worker-1.model',
                                                         'ship.toml: seats.worker-1.effort']
    assert spec.model == 'Grok 4.7' and spec.effort == 'low'
    assert 'selection_warning' not in text
    assert MODEL_REFUSAL not in text and EFFORT_REFUSAL not in text
    changed = [line for line in text.splitlines() if line not in before.splitlines()]
    assert changed == ['model = "Grok 4.7"', 'effort = "low"']


def test_adopt_with_no_warning_writes_the_same_model_and_effort(drydock, monkeypatch):
    lines, text, spec = adopted(drydock, monkeypatch, screen(False))
    assert [line.split(' = ')[0] for line in lines] == ['ship.toml: seats.worker-1.model',
                                                         'ship.toml: seats.worker-1.effort']
    assert spec.model == 'Grok 4.7' and spec.effort == 'low'


def test_a_warning_alone_is_nothing_to_adopt(drydock, monkeypatch):
    shown = screen(True)
    bare = SimpleNamespace(**{**vars(shown), 'shows': {'selection_warning': shown.shows['selection_warning']}})
    before = (drydock / 'ship.toml').read_text()
    with pytest.raises(ShipError, match='nothing to adopt'):
        adopted(drydock, monkeypatch, bare)
    assert (drydock / 'ship.toml').read_text() == before
