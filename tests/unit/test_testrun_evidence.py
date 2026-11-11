"""What recorded pytest events prove: complete counts only when every expected item reached a terminal
report and the session finished in agreement; otherwise unknown, with known failures kept."""
import json

from tests.testrunner import evidence, report


def write(folder, role, *events):
    folder.mkdir(exist_ok=True)
    with (folder / f'{role}.jsonl').open('a') as handle:
        for event in events:
            handle.write(json.dumps({'role': role, **event}) + '\n')


def phases(nodeid, call='passed', setup='passed', teardown='passed', xfail=False, longrepr='', node=None):
    out = [{'kind': 'report', 'nodeid': nodeid, 'when': 'setup', 'outcome': setup, 'wasxfail': False,
            'longrepr': longrepr if setup == 'failed' else '', 'sections': [], 'node': node}]
    if setup == 'passed':
        out.append({'kind': 'report', 'nodeid': nodeid, 'when': 'call', 'outcome': call, 'wasxfail': xfail,
                    'longrepr': longrepr if call != 'passed' else '', 'sections': [['Captured stdout call', 'out']],
                    'node': node})
    out.append({'kind': 'report', 'nodeid': nodeid, 'when': 'teardown', 'outcome': teardown, 'wasxfail': False,
                'longrepr': longrepr if teardown == 'failed' else '', 'sections': [], 'node': node})
    return out


START = {'kind': 'start', 'plugin': 1, 'pytest': '9.0.2', 'xdist': '3.8.0', 'python': '3.14.4'}


def test_every_category_is_counted_as_pytest_counts_it(tmp_path):
    ids = ['t::passed', 't::failed', 't::setup', 't::teardown', 't::skip', 't::xfail', 't::xpass', 't::strict']
    events = [START, {'kind': 'collected', 'nodeids': ids}]
    events += phases('t::passed') + phases('t::failed', call='failed', longrepr='boom')
    events += phases('t::setup', setup='failed', longrepr='fixture broke')
    events += phases('t::teardown', teardown='failed', longrepr='teardown broke')
    events += phases('t::skip', call='skipped') + phases('t::xfail', call='skipped', xfail=True)
    events += phases('t::xpass', xfail=True)
    events += phases('t::strict', call='failed', longrepr='[XPASS(strict)] reason')
    events.append({'kind': 'finish', 'exitstatus': 1})
    write(tmp_path / 'e', 'main', *events)
    found = evidence.analyse(tmp_path / 'e', 1)
    assert found['complete'], found['causes']
    assert found['counts'] == {'passed': 2, 'failed': 2, 'error': 2, 'skipped': 1, 'xfailed': 1, 'xpassed': 1}
    assert [failure['phase'] for failure in found['failures']] == ['call', 'setup', 'teardown', 'call']


def test_a_missing_terminal_report_makes_every_count_unknown_but_keeps_what_is_known(tmp_path):
    events = [START, {'kind': 'collected', 'nodeids': ['t::a', 't::b', 't::c']}]
    events += phases('t::a', call='failed', longrepr='known failure')
    events += phases('t::b')[:2]  # setup and call, never torn down
    write(tmp_path / 'e', 'main', *events)
    found = evidence.analyse(tmp_path / 'e', None)
    assert not found['complete']
    assert found['counts'] == dict.fromkeys(evidence.CATEGORIES)
    assert found['observed_at_least']['failed'] == 1 and found['observed_at_least']['passed'] == 1
    assert found['missing'] == ['t::b', 't::c']
    assert any('did not finish' in cause for cause in found['causes'])
    assert found['failures'][0]['longrepr'] == 'known failure'


def test_collection_errors_stay_known_when_execution_is_unknown(tmp_path):
    write(tmp_path / 'e', 'main', START,
          {'kind': 'collect', 'nodeid': 'tests/test_bad.py', 'outcome': 'failed', 'longrepr': 'SyntaxError', 'sections': []},
          {'kind': 'collected', 'nodeids': []}, {'kind': 'finish', 'exitstatus': 2})
    found = evidence.analyse(tmp_path / 'e', 2)
    assert not found['complete'] and found['collection_errors'] == 1
    assert found['failures'] == [{'nodeid': 'tests/test_bad.py', 'phase': 'collection', 'node': 'main',
                                  'longrepr': 'SyntaxError', 'sections': []}]


def test_xdist_expected_set_comes_from_every_worker_and_a_lost_worker_is_incomplete(tmp_path):
    ids = ['t::a', 't::b']
    write(tmp_path / 'e', 'main', START,
          {'kind': 'node_collected', 'node': 'gw0', 'nodeids': ids},
          {'kind': 'node_collected', 'node': 'gw1', 'nodeids': ids},
          *phases('t::a', node='gw0'),
          {'kind': 'crash', 'nodeid': 't::b', 'node': 'gw1', 'longrepr': 'worker gw1 crashed'},
          {'kind': 'report', 'nodeid': 't::b', 'when': '???', 'outcome': 'failed', 'wasxfail': False,
           'longrepr': 'worker gw1 crashed while running t::b', 'sections': [], 'node': 'gw1'},
          {'kind': 'node_down', 'node': 'gw1', 'error': 'Not properly terminated'},
          {'kind': 'finish', 'exitstatus': 1})
    write(tmp_path / 'e', 'gw0', START, {'kind': 'deselected', 'nodeids': ['t::c']},
          {'kind': 'collected', 'nodeids': ids})
    found = evidence.analyse(tmp_path / 'e', 1)
    assert found['expected'] == 2 and found['deselected'] == 1 and found['xdist']
    assert not found['complete'] and found['crashed_items'] == ['t::b']
    assert found['workers_lost'] == [{'node': 'gw1', 'error': 'Not properly terminated'}]
    assert found['failures'][0]['phase'] == '???'


def test_workers_that_disagree_on_collection_are_incomplete(tmp_path):
    write(tmp_path / 'e', 'main', START, {'kind': 'node_collected', 'node': 'gw0', 'nodeids': ['t::a']},
          {'kind': 'node_collected', 'node': 'gw1', 'nodeids': ['t::a', 't::b']}, *phases('t::a'), *phases('t::b'),
          {'kind': 'finish', 'exitstatus': 0})
    found = evidence.analyse(tmp_path / 'e', 0)
    assert not found['complete'] and any('different sets' in cause for cause in found['causes'])


def test_an_exit_that_disagrees_with_the_reports_is_incomplete(tmp_path):
    write(tmp_path / 'e', 'main', START, {'kind': 'collected', 'nodeids': ['t::a']}, *phases('t::a'),
          {'kind': 'finish', 'exitstatus': 0})
    assert not evidence.analyse(tmp_path / 'e', 3)['complete']
    assert evidence.analyse(tmp_path / 'e', 0)['complete']


def test_no_tests_is_complete_only_with_exit_five(tmp_path):
    write(tmp_path / 'e', 'main', START, {'kind': 'collected', 'nodeids': []}, {'kind': 'finish', 'exitstatus': 5})
    found = evidence.analyse(tmp_path / 'e', 5)
    assert found['complete'] and found['expected'] == 0


def test_no_evidence_and_torn_lines_are_never_complete(tmp_path):
    assert not evidence.analyse(tmp_path / 'absent', 0)['complete']
    write(tmp_path / 'e', 'main', START, {'kind': 'collected', 'nodeids': []}, {'kind': 'finish', 'exitstatus': 5})
    with (tmp_path / 'e' / 'main.jsonl').open('a') as handle:
        handle.write('{"kind": "repo')
    found = evidence.analyse(tmp_path / 'e', 5)
    assert not found['complete'] and found['problems']


def test_failure_files_are_complete_and_the_report_summary_points_at_them(tmp_path):
    long = '\n'.join(f'line {n}' for n in range(5000))
    failures = [{'nodeid': 't::a', 'phase': 'call', 'node': 'gw0', 'longrepr': long,
                 'sections': [['Captured stdout call', 'printed ```` with fences']]}]
    folder = tmp_path / 'results'
    folder.mkdir()
    import os
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        index = report.write_failures(fd, failures)
        result = {'run': 'synthetic', 'target': {'top': '/t', 'head': 'h', 'tree': 't'}, 'runner': {},
                  'outcome': {'status': 'failed', 'wrapper_exit': 1}, 'failures': index,
                  'evidence': {'complete': True, 'counts': {'failed': 1}}}
        report.write(fd, result, 'console ``` text', failures)
    finally:
        os.close(fd)
    text = (folder / 'failures' / '001.txt').read_text()
    assert 'line 4999' in text and 'printed ```` with fences' in text
    rendered = (folder / 'report.md').read_text()
    assert 'failures/001.txt' in rendered and 'Summary truncated' in rendered and 'line 4999' not in rendered
    assert '````\nconsole ``` text' in rendered  # a fence longer than any run of backticks inside
    assert json.loads((folder / 'result.json').read_text())['format'] == 1
    assert sorted(path.name for path in folder.iterdir()) == ['failures', 'report.md', 'result.json']
