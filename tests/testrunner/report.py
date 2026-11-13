"""The three things a run leaves: ``result.json`` (what ran, on what, and what it proved), ``report.md`` (the
same, readable and self-contained, with the complete console) and ``failures/NNN.txt`` (each failure's
complete text). Each is written atomically into the run's verified results folder."""
from __future__ import annotations

import json
import os
import re

from .layout import write_atomic, write_json


def failure_text(number: int, failure: dict) -> str:
    lines = [f'failure {number:03d}', f'test: {failure["nodeid"]}', f'phase: {failure["phase"]}',
             f'worker: {failure.get("node") or "-"}', '', '== traceback ==', failure.get('longrepr') or '(none recorded)']
    for title, body in failure.get('sections') or []:
        lines += ['', f'== {title} ==', body]
    return '\n'.join(lines) + '\n'


def write_failures(results_fd: int, failures: list[dict]) -> list[dict]:
    index = []
    if not failures:
        return index
    try:
        os.mkdir('failures', 0o700, dir_fd=results_fd)
    except FileExistsError:
        pass
    folder = os.open('failures', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=results_fd)
    try:
        for number, failure in enumerate(failures, 1):
            name = f'{number:03d}.txt'
            data = failure_text(number, failure).encode('utf-8', errors='replace')
            write_atomic(folder, name, data)
            index.append({'file': f'failures/{name}', 'nodeid': failure['nodeid'], 'phase': failure['phase'],
                          'bytes': len(data)})
    finally:
        os.close(folder)
    return index


def fence(text: str) -> str:
    longest = max((len(run) for run in re.findall(r'`+', text)), default=0)
    return '`' * max(3, longest + 1)


def block(text: str) -> str:
    marks = fence(text)
    return f'{marks}\n{text.rstrip()}\n{marks}'


SUMMARY_LINES = 40
NEVER_COLLECTED_SETUP = ('Collection never finished: pytest\'s evidence recorder never started, so the run stopped in '
                         'setup, before pytest ran. What it would have run is unknown.')
NEVER_COLLECTED_DURING = ('Collection never finished: pytest started and the run stopped during its collection. What it '
                          'would have run is unknown.')
COLLECTION_INTERRUPTED = ('Collection was interrupted: pytest recorded only part of it, so what it would have run is '
                          'unknown.')
COLLECTION_UNVERIFIED = ('A collection was recorded, but the evidence plugin versions of its processes differ or are '
                         'missing, so whether it completed is unknown.')
PARTIAL_MISSING = 'Never reached a terminal report (partial, at least these: collection is not known to have finished)'
PLUGIN_1 = ('Read under evidence plugin version 1, which does not mark a completed collection: a recorded collection '
            'is taken as complete.')


def jail_line(jail: dict | None) -> str:
    if not jail or not jail.get('jailed'):
        return '- jailed: no'
    tool = jail.get('tool') or {}
    return (f'- jailed: yes, by the tool at `{tool.get("checkout")}` HEAD `{tool.get("head")}` (files match HEAD: '
            f'{tool.get("matches_head")}) · {jail.get("bubblewrap")} · {jail.get("namespaces")} · time limit '
            f'{jail.get("timeout_seconds")} s · memory {jail.get("memory_mib")} MiB ({jail.get("memory_resource")})')


def render(result: dict, console: str, failures: list[dict]) -> str:
    target, runner, outcome = result['target'], result['runner'], result['outcome']
    counts = result.get('evidence', {}).get('counts') or {}
    observed = result.get('evidence', {}).get('observed_at_least') or {}
    out = [f'# Test run {result["run"]}', '',
           f'- status: **{outcome["status"]}**',
           f'- exits: wrapper {outcome["wrapper_exit"]}, pytest {outcome.get("pytest_exit")}, '
           f'signal {outcome.get("signal")}, cleanup {result.get("cleanup", {}).get("outcome")}',
           f'- target: `{target["top"]}` branch `{target.get("branch")}` commit `{target["head"]}` tree `{target["tree"]}`',
           f'- change: `{result.get("change")}`',
           f'- run checkout: tree `{result.get("checkout", {}).get("tree")}` (equals target: '
           f'{result.get("checkout", {}).get("tree") == target["tree"]})',
           f'- runner: `{runner.get("checkout")}` HEAD `{runner.get("head")}` files match HEAD: {runner.get("matches_head")}',
           jail_line(result.get('jail')),
           f'- command: `{" ".join(result.get("argv", []))}`',
           f'- started {result.get("started")}, ended {result.get("ended")}, {result.get("seconds")} s', '']
    evidence = result.get('evidence') or {}
    out += ['## Counts', '']
    if evidence.get('complete'):
        out.append(', '.join(f'{key} {counts.get(key)}' for key in counts) +
                   f'; expected {evidence.get("expected")}, deselected {evidence.get("deselected")}')
    else:
        out.append('Evidence INCOMPLETE; counts unknown. Causes: ' + '; '.join(evidence.get('causes') or ['no evidence']))
        expected = evidence.get('expected')
        out.append('Observed at least: ' + ', '.join(f'{key} {value}' for key, value in observed.items()) +
                   f'; collection errors {evidence.get("collection_errors")}; '
                   f'expected {"unknown" if expected is None else expected}, reached terminal {evidence.get("terminal")}')
        partial = evidence.get('collection') in ('partial', 'unverified')
        if evidence.get('collection') == 'partial':
            out.append(COLLECTION_INTERRUPTED)
        elif evidence.get('collection') == 'unverified':
            out.append(COLLECTION_UNVERIFIED)
        elif expected is None:
            out.append(NEVER_COLLECTED_DURING if evidence.get('plugin') else NEVER_COLLECTED_SETUP)
        if evidence.get('missing'):
            out.append(f'{PARTIAL_MISSING if partial else "Never reached a terminal report"} '
                       f'({len(evidence["missing"])}):')
            out.append(block('\n'.join(evidence['missing'])))
    if evidence.get('plugin') == [1] and evidence.get('collection') != 'unverified':
        out.append(PLUGIN_1)
    out += ['', '## Failures', '']
    if not failures:
        out.append('None recorded.')
    for entry, failure in zip(result.get('failures', []), failures):
        text = failure_text(int(entry['file'][9:12]), failure)
        lines = text.splitlines()
        shown = '\n'.join(lines[:SUMMARY_LINES])
        out += [f'### {entry["file"]}: `{entry["nodeid"]}` ({entry["phase"]})', '', block(shown)]
        if len(lines) > SUMMARY_LINES:
            out.append(f'Summary truncated at {SUMMARY_LINES} of {len(lines)} lines; the complete text is '
                       f'{entry["file"]} ({entry["bytes"]} bytes), retained with this report.')
        out.append('')
    for text in evidence.get('texts') or []:
        out += [f'### {text["kind"]} ({text["role"]})', '', block(text['text']), '']
    out += ['## Source before and after', '']
    for name in ('target', 'checkout'):
        manifests = result.get('manifests', {}).get(name, {})
        for key, value in manifests.items():
            out.append(f'- {name} {key}: `{json.dumps(value, sort_keys=True)}`')
    out += ['', '## Environment and tools', '']
    for key, value in result.get('environment', {}).items():
        out.append(f'- {key}: `{json.dumps(value, sort_keys=True)}`')
    for key, value in result.get('tools', {}).items():
        out.append(f'- {key}: `{json.dumps(value, sort_keys=True)}`')
    out += ['', '## Cleanup', '', block(json.dumps(result.get('cleanup', {}), indent=2, sort_keys=True)), '',
            '## Complete console output', '', block(console or '(no console output)'), '']
    return '\n'.join(out)


def write(results_fd: int, result: dict, console: str, failures: list[dict]) -> None:
    """The report first, the result last: a result that names its outcome always has its report beside it."""
    write_atomic(results_fd, 'report.md', render(result, console, failures).encode('utf-8', errors='replace'))
    write_json(results_fd, 'result.json', result)
