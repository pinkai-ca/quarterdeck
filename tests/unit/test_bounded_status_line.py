"""Final status output uses one display-column budget."""

from dataclasses import fields, replace
from datetime import datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from quarterdeck import headlines, post, presentation, watch
from quarterdeck.harness.limits import Usage, Window
from quarterdeck.ledger import Ledger

from .test_headlines import AT, ship_at


def crowded():
    wide = '界' * 90
    exempt = {'needs_human', 'record_bounds', 'disks', 'login', 'usage', 'walls', 'pool_forecasts'}
    groups = {item.name: [f'{item.name} {wide}'] for item in fields(watch.Facts)
              if item.default_factory is list and item.name not in exempt}
    return watch.Facts('down', 23, 8, 2, unsettled=3, queued=4, oldest_queued=2,
                       needs_human=[f'{wide}-{n}' for n in range(50)], disks=[(wide, 97)],
                       login=[(wide, 'tomorrow')], record_bounds=[f'{wide}: record past the read bound'],
                       retirement=f'retiring {wide}', startup_delivery=f'starting {wide}',
                       accounting=f'accounting {wide}', cutover=f'default {wide}', **groups)


def test_every_category_obeys_the_budget_and_uses_complete_items():
    facts = crowded()
    line = facts.line()
    first, *items, last = line.split(headlines.SEPARATOR)
    assert first == 'SERVICE DOWN' and items[0] == '50 need human' and last == 'qd status'
    assert headlines.columns(line) <= headlines.WIDTH
    assert '界' not in line
    assert '1 usage evidence needs human' in items
    assert any(item.startswith('+') and item.endswith(' more') for item in items)
    deck = headlines.sections(facts)
    assert all(item in {group.summary for group in (*deck.immediate, *deck.lower)}
               or item.startswith('+') and item.endswith(' more') for item in items)
    assert facts.seat_holds[0] in headlines.details(facts)


def test_wide_items_and_both_annotations_share_final_cached_and_cold_budget():
    facts = watch.Facts('ok', 2, 5, 1, needs_human=['界' * 100] * 50, disks=[('root', 97)],
                        operational_notices=['界' * 100])
    publication = dict(line=facts.line(), headline=json.loads(json.dumps(headlines.cache_document(facts))))
    copy = headlines.note('copy warning ' + '界' * 100, 'copy warning')
    stale = headlines.note('status stale: published 15s ago', 'status stale')
    final = presentation._render(publication, lead='service busy 20s', notes=(stale, copy))
    cold = presentation.collected_line(facts, 15, notes=(copy,))
    for line, lead, freshness in ((final, 'service busy 20s', 'status stale'),
                                  (cold, 'service ok', 'publication 15s old')):
        assert line.startswith(lead) and freshness in line and 'copy warning' in line
        assert '50 need human' in line and line.endswith(' · qd status')
        assert headlines.columns(line) <= headlines.WIDTH and '界' not in line


@pytest.mark.parametrize(('name', 'reason', 'shown'), [
    ('builder', 'account holder approval is required', 'builder: account holder approval is required'),
    ('builder', 'account holder approval is required ' + '界' * 70, 'builder needs a human'),
    ('界' * 70, 'account holder approval is required', '1 needs a human'),
])
def test_human_alert_keeps_the_best_complete_item(name, reason, shown):
    full_reason = f'{name}: {reason}'
    facts = watch.Facts('down', 3, 2, 0, needs_human=[name], seat_holds=[full_reason], disks=[('/', 97)])
    published = json.loads(json.dumps(headlines.cache_document(facts)))
    for line in (facts.line(), headlines.render(headlines.recover_document(published))):
        first, human, *items, last = line.split(headlines.SEPARATOR)
        assert (first, human, last) == ('SERVICE DOWN', shown, 'qd status')
        assert items[0] == 'disk: / 97%' and any(item.startswith('3 waiting') for item in items)
        assert '1 seat held' not in items and full_reason not in items
        assert headlines.columns(line) <= headlines.WIDTH
    assert full_reason in headlines.details(facts)


def test_an_overlong_human_item_cannot_hide_another_seats_reason():
    long_name = '界' * 70
    reason = 'builder: account holder approval is required'
    facts = watch.Facts('ok', 0, 0, 0, needs_human=[long_name, 'builder'],
                        seat_holds=[f'{long_name}: action required', reason])
    assert facts.line().split(headlines.SEPARATOR) == ['service ok', reason, '1 needs a human', 'qd status']


def test_named_priority_precedes_extra_seat_groups_and_annotations():
    facts = watch.Facts('ok', 1, 2, 0, needs_human=['op'], login=[('p', '03:10')],
                        disks=[('/', 97)], held=['a'], record_bounds=['b: record past the read bound'],
                        ending=['another program is ending'], manual_fresh=['a different program needs a fresh start'],
                        pane_obstructions=['another pane is too short'])
    notes = (headlines.note('status stale: published 15s ago', 'status stale'),
             headlines.note('copy warning ' + '界' * 150, 'copy warning'))
    line = headlines.status_line(facts, notes=notes)
    limit = 'held at limit' if 'held at limit' in line else '1 limit hold'
    necessary = ('service ok', 'status stale', 'copy warning', 'needs a human', 'login needed',
                 'disk: / 97%', limit, '1 past read bound', '1 waiting')
    positions = [line.index(item) for item in necessary]
    assert positions == sorted(positions)
    for item in ('another program', 'fresh start', 'pane'):
        assert item not in line or line.index(item) > positions[-1]
    assert line.endswith(' · qd status') and headlines.columns(line) <= headlines.WIDTH


def test_crowded_independent_collection_keeps_its_provenance_and_shortens_the_lead():
    state = 'busy 400s in ' + '界' * 100 + ': main loop stuck, nothing is delivered'
    # This case measures the budget with login pressure and no usage-pacing alert.
    facts = replace(crowded(), service=state, usage_errors=[])
    copy = headlines.note('copy warning ' + '界' * 100, 'copy warning')
    line = presentation.collected_line(facts, 120, notes=(copy,))
    lead, freshness, warning, human, *items, pointer = line.split(headlines.SEPARATOR)
    assert (lead, freshness, warning, human, pointer) == (
        'service busy: main loop stuck', 'collected here', 'copy warning', '50 need human', 'qd status')
    assert items[:3] == ['1 seat held', '1 sign-in failure', '1 login needed']
    assert any(item.startswith('+') and item.endswith(' more') for item in items)
    assert headlines.columns(line) <= headlines.WIDTH


def test_three_dead_panes_keep_time_and_exit_status_with_disk_and_waiting_alerts():
    when = '2026-11-27T06:00:00-08:00'
    exits = {'desk': (datetime.fromisoformat(when), 0),
             'worker1': (datetime.fromisoformat(when), 7), 'captain': (None, None)}
    facts = watch.Facts('ok', 2, 7, 0, disks=[('/', 94)], pane_exits=exits)
    headline = headlines.recover_document(json.loads(json.dumps(headlines.cache_document(facts))))
    line = headlines.render(headline)
    assert line == facts.line()
    assert 'pane died: desk 06:00:00 exit 0, worker1 06:00:00 exit 7, captain time unknown exit unknown' in line
    assert 'disk: / 94%' in line and '2 waiting, oldest 7m' in line
    assert headlines.columns(line) <= headlines.WIDTH
    details = '\n'.join(headlines.details(facts))
    assert f'desk: pane died at {when}; exit status 0' in details
    assert f'worker1: pane died at {when}; exit status 7' in details
    assert 'captain: pane died at unknown time; exit status unknown' in details


def test_dead_pane_names_remain_when_the_time_and_status_list_outgrows_the_line():
    exits = dict.fromkeys(('captain', 'builder', 'office', 'worker1', 'worker8', 'operator'), (None, None))
    facts = watch.Facts('ok', 0, 0, 0, pane_exits=exits)
    headline = headlines.recover_document(json.loads(json.dumps(headlines.cache_document(facts))))
    line = headlines.render(headline)
    assert 'pane died: captain, builder, office, worker1, worker8, operator' in line
    assert headlines.columns(line) <= headlines.WIDTH


def test_emoji_variations_and_keycaps_are_budgeted_conservatively():
    assert headlines.columns('✈️') >= 2
    assert headlines.columns('1️⃣') >= 2
    line = watch.Facts('ok', 0, 0, 0, needs_human=['✈️1️⃣' * 33]).line()
    assert '1 needs a human' in line and '✈' not in line
    assert line.endswith(' · qd status') and headlines.columns(line) <= headlines.WIDTH


def test_forecasts_have_only_pool_and_local_time_or_one_count():
    few = ['made-one spent by 1 Jan 01:23', 'made-two spent by 1 Jan 04:56']
    facts = watch.Facts('ok', 0, 0, 0, pool_forecast_headlines=few, operational_notices=['tool ready'])
    line = facts.line()
    assert line.index(few[0]) < line.index(few[1]) < line.index('tool ready') < line.index('qd status')
    many = [f'{i}-界界界 spent by 1 Jan 01:23' for i in range(20)]
    line = replace(facts, pool_forecast_headlines=many).line()
    assert '20 pool forecasts' in line and 'spent by' not in line
    assert line.endswith(' · qd status') and headlines.columns(line) <= headlines.WIDTH


def test_an_active_wall_suppresses_only_the_forecast_headline(tmp_path, monkeypatch):
    ship = ship_at(tmp_path)
    now = AT + timedelta(hours=1)
    monkeypatch.setattr(post, 'now', lambda: now)
    with Ledger(ship.runtime / 'ledger.sqlite') as book:
        for minutes, used in ((0, .1), (30, .3), (60, .5)):
            at = AT + timedelta(minutes=minutes)
            book.record_usage('claude', Usage((Window('7d', used, AT + timedelta(days=7)),
                                              Window('5h', 1, AT + timedelta(hours=5))), at))
    facts = watch.facts(ship, SimpleNamespace())
    assert facts.pool_forecasts and 'reset' in facts.pool_forecasts[0] and '%/h' in facts.pool_forecasts[0]
    assert facts.pool_forecast_headlines == [] and 'spent by' not in facts.line()
    assert facts.pool_forecasts[0] in headlines.details(facts)
