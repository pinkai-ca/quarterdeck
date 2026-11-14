"""What a run's recorded events prove: the expected set, what reached a terminal report, the counts, and each
failure's complete text. A count is stated only when the events prove the run complete; otherwise it is
``None`` (unknown) and the observed figure is given as a lower bound. Known collection errors stay known. With
no collection recorded, or only part of one, the expected count is ``None`` too: what pytest would have run is
unknown, not empty. Evidence plugin 2 marks a collection that completed, and every recorded process must have
marked its own; plugin 1 evidence, which cannot tell, is read as before when every process reports version 1: a
recorded collection is taken as complete. Versions that differ or are missing leave the collection unverified."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat

from .layout import DIRECTORY, Refused

CATEGORIES = ('passed', 'failed', 'error', 'skipped', 'xfailed', 'xpassed')


def load(folder: Path, *, parent_fd=None) -> tuple[dict[str, list[dict]], list[str]]:
    """Events by role; a torn last line (a process killed mid-write) is noted, never guessed at."""
    events, problems = {}, []
    if parent_fd is None:
        if not folder.is_dir():
            return events, ['no evidence folder: the evidence plugin never started']
        sources = [(path, None) for path in sorted(folder.glob('*.jsonl'))]
        return _load_sources(sources, events, problems)
    try:
        fd = os.open(folder.name, DIRECTORY, dir_fd=parent_fd)
    except FileNotFoundError:
        return events, ['no evidence folder: the evidence plugin never started']
    try:
        if os.fstat(fd).st_uid != os.getuid() or os.fstat(fd).st_dev != os.fstat(parent_fd).st_dev:
            raise Refused(f'{folder}: evidence ownership/filesystem differs; retained')
        sources = [(folder / name, fd) for name in sorted(os.listdir(fd)) if name.endswith('.jsonl')]
        return _load_sources(sources, events, problems)
    finally:
        os.close(fd)


def read_owned(parent_fd, name, where):
    """Read one regular evidence file through its verified directory, without following a link."""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_dev != os.fstat(parent_fd).st_dev:
            raise Refused(f'{where}: not this run\'s regular evidence file; retained')
        with os.fdopen(os.dup(fd), 'r', encoding='utf-8', errors='replace') as handle:
            return handle.read()
    finally:
        os.close(fd)


def _load_sources(sources, events, problems):
    for path, fd in sources:
        text = path.read_text(encoding='utf-8', errors='replace') if fd is None else read_owned(fd, path.name, path)
        lines = text.split('\n')
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


def marked_complete(events: list[dict]) -> bool:
    """Whether these events hold a collection its recorder marked complete (evidence plugin 2)."""
    return any(event['kind'] == 'collected' and event.get('complete') is True for event in events)


def start_summaries(starts: list[dict]) -> tuple[list, list, list[str]]:
    """Validate optional start metadata before hashing or comparing any of its values."""
    tools, plugins, problems = set(), set(), []
    for event in starts:
        versions = tuple(event.get(name) for name in ('pytest', 'xdist', 'python'))
        if all(value is None or isinstance(value, str) for value in versions):
            tools.add(versions)
        else:
            problems.append('malformed tools in start event: versions must be strings or null')
        names = event.get('plugins', [])
        if isinstance(names, list) and all(isinstance(name, str) for name in names):
            plugins.update(names)
        else:
            problems.append('malformed plugins in start event: expected a list of strings')
    # Missing optional tools remain null; a null and a measured version can both occur across processes.
    ordered_tools = sorted(tools, key=lambda row: tuple((value is not None, value or '') for value in row))
    return ordered_tools, sorted(plugins), list(dict.fromkeys(problems))


def analyse(folder: Path, pytest_exit: int | None, *, parent_fd=None) -> dict:
    events, problems = load(folder, parent_fd=parent_fd)
    main = events.get('main', [])
    starts = [event for role in events.values() for event in role if event['kind'] == 'start']
    tools, plugins, metadata_problems = start_summaries(starts)
    layouts = []
    for event in starts:
        if 'layout' not in event:
            continue
        if not isinstance(event['layout'], dict):
            metadata_problems.append('malformed layout in start event: expected an object')
        else:
            layouts.append({**event['layout'], 'role': event['role']})
    finish = next((event for event in main if event['kind'] == 'finish'), None)
    node_sets = {}
    for event in main:
        if event['kind'] == 'node_collected':
            node_sets[event['node']] = event['nodeids']
    xdist = bool(node_sets) or any(role != 'main' for role in events)
    expected, collection_agreed = [], True
    collected = [event for event in main if event['kind'] == 'collected']
    versions = [event.get('plugin') for event in starts]
    integers = bool(versions) and all(type(version) is int for version in versions)  # before any set is made
    legacy = integers and set(versions) == {1}
    marked = integers and len(set(versions)) == 1 and versions[0] >= 2  # one modern version, agreed by all
    # Every recorded worker counts: those the controller knows (a collection or a node going down) and every
    # worker whose own recorder started. Each must have marked its own collection complete; none is inferred.
    workers = (set(node_sets) | {event['node'] for event in main if event['kind'] == 'node_down'}
               | {role for role, items in events.items() if role != 'main' and any(e['kind'] == 'start' for e in items)})
    if node_sets:
        sets = [tuple(ids) for ids in node_sets.values()]
        collection_agreed = all(set(ids) == set(sets[0]) for ids in sets)
        expected = sorted(set().union(*map(set, sets)))
    elif collected:
        expected = collected[-1]['nodeids']
    completed = (all(marked_complete(events.get(worker, [])) for worker in workers)
                 and (bool(node_sets) or marked_complete(collected[-1:])))
    if not (node_sets or collected):
        collection = 'none'
    elif legacy:
        collection = 'complete'  # plugin 1 cannot tell a completed collection: read as before
    elif not marked:
        collection = 'unverified'  # versions differ or are missing: whether it completed is unknown
    else:
        collection = 'complete' if completed else 'partial'
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
    causes = list(metadata_problems)
    if not starts:
        causes.append('the evidence plugin never started')
    if finish is None:
        causes.append('the session did not finish (no sessionfinish event)')
    if collection == 'none':
        causes.append('collection never finished (no collection was recorded)')
    elif collection == 'partial':
        causes.append('collection interrupted: the recorded collection is partial')
    elif collection == 'unverified':
        causes.append('evidence plugin versions differ or are missing across processes: whether the recorded '
                      'collection completed is unknown')
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
        'plugin': sorted({version for version in versions if type(version) is int}),
        'tools': tools,
        'plugins': plugins,
        'collection': collection,
        'expected': len(expected) if collection_known else None,
        'session_layouts': layouts,
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
