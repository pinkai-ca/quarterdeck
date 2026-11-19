"""Ordinary operational hold/log/recovery and final-notice rendering fixtures only."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from quarterdeck import cascade, cascade_runtime, watch
from quarterdeck.service import Service
from .test_cascade import current, report, ring  # noqa: F401


pytestmark = pytest.mark.usefixtures('synthetic_disk_room')


@pytest.mark.parametrize('error,reason', [
    (OSError('Synthetic ordinary run unavailable'), 'cascade evidence is unreadable'),
    (ValueError('Synthetic ordinary coordination unavailable'), 'cascade evidence is inconsistent')])
def test_operational_hold_is_visible_logs_once_across_coordinator_restart_and_clears(ring, monkeypatch, error, reason):
    data = cascade.load(ring.path)
    ring.runner.finish(ring.path, data, 'cascade declaration changed')
    second = ring.store.new('question', 'operator', 'conductor', 'Another fictional plan.')
    cascade.start(ring.service, second.id, 'conductor')
    observed, events = [], []
    failing = [True]

    def advance(runner, path):
        observed.append(path.name)
        if path == ring.path and failing[0]:
            raise error

    monkeypatch.setattr(cascade.Runner, 'advance', advance)

    def coordinator():
        return SimpleNamespace(ship=ring.ship, runtime=ring.ship.runtime, store=ring.store,
                               log=lambda event, **fields: events.append((event, fields)))

    Service.cascades(coordinator())
    assert second.id in observed, 'another retained run is processed in the same pass'
    assert events == [('cascade-held', dict(request=ring.question.id, reason=reason))]
    alerts = cascade_runtime.holds(ring.ship.runtime)
    assert len(alerts) == 1 and reason in alerts[0]
    for _ in range(2):
        Service.cascades(coordinator())  # no in-memory suppression survives in this new coordinator
    assert len(events) == 1
    state = replace(ring.ship, seats={}, pools={})
    before = sorted(str(p) for p in ring.ship.runtime.rglob('*'))
    facts = watch.facts(state, None)
    assert facts.cascade_held == alerts and facts.group_held == []
    assert 'cascade held:' in facts.line() and 'SERVICE DOWN' in facts.line()
    assert sorted(str(p) for p in ring.ship.runtime.rglob('*')) == before, 'status is read only'
    failing[0] = False
    Service.cascades(coordinator())
    assert cascade_runtime.holds(ring.ship.runtime) == [] and len(events) == 1
    assert watch.facts(state, None).cascade_held == []


def test_cascade_hold_category_survives_status_overflow_without_changing_group_count():
    assert watch.Facts('ok', 0, 0, 0).cascade_held == []
    facts = watch.Facts('down', 0, 0, 0, group_held=['group held: ' + 'g' * 200],
                        cascade_held=['cascade held: ' + 'c' * 200])
    assert '1 group held' in facts.line() and '1 cascade held' in facts.line()
    assert len(facts.line()) <= 160


@pytest.mark.parametrize('category', ['seat', 'provider-refusal'])
def test_final_notice_rendering_uses_explicit_category_for_withheld_answer(ring, category):
    ring.runner.advance(ring.path)
    completed = report(ring, text='A completed short garden note.', answer='A complete fictional garden answer.')
    ring.runner.advance(ring.path)
    data = cascade.load(ring.path)
    filed = None
    if category == 'provider-refusal':
        filed = report(ring, text='A separate fictional filed note.', answer='A separate fictional filed answer.')
        data['stopped_report'] = filed.id
    reason = 'beta: provider refused this step' if category == 'provider-refusal' else 'beta: owned pane is dead'
    # Rendering only: no provider record, exclusion, recognizer or lifecycle is exercised.
    ring.runner.finish(ring.path, data, reason, category=category)
    final = [m for m in ring.store.for_seat('conductor') if m.sender == 'ship'][0]
    assert str(ring.path / 'responses.md') in final.body
    if category == 'provider-refusal':
        assert completed.body not in final.body and current(ring).body not in final.body
        assert completed.id in final.body and 'Seat: beta; lap: 1' in final.body
        assert filed.id in final.body and filed.body not in final.body
        assert 'Interrupted order remains open' not in final.body
        assert 'report' not in data['steps'][-1] and data['last'] == completed.id
        assert 'last full answer is withheld' in final.body
        assert 'Conclude from completed notes only' in final.body
        assert 'Do not automatically read or retype' in final.body
    else:
        assert final.body.endswith(completed.body)
    assert cascade.load(ring.path)['stop_category'] == category
    assert ring.store.is_open(ring.question)
