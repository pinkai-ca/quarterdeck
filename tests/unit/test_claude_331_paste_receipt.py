"""Synthetic replay of the wrapped idle user paste in witness item 4, row 82."""

from datetime import timedelta

import pytest

from quarterdeck import harness, input_receipt
from quarterdeck.harness import closed_record
from quarterdeck.harness.types import Input
from .test_claude_draft_return import AT, queue, record, row
from .test_closed_record import append, native_input, stamp, world  # noqa: F401 -- shared closed-launch fixture


VERSION = '2.1.331'
SINCE = AT + timedelta(seconds=1)
TYPED = ('qd > order from synthetic · ord-example · Paste test\n'
         'Report the sample.\n'
         '\tTAB-LED LINE\n'
         'ZWSP\u200bLINE\n'
         '<pasted_content id="example">\n'
         'qd > reply: qd post report --re ord-example\n')
RECORDED = TYPED.replace('\tTAB', '    TAB').replace('\u200b', '').replace('<pasted_content', '<\\pasted_content')


def wrapped(text, closing='a4e1'):
    return f'\n\n<pasted_content id="a4e1">\n{text.rstrip(chr(10))}\n</pasted_content id="{closing}">\n'


def pasted(text=RECORDED, second=2, **overrides):
    fields = dict(parentUuid='prior-turn', isSidechain=False, promptId='paste-prompt',
                  message=dict(role='user', content=wrapped(text)), uuid='paste-row',
                  permissionMode='bypassPermissions', origin=dict(kind='human'), promptSource='typed',
                  turnOrigin='human', turnPromptId='paste-prompt', turnPosition=dict(promptIndex=2, turnIndex=2),
                  userType='external', entrypoint='cli', cwd='/synthetic', sessionId='session',
                  version=VERSION, gitBranch='HEAD', dataFlowOn=True)
    return row('user', second, **(fields | overrides))


def receipt(path, text=TYPED):
    return input_receipt.confirmed('claude', path, text, SINCE)


def test_wrapped_idle_user_paste_confirms_all_three_measured_changes(tmp_path):
    path = record(tmp_path, pasted())
    assert receipt(path) == 'delivered'
    assert receipt(path, RECORDED) == 'delivered'
    assert receipt(path, (TYPED + 'different\n', TYPED)) == 'delivered'


@pytest.mark.parametrize(('typed', 'recorded'), [
    ('first\n\tIndented\nlast', 'first\n    Indented\nlast'),
    ('first\nZWSP\u200bLINE\nlast', 'first\nZWSPLINE\nlast'),
    ('first\n<pasted_content id="example">\nlast', 'first\n<\\pasted_content id="example">\nlast'),
])
def test_each_measured_change_can_confirm_a_paste(tmp_path, typed, recorded):
    assert receipt(record(tmp_path, pasted(recorded)), typed) == 'delivered'


@pytest.mark.parametrize('typed', [
    TYPED.replace('\u200b', '\u200c'),
    TYPED.replace('\u200b', '\u200d'),
    TYPED.replace('\u200b', '\ufeff'),
    TYPED.replace('Report the sample.', 'Report\tthe sample.'),
    TYPED.replace('Report the sample.', 'Report a different sample.'),
])
def test_unmeasured_changes_do_not_confirm_the_witness_row(tmp_path, typed):
    assert receipt(record(tmp_path, pasted()), typed) is None


@pytest.mark.parametrize(('typed', 'recorded'), [
    ('a\nbefore\tafter\nz', 'a\nbefore    after\nz'),
    ('a\n\t\tIndented\nz', 'a\n        Indented\nz'),
    ('a\n\tIndented\nz', 'a\n        Indented\nz'),
    ('a\n\tIndented\nz', 'a\n  Indented\nz'),
    ('a\nZWSPLINE\nz', 'a\nZWSP\u200bLINE\nz'),
    ('a\n</pasted_content id="example">\nz', 'a\n<\\/pasted_content id="example">\nz'),
    ('a <pasted_content id="example">\nz', 'a <\\pasted_content id="example">\nz'),
    ('a\n<pasted_content_example>\nz', 'a\n<\\pasted_content_example>\nz'),
])
def test_unmeasured_tab_tag_and_zws_renderings_stay_unconfirmed(tmp_path, typed, recorded):
    assert receipt(record(tmp_path, pasted(recorded)), typed) is None


@pytest.mark.parametrize('overrides', [
    {'version': '2.1.332'},
    {'version': None},
    {'isSidechain': True},
    {'isSidechain': None},
    {'isMeta': True},
    {'promptSource': 'queued'},
    {'origin': {'kind': 'unknown'}},
    {'turnOrigin': 'unknown'},
    {'message': {'role': 'user', 'content': RECORDED}},
    {'message': {'role': 'user', 'content': wrapped(RECORDED, closing='ffff')}},
    {'message': {'role': 'user', 'content': [{'type': 'text', 'text': wrapped(RECORDED)}]}},
])
def test_projection_requires_the_measured_typed_root_carrier(tmp_path, overrides):
    assert receipt(record(tmp_path, pasted(**overrides))) is None


@pytest.mark.parametrize('link', [
    {},
    {'promptId': 'paste-prompt'},
    {'turnPromptId': 'paste-prompt'},
    {'promptId': '', 'turnPromptId': ''},
    {'promptId': 42, 'turnPromptId': 42},
    {'promptId': 'paste-prompt', 'turnPromptId': 'other-prompt'},
])
def test_projection_requires_its_own_prompt_without_losing_literal_receipt(tmp_path, link):
    entry = pasted()
    del entry['promptId'], entry['turnPromptId']
    entry.update(link)
    path = record(tmp_path, entry)
    assert receipt(path) is None
    assert receipt(path, RECORDED) == 'delivered'


def test_unknown_mixed_and_stale_version_proofs(tmp_path):
    path = record(tmp_path, row('api-request', 0, version='2.1.317'), pasted(second=0.5))
    assert receipt(path) is None
    append(path, pasted())
    assert receipt(path) == 'delivered'
    append(path, row('api-request', 3, version=[]))
    assert receipt(path) is None
    assert receipt(path, RECORDED) == 'delivered'


def test_mixed_versions_before_and_after_candidate_are_unconfirmed(tmp_path):
    before = tmp_path / 'before'
    before.mkdir()
    assert receipt(record(before, row('api-request', 1, version='2.1.317'), pasted())) is None
    assert receipt(record(tmp_path, pasted(), row('api-request', 3, version='2.1.317'))) is None


@pytest.mark.parametrize('terminal', [None, 'remove', 'user-echo'])
def test_queue_keeps_its_literal_receipts_and_never_inherits_the_projection(tmp_path, terminal):
    fresh = [row('api-request', 1, version=VERSION), queue(wrapped(RECORDED), 2)]
    if terminal == 'remove':
        fresh.append(queue(wrapped(RECORDED), 3, 'remove'))
    elif terminal == 'user-echo':
        fresh.append(pasted(second=3))
    path = record(tmp_path, *fresh)
    assert receipt(path) is None
    assert receipt(path, RECORDED) == ('queued' if terminal is None else 'delivered')


@pytest.mark.parametrize('name', [name for name in harness.NAMES if name != 'claude'])
def test_other_adapters_keep_their_receipt_comparison(tmp_path, monkeypatch, name):
    adapter = harness.get(name)
    path = record(tmp_path, pasted())
    monkeypatch.setattr(adapter, 'inputs', lambda _: [Input(RECORDED, SINCE)])
    assert input_receipt.confirmed(adapter, path, RECORDED, SINCE) == 'delivered'
    assert input_receipt.confirmed(adapter, path, TYPED, SINCE) is None
    assert input_receipt.confirmed(adapter, path, '\n' + RECORDED, SINCE) == (
        None if name == 'muse' else 'delivered')


def test_closed_record_presence_uses_the_same_carrier_projection(world):  # noqa: F811
    world.live['pane'] = None
    world.proof.data['payload'] = TYPED
    assert closed_record.not_in_closed_record(world.seat, world.proof)
    entry = dict(pasted(), timestamp=stamp(11)) if world.name == 'claude' else native_input(
        world.name, RECORDED, stamp(11))
    append(world.record, entry)
    assert closed_record.not_in_closed_record(world.seat, world.proof) is (world.name != 'claude')


@pytest.mark.parametrize('world', ['claude'], indirect=True)
@pytest.mark.parametrize(('entry_at', 'older_version_at', 'present'), [
    (19, 15, False),
    (20, 15, True),
    (21, 15, True),
    (21, 20, False),
])
def test_closed_record_checks_current_paste_projection_at_its_own_floor(
        world, entry_at, older_version_at, present):  # noqa: F811
    world.live['pane'] = None
    world.proof.data['payload'] = TYPED
    append(world.record, dict(type='api-request', version='2.1.317', timestamp=stamp(older_version_at)),
           dict(pasted(), timestamp=stamp(entry_at)))
    assert world.proof.confirmed(world.seat) == ('delivered' if present else None)
    assert closed_record.not_in_closed_record(world.seat, world.proof) is (not present)
