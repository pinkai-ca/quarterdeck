"""What a run's recorded events prove: the expected set, what reached a terminal report, the counts, and each
failure's complete text. A count is stated only when the events prove the run complete; otherwise it is
``None`` (unknown) and the observed figure is given as a lower bound. Known collection errors stay known. With
no collection recorded, or only part of one, the expected count is ``None`` too: what pytest would have run is
unknown, not empty. Evidence plugin 2 marks a collection that completed; plugin 1 evidence, which cannot tell,
is read as before: a recorded collection is taken as complete."""
from __future__ import annotations

import json
from pathlib import Path

CATEGORIES = ('passed', 'failed', 'error', 'skipped', 'xfailed', 'xpassed')


def load(folder: Path) -> tuple[dict[str, list[dict]], list[str]]:
    """Events by role; a torn last line (a process killed mid-write) is noted, never guessed at."""
    events, problems = {}, []
    if not folder.is_dir():
        return events, ['no evidence folder: the evidence plugin never started']
    for path in sorted(folder.glob('*.jsonl')):
        lines = path.read_text(encoding='utf-8', errors='replace').split('\n')
        role = path.stem
        events[role] = []
        for number, line in enumerate(lines, 1):
            if not line:
                continue
            try:
                events[role].append(json.loads(line))
            except ValueError:
                problems.append(f'{path.name} line {number}: unreadable event (torn write)')
    return events, problems


def category(report: dict) -> str | None:
    """The terminal category a phase report adds, as pytest's own summary counts it."""
    when, outcome, xfail = report['when'], report['outcome'], report.get('wasxfail')
    if outcome == 'failed':
        return 'failed' if when in ('call', '???') else 'error'
    if outcome == 'skipped':
        return 'xfailed' if xfail else 'skipped'
    if outcome == 'passed' and when == 'call':
        return 'xpassed' if xfail else 'passed'
    return None


def analyse(folder: Path, pytest_exit: int | None) -> dict:
    events, problems = load(folder)
    main = events.get('main', [])
    starts = [event for role in events.values() for event in role if event['kind'] == 'start']
    finish = next((event for event in main if event['kind'] == 'finish'), None)
    node_sets = {}
    for event in main:
        if event['kind'] == 'node_collected':
            node_sets[event['node']] = event['nodeids']
    xdist = bool(node_sets) or any(role != 'main' for role in events)
    expected, collection_agreed, completed = [], True, False
    collected = [event for event in main if event['kind'] == 'collected']
    marked = bool(starts) and all(isinstance(event.get('plugin'), int) and event['plugin'] >= 2 for event in starts)
    if node_sets:
        sets = [tuple(ids) for ids in node_sets.values()]
        collection_agreed = all(set(ids) == set(sets[0]) for ids in sets)
        expected = sorted(set().union(*map(set, sets)))
        # each worker's own collection must have completed; the controller's set comes from the same finally
        completed = all(any(event['kind'] == 'collected' and event.get('complete') for event in events.get(node, []))
                        for node in node_sets)
    elif collected:
        expected = collected[-1]['nodeids']
        completed = bool(collected[-1].get('complete'))
    collection = ('none' if not (node_sets or collected) else
                  'complete' if completed or not marked else 'partial')
    collection_known = collection == 'complete'
    deselected = sorted({nodeid for role in events.values() for event in role if event['kind'] == 'deselected'
                         for nodeid in event['nodeids']})
    collect_errors = {}
    for role in events.values():
        for event in role:
            if event['kind'] == 'collect' and event['outcome'] == 'failed':
                collect_errors.setdefault(event['nodeid'], event)
    reports = [event for event in main if event['kind'] == 'report']
    terminal, observed, failures = set(), dict.fromkeys(CATEGORIES, 0), []
    for report in reports:
        kind = category(report)
        if kind:
            observed[kind] += 1
        if report['when'] in ('teardown', '???'):
            terminal.add(report['nodeid'])
        if report['outcome'] == 'failed':
            failures.append({'nodeid': report['nodeid'], 'phase': report['when'], 'node': report.get('node'),
                             'longrepr': report.get('longrepr', ''), 'sections': report.get('sections', [])})
    for nodeid, event in collect_errors.items():
        failures.append({'nodeid': nodeid or '(collection)', 'phase': 'collection', 'node': event['role'],
                         'longrepr': event.get('longrepr', ''), 'sections': event.get('sections', [])})
    lost = [event for event in main if event['kind'] == 'node_down' and event.get('error')]
    crashes = [event for event in main if event['kind'] == 'crash']
    texts = [{'kind': event['kind'], 'role': event['role'], 'text': event.get('text', '')}
             for role in events.values() for event in role if event['kind'] in ('internalerror', 'interrupt')]
    missing = sorted(set(expected) - terminal)
    causes = []
    if not starts:
        causes.append('the evidence plugin never started')
    if finish is None:
        causes.append('the session did not finish (no sessionfinish event)')
    if collection == 'none':
        causes.append('collection never finished (no collection was recorded)')
    elif collection == 'partial':
        causes.append('collection interrupted: the recorded collection is partial')
    if problems:
        causes.append('torn evidence lines')
    if not collection_agreed:
        causes.append('xdist workers collected different sets')
    if collect_errors:
        causes.append(f'{len(collect_errors)} collection error(s)')
    if lost or crashes:
        causes.append(f'{len(lost)} worker(s) lost, {len(crashes)} crashed item(s)')
    if missing:
        causes.append(f'{len(missing)} expected item(s) never reached a terminal report')
    if texts:
        causes.append('internal error or interruption recorded')
    if finish is not None:
        status = finish['exitstatus']
        bad = observed['failed'] + observed['error']
        agrees = ((status == 0 and bad == 0 and expected) or (status == 1 and bad > 0)
                  or (status == 5 and not expected))
        if pytest_exit is not None and pytest_exit != status:
            causes.append(f'pytest exited {pytest_exit} but its session finished with {status}')
        elif not agrees:
            causes.append(f'session exit status {status} disagrees with the recorded reports')
    complete = not causes
    return {
        'complete': complete,
        'causes': causes,
        'xdist': xdist,
        'plugin': sorted({event.get('plugin') for event in starts if event.get('plugin') is not None}),
        'tools': sorted({(event.get('pytest'), event.get('xdist'), event.get('python')) for event in starts}),
        'plugins': sorted({name for event in starts for name in event.get('plugins', [])}),
        'collection': collection,
        'expected': len(expected) if collection_known else None,
        'deselected': len(deselected),
        'terminal': len(terminal & set(expected)),
        'missing': missing,
        'collection_errors': len(collect_errors),
        'counts': dict(observed) if complete else dict.fromkeys(CATEGORIES),
        'observed_at_least': dict(observed),
        'workers_lost': [{'node': event['node'], 'error': event['error']} for event in lost],
        'crashed_items': [event['nodeid'] for event in crashes],
        'session_exit': finish['exitstatus'] if finish else None,
        'failures': failures,
        'texts': texts,
        'problems': problems,
    }
