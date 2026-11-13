"""Synthetic readable profiles and byte budgets; no native records or programs."""

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import carriage_plan as cp, doctor, ship


def item(name, text='ordinary text\n', *, kind='order', sender='lead', recipient='builder', at='morning'):
    return cp.Item(name, sender, recipient, kind, at, 'invented subject', text,
                   Path('/synthetic/posts') / (name + '.json'))


def plan(items, *, limit=2400, **options):
    return cp.build(tuple(items), seat='builder', header='qd > synthetic briefed header',
                    inline='canonical inline\n', directory=Path('/synthetic/carried/current'),
                    bound=cp.Bound(limit, 'policy bound'), force_files=True, **options)


def leaves(result):
    return [line for text in result.files.values() for line in text.splitlines() if line.startswith('id=')]


def test_handoff_whole_oldest_fitting_order_and_complete_overflow():
    posts = [item('very-large-old', 'old oversized\n' * 900, at='a'),
             item('next-order', 'oldest fitting\n' * 45, at='b'),
             item('self-question', 'question\n' * 80, kind='question', sender='builder', at='a'),
             item('handoff', 'whole handoff\n' * 35, sender='builder', at='z'),
             item('later-order', 'later order\n' * 70, at='c')]
    result = plan(posts)
    assert 'handoff' in result.whole
    assert 'next-order' in result.whole
    assert 'very-large-old' in result.overflow
    assert 'self-question' in result.overflow
    assert set(result.whole).isdisjoint(result.overflow)
    assert set(result.whole) | set(result.overflow) == {p.id for p in posts}
    assert len(leaves(result)) == len(result.overflow)
    for name in result.overflow:
        assert sum(line.startswith('id=' + json.dumps(name) + ' ') for line in leaves(result)) == 1
    assert result.required_bytes <= result.bound.bytes
    assert all(cp.size(text) <= result.bound.bytes for text in result.files.values())


@pytest.mark.parametrize('kind', ['question', 'answer', 'report', 'ack'])
def test_self_traffic_is_not_promoted_to_handoff(kind):
    result = plan([item('self-traffic', 's' * 1200, kind=kind, sender='builder', at='a'),
                   item('ordinary-order', 'o' * 1200, at='z')])
    assert result.whole == ('ordinary-order',)
    assert result.overflow == ('self-traffic',)


def test_context_only_order_is_not_a_handoff():
    context = replace(item('context', 'c' * 1200, sender='builder', at='a'), context_only=True)
    result = plan([context, item('ordinary-order', 'o' * 1200, at='z')])
    assert result.whole == ('ordinary-order',)


def test_handoff_renders_first_then_other_admitted_thread_order():
    result = plan([item('old-order', 'OLD\n', at='a'),
                   item('amendment', 'AMENDMENT\n', kind='answer', at='b'),
                   item('handoff', 'HANDOFF\n', sender='builder', at='z')])
    assert result.whole == ('handoff', 'old-order', 'amendment')
    assert result.files['whole.txt'] == 'HANDOFF\nOLD\nAMENDMENT\n'


def test_amendment_cannot_be_whole_without_its_oversized_parent():
    amendment = replace(item('amendment', 'small amendment\n', kind='answer'), requires=('parent',))
    result = plan([item('parent', 'large order' * 1000), amendment])
    assert result.whole == ()
    assert result.overflow == ('parent', 'amendment')


def test_context_and_parent_fit_together_with_the_answer():
    context = replace(item('own-question', 'CONTEXT\n', kind='question', sender='builder'),
                      context_only=True, requires=('parent',))
    answer = replace(item('answer', 'ANSWER\n', kind='answer'), requires=('own-question',))
    result = plan([item('parent', 'PARENT\n'), context, answer])
    assert result.files['whole.txt'] == 'PARENT\nCONTEXT\nANSWER\n'


def test_handoff_requires_recipient_as_well_as_sender():
    result = plan([item('outgoing', 'x' * 1200, sender='builder', recipient='other', at='z'),
                   item('self-handoff', 'h' * 1200, sender='builder', at='a')])
    assert result.whole == ('self-handoff',)


def test_inline_is_byte_exact_and_creates_no_read_files():
    text = 'qd > ordinary\n' + 'é' * 100
    result = cp.build((item('ordinary'),), seat='builder', header='header', inline=text,
                      directory=Path('/synthetic/current'), bound=cp.Bound(cp.size(text), 'policy bound'))
    assert result.opening == text
    assert result.files == {}
    assert result.required_bytes == cp.size(text)


def test_exact_duplicate_is_counted_once():
    post = item('same', 'abc\n')
    result = plan([post, post, item('large', 'L' * 8000)])
    assert result.whole.count('same') == 1
    assert result.files['whole.txt'] == 'abc\n'


def test_unicode_huge_line_parts_reconstruct_exactly_and_indexes_stay_bounded():
    text = '🦉é\n' * 9000 + 'a' * 20000
    result = plan([item('oversized', text)], limit=1600)
    view_names = sorted((name for name in result.files if name.startswith('view-')),
                        key=lambda name: int(name.removesuffix('.txt').split('-')[-1]))
    assert ''.join(result.files[name] for name in view_names) == text
    assert len(view_names) > 20
    assert len([name for name in result.files if name.startswith('parts-')]) > 1
    assert all(cp.size(value) <= 1600 for value in result.files.values())
    assert result.required_bytes <= 1600
    assert result.overflow == ('oversized',)


def test_many_large_messages_form_complete_bounded_index_tree():
    posts = [item(f'post-item-{n:04}', 'text\n' * 1000) for n in range(150)]
    result = plan(posts, limit=1600)
    assert len(leaves(result)) == 150
    assert len([name for name in result.files if name.startswith('messages-')]) > 3
    assert 'overflow.txt' in result.files
    assert all(cp.size(value) <= 1600 for value in result.files.values())
    # Traverse only published links: no orphaned/missing leaf or part page.
    seen, found = set(), set()
    def visit(name):
        if name in seen:
            return
        seen.add(name)
        for line in result.files[name].splitlines():
            if line.startswith('id='):
                decoded, offset = json.JSONDecoder().raw_decode(line[3:])
                found.add(decoded)
                assert offset > 0
            else:
                row = json.loads(line)
                visit(Path(row['path']).name)
    visit('overflow.txt')
    assert found == {post.id for post in posts}


def test_metadata_is_one_line_and_subject_summary_is_explicit():
    post = replace(item('multiline', 'large' * 2000), subject='subject\n\t"' * 100)
    result = plan([post])
    row, = leaves(result)
    assert '\n' not in row and '\t' not in row
    assert '...' in row
    assert 'original="/synthetic/posts/multiline.json"' in row


def test_one_large_leaf_per_page_can_still_have_a_small_routing_index():
    posts = [replace(item(f'large-metadata-{n}', 'body' * 2000),
                     source=Path('/synthetic/' + 'long-path-' * 90 + str(n))) for n in range(8)]
    result = plan(posts, limit=1600)
    assert len(leaves(result)) == 8
    assert all(cp.size(value) <= 1600 for value in result.files.values())


def test_tiny_minimal_account_fails_before_publication(tmp_path):
    target = tmp_path / 'generation'
    with pytest.raises(cp.ImpossibleBudget, match='minimal first-message account'):
        cp.build((item('large', 'x' * 5000),), seat='builder', header='header', inline='x' * 5000,
                 directory=target, bound=cp.Bound(100, 'policy bound'))
    assert list(tmp_path.iterdir()) == []


def test_indivisible_identity_cannot_be_silently_cut():
    with pytest.raises(cp.ImpossibleBudget, match='minimal index entry'):
        plan([item('long-identity-' * 500, 'large' * 2000)], limit=1600)


def test_index_that_cannot_shrink_is_explicitly_impossible():
    # Long full identities fit leaf rows but prevent two route entries sharing a page.
    with pytest.raises(cp.ImpossibleBudget, match='shrinking index'):
        plan([item(str(n) + 'identity-' * 55, 'large' * 2000) for n in range(5)], limit=1600)


@pytest.mark.parametrize('value', [True, False, 0, -1, 1.5, '100000', None])
def test_ship_policy_rejects_non_positive_integer(value):
    with pytest.raises(ship.ShipError, match=r'ship\.carry_bytes'):
        ship._carry_bytes(value)


def test_ship_policy_default_and_declaration_are_single_source(tmp_path, monkeypatch):
    monkeypatch.setenv('QD_CARRY_BYTES', '999')
    assert ship._from_text('[ship]\n', tmp_path).carry_bytes == 100000
    assert ship._from_text('[ship]\ncarry_bytes = 1234\n', tmp_path).carry_bytes == 1234


@pytest.mark.parametrize('version,expected,label', [(None, 100000, 'policy bound'),
    ('different', 100000, 'policy bound'), ('synthetic-version', 3000, 'policy bound and measured adapter ceiling')])
def test_profile_is_exact_version_and_only_reduces_policy(version, expected, label):
    profile = cp.ReadableCapacity('synthetic-version', 3000, 'synthetic fixture', 'invented brief and work reserve')
    result = cp.effective_bound(100000, profile, version)
    assert (result.bytes, result.label) == (expected, label)
    assert cp.effective_bound(2000, profile, 'synthetic-version').bytes == 2000


def test_unknown_profile_never_becomes_a_hold():
    assert cp.effective_bound(100000, None, 'synthetic-version') == cp.Bound(100000, 'policy bound')


def test_doctor_reports_effective_bound_and_provenance():
    synthetic = SimpleNamespace(name='synthetic', readable_capacity=cp.ReadableCapacity(
        'fixture-version', 4000, 'invented measurement', 'synthetic standing and working reserves'))
    checked = doctor.carriage_bound_check(SimpleNamespace(carry_bytes=5000), synthetic, 'fixture-version')
    assert checked.passed
    assert '4000 UTF-8 bytes effective' in checked.detail
    assert 'ship.carry_bytes=5000' in checked.detail
    assert 'invented measurement' in checked.detail
    unknown = doctor.carriage_bound_check(SimpleNamespace(carry_bytes=5000), synthetic, None)
    assert unknown.passed and 'policy bound' in unknown.detail
    assert 'measured' not in unknown.detail


def test_publish_is_complete_private_and_never_rewrites(tmp_path):
    target = tmp_path / 'generation'
    result = cp.build((item('large', 'abc' * 6000), item('small')), seat='builder', header='header',
                      inline='large' * 6000, directory=target, bound=cp.Bound(2000, 'policy bound'))
    cp.publish(result)
    assert {path.name: path.read_text() for path in target.iterdir()} == result.files
    assert target.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in target.iterdir())
    before = {path.name: path.read_bytes() for path in target.iterdir()}
    with pytest.raises(FileExistsError):
        cp.publish(result)
    assert {path.name: path.read_bytes() for path in target.iterdir()} == before
    assert list(tmp_path.iterdir()) == [target]


def test_write_failure_leaves_no_published_partial_generation(tmp_path, monkeypatch):
    target = tmp_path / 'generation'
    result = cp.build((item('large', 'x' * 9000),), seat='builder', header='header', inline='x' * 9000,
                      directory=target, bound=cp.Bound(2000, 'policy bound'))
    def fail(_folder):
        raise OSError('synthetic write interruption')
    monkeypatch.setattr(cp.durable, 'sync_folder', fail)
    with pytest.raises(OSError, match='synthetic write interruption'):
        cp.publish(result)
    assert list(tmp_path.iterdir()) == []
