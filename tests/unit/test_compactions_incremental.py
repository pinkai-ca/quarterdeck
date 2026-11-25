"""A compaction reading is kept incremental: an appended record feeds only its new lines to the kept interpretation.

The reading after appends equals a whole reading's; an append decodes only its new lines, through the compaction read
as through the readers; a history the interpretation refuses stays refused across appends, and a rewritten record is
read whole again. Synthetic histories only (tests/compaction_shapes.py).
"""
import json
from dataclasses import replace
import hashlib

import pytest

from quarterdeck.harness import claude, claude_record, codex, json_record
from tests.compaction_shapes import Native

PROGRAMS = [('claude', claude), ('codex', codex)]


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.setattr(json_record, '_KEEP_ROWS', False)
    json_record._snapshots.clear()
    yield
    json_record._snapshots.clear()


def comparable(reading):
    return replace(reading, prefix=None, state=None)


def history(tmp_path, program):
    native = Native(tmp_path / 'record.jsonl', program)
    native.call()
    native.result()
    native.finish()
    return native


@pytest.mark.parametrize('program,module', PROGRAMS)
def test_the_reading_after_appends_equals_a_whole_reading(tmp_path, program, module):
    native = history(tmp_path, program)
    assert module.compactions(native.path).reason is None
    for step in range(3):
        native.begin(f'Synthetic task {step}')
        module.read(native.path)  # a status reading between ticks: the entry moves on
        native.compact()
        native.call()
        native.result()
        native.finish()
        incremental = module.compactions(native.path)
    json_record._snapshots.clear()
    whole = module.compactions(native.path)
    assert incremental.reason is None and incremental.count == 3
    assert comparable(incremental) == comparable(whole)
    raw = native.path.read_bytes()
    extent = raw.index(b'\n') + 1
    assert whole.digest == hashlib.sha256(raw).hexdigest()
    assert whole.matches_prefix(extent, hashlib.sha256(raw[:extent]).hexdigest())


@pytest.mark.parametrize('program,module', PROGRAMS)
def test_an_append_decodes_only_its_new_lines(tmp_path, monkeypatch, program, module):
    native = history(tmp_path, program)
    module.read(native.path)
    module.compactions(native.path)
    before = len(native.rows)
    decoded = []
    original = json.loads
    monkeypatch.setattr(json_record.json, 'loads', lambda raw, *a, **k: decoded.append(raw) or original(raw, *a, **k))
    native.compact()
    native.begin('Synthetic later task')
    reading = module.compactions(native.path)
    assert reading.reason is None and reading.count == 1
    assert len(decoded) == len(native.rows) - before, (len(decoded), len(native.rows) - before)


def test_a_refused_history_stays_refused_across_appends_and_a_rewrite_reads_whole(tmp_path):
    native = history(tmp_path, 'claude')
    assert claude.compactions(native.path).reason is None
    native.add('system', subtype='compact_boundary', compactMetadata=dict(trigger='unknown'))  # refused
    assert claude.compactions(native.path).reason is not None
    native.finish()
    assert claude.compactions(native.path).reason is not None, 'refused rows stay in the history'
    refused = next(index for index, row in enumerate(native.rows) if row.get('compactMetadata', {}).get('trigger') == 'unknown')
    native.rows = native.rows[:refused]  # rewritten from before the refused row: the prefix no longer proves
    native.previous = native.rows[-1]['uuid']
    native.finish()
    assert claude.compactions(native.path).reason is None


def candidate_boundary(native, trigger, anchor='synthetic-summary', *, form='witnessed'):
    # The interactive saved forms with synthetic values.
    kept = [row['uuid'] for row in native.rows[-2:]]
    native.compact()
    row = native.rows[-1]
    row.update(content='Conversation compacted', level='info', userType='external', entrypoint='cli',
               cwd='/synthetic/workspace', version='2.1.331', gitBranch='main', slug='synthetic-slug',
               dataFlowOn=True, compactMetadata={
                   'trigger': trigger, 'preTokens': 100, 'postTokens': 10,
                   'cumulativeDroppedTokens': 90, 'durationMs': 20,
                   'preservedSegment': dict(headUuid=kept[0], anchorUuid=anchor, tailUuid=kept[-1]),
                   'preservedMessages': dict(anchorUuid=anchor, uuids=kept, allUuids=kept),
                   'preCompactOrganizations': ['synthetic-org'],
                   'preCompactOrganizationsCoverStart': True,
               })
    if form != 'witnessed':
        metadata = row['compactMetadata']
        del metadata['preCompactOrganizationsCoverStart']
        metadata['preCompactDiscoveredTools'] = ['SyntheticTool']
        if form == 'precomputed':
            metadata['precomputed'] = True
        elif form == 'is-meta':
            row['isMeta'] = False
    return row


@pytest.mark.parametrize('trigger,form', [
    ('manual', 'witnessed'), ('auto', 'witnessed'), ('auto', 'precomputed'), ('auto', 'is-meta'),
])
@pytest.mark.parametrize('compatibility', [False, True], ids=['streaming', 'full'])
def test_interactive_candidate_boundary_counts_once_and_preserves_the_turn(tmp_path, monkeypatch, trigger, form,
                                                                      compatibility):
    native = history(tmp_path, 'claude')
    before = claude.compactions(native.path)
    candidate_boundary(native, trigger, form=form)
    native.add('user', isCompactSummary=True, message=dict(role='user', content='Synthetic summary'))
    native.rows[-1]['uuid'] = 'synthetic-summary'
    if compatibility:
        monkeypatch.setattr(claude_record, 'ROW_BYTES', 1024)
        native.rows.append(dict(type='file-history-snapshot', message=dict(content='x' * 1024)))
    native.flush()

    after = claude.compactions(native.path)
    assert after.reason is None and after.count == 1
    assert after.state.projection_usage.compatibility is compatibility
    assert after.matches_prefix(before.extent, before.digest)
    assert after.conversation == before.conversation
    assert after.complete and after.turn == before.turn and after.frontier != before.frontier
    assert [item.text for item in claude.inputs(native.path)] == ['Synthetic task']


@pytest.mark.parametrize('variant', ['full', 'precomputed', 'replacement', 'headless', 'direct-idle',
                                    'wrong-anchor', 'wrong-token-type', 'wrong-parent', 'missing-version'])
def test_distinct_or_malformed_candidate_auto_boundary_leaves_the_count_unknown(tmp_path, variant):
    native = history(tmp_path, 'claude')
    native.rows[-1]['version'] = '2.1.331'
    native.flush()
    assert claude.compactions(native.path).count == 0
    row = candidate_boundary(native, 'auto')
    metadata = row['compactMetadata']
    if variant in ('full', 'replacement'):
        del metadata['preservedSegment'], metadata['preservedMessages']
    if variant == 'precomputed':
        metadata['precomputed'] = True
    if variant == 'headless':
        metadata['runningTurnPrompt'] = {}
    if variant == 'direct-idle':
        row['isMeta'] = False
    if variant == 'wrong-anchor':
        metadata['preservedSegment']['anchorUuid'] = row['uuid']
    if variant == 'wrong-token-type':
        metadata['postTokens'] = True
    if variant == 'wrong-parent':
        row['parentUuid'] = row['logicalParentUuid']
    if variant == 'missing-version':
        del row['version']
    if variant == 'replacement':
        metadata['precomputed'] = True
    native.flush()

    reading = claude.compactions(native.path)
    assert reading.count is None and reading.reason is not None
    native.begin('Synthetic later task')
    native.finish()
    assert claude.compactions(native.path).count is None


@pytest.mark.parametrize('form,row_changes,metadata_changes', [
    ('precomputed', {}, dict(precomputed=False)),
    ('precomputed', {}, dict(precomputed=1)),
    ('precomputed', {}, dict(unmeasured=True)),
    ('precomputed', {}, dict(preCompactDiscoveredTools=[])),
    ('precomputed', {}, dict(preCompactDiscoveredTools='SyntheticTool')),
    ('precomputed', {}, dict(preCompactDiscoveredTools=['SyntheticTool', None])),
    ('precomputed', dict(isMeta=False), {}),
    ('precomputed', {}, dict(preCompactOrganizationsCoverStart=True)),
    ('is-meta', dict(isMeta=True), {}),
    ('is-meta', dict(isMeta=0), {}),
    ('is-meta', dict(unmeasured=True), {}),
    ('is-meta', {}, dict(preCompactOrganizationsCoverStart=True)),
    ('is-meta', {}, dict(preCompactOrganizations=[])),
    ('is-meta', {}, dict(preCompactDiscoveredTools=[None])),
])
def test_unmeasured_variants_of_the_new_auto_boundaries_leave_the_count_unknown(
        tmp_path, form, row_changes, metadata_changes):
    native = history(tmp_path, 'claude')
    row = candidate_boundary(native, 'auto', form=form)
    row.update(row_changes)
    row['compactMetadata'].update(metadata_changes)
    native.flush()

    reading = claude.compactions(native.path)
    assert reading.count is None and reading.reason is not None


@pytest.mark.parametrize('version', [None, '2.1.317', '2.1.325'])
def test_legacy_automatic_boundaries_keep_the_existing_metadata_contract(tmp_path, version):
    native = history(tmp_path, 'claude')
    native.compact()
    if version is not None:
        native.rows[-1]['version'] = version
        native.flush()
    reading = claude.compactions(native.path)
    assert reading.reason is None and reading.count == 1
