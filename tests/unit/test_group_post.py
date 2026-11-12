"""Synthetic group orders and crash recovery; no program or provider is invoked."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import io
from pathlib import Path

import pytest

from quarterdeck import cli, group_post, post, ship as shipmod, thread_context


ROOT = Path(__file__).resolve().parents[2]
BODY = 'Review this fictional proposal.\n\nKeep the dissent: α ≠ β.\n  Indented\tdetail.\n'


@pytest.fixture
def forum(tmp_path):
    text = (ROOT / 'ships/forum/ship.toml').read_text()
    (tmp_path / 'ship.toml').write_text(text)
    return shipmod.load(tmp_path)


def filed(store, copies):
    return [store.new('report', m.to, m.sender, f'{m.to}: synthetic review', re=m.id) for m in copies]


def notices(store):
    return [m for m in store.for_seat('lead') if m.sender == post.SHIP]


def test_optional_groups_preserve_existing_ship_constructors(forum):
    old = shipmod.Ship(forum.folder, forum.name, forum.operator, forum.workspace,
                      forum.pools, forum.sessions, forum.seats)
    assert old.groups == {}
    assert forum.groups['reviewers'] == shipmod.Group(
        'reviewers', ('reviewer-1', 'reviewer-2', 'reviewer-3', 'reviewer-4'), 'lead')


@pytest.mark.parametrize('declaration', [
    'lead="lead"\nmembers=[]', 'lead="missing"\nmembers=["reviewer-1"]',
    'lead="lead"\nmembers=["missing"]', 'lead="lead"\nmembers=["lead"]',
    'lead="lead"\nmembers=["reviewer-1","reviewer-1"]',
    'lead="lead"\nmembers="reviewer-1"', 'members=["reviewer-1"]',
    'lead="lead"\nmembers=["reviewer-1"]\nextra=true',
])
def test_invalid_declarations_are_named(forum, declaration):
    text = forum.file.read_text().replace('[groups.reviewers]', '[groups.old]')
    text += '\n[groups.reviewers]\n' + declaration + '\n'
    forum.file.write_text(text)
    with pytest.raises(shipmod.ShipError, match='group reviewers'):
        shipmod.load(forum.folder)


def test_group_address_cannot_also_name_a_seat(forum):
    forum.file.write_text(forum.file.read_text().replace('[groups.reviewers]', '[groups.lead]'))
    with pytest.raises(shipmod.ShipError, match='address also names'):
        shipmod.load(forum.folder)


def test_copies_keep_exact_body_separate_ids_and_common_request(forum):
    store = post.Store(forum.runtime)
    root, copies = group_post.submit(forum, store, 'reviewers', 'operator', BODY, subject='Proposal')
    assert root.sender == 'operator' and root.to == 'lead'
    assert len({m.id for m in (root, *copies)}) == 5
    assert {m.to for m in copies} == set(forum.groups['reviewers'].members)
    assert all(m.body.encode('utf-8') == BODY.encode('utf-8') for m in copies)
    assert all(m.re == root.id and m.sender == 'lead' and m.subject == 'Proposal' for m in copies)
    # These remain readable by the preceding Message reader, with no new fields.
    assert all(post.Message.from_json(m.to_json()) == m for m in (root, *copies))


def test_own_inbox_and_carriage_select_the_reviewers_copy(forum):
    store = post.Store(forum.runtime)
    root, copies = group_post.submit(forum, store, 'reviewers', 'operator', BODY)
    for copy in copies:
        assert store.for_seat(copy.to) == [copy]
        assert post.inbox_reading(store, copy.to)['open'] == [copy]
        selection = thread_context.select(store, copy.to)
        assert selection.incoming == [copy]
        assert selection.render() == post.typed([copy])
    assert store.open_for('lead') == [root]


def test_existing_request_keeps_its_requester_and_closes_only_on_leads_report(forum):
    store = post.Store(forum.runtime)
    original = store.new('order', 'operator', 'lead', BODY)
    root, copies = group_post.submit(forum, store, 'reviewers', 'lead', BODY, re=original.short)
    assert root == original and len(store.all()) == 5
    filed(store, copies)
    group_post.advance(forum, store)
    assert store.is_open(root)  # the service notice never impersonates the lead
    final = store.new('report', 'lead', 'operator', 'Combined reviews; dissent retained.', re=root.id)
    assert store.closing_report(root) == final
    assert all(not store.is_open(m) for m in copies)


def test_partial_reports_remain_pending_then_all_filed_notice_is_once(forum):
    store = post.Store(forum.runtime)
    root, copies = group_post.submit(forum, store, 'reviewers', 'operator', BODY)
    reports = filed(store, copies[:3])
    group_post.advance(forum, store)
    assert notices(store) == []
    reports += filed(store, copies[3:])
    group_post.advance(forum, store)
    notice, = notices(store)
    assert 'all filed' in notice.body and notice.re == root.id
    assert all(report.id in notice.body for report in reports)
    for _ in range(3):
        group_post.advance(forum, post.Store(forum.runtime))
    assert notices(post.Store(forum.runtime)) == [notice]


def test_failure_notice_is_terminal_and_names_unfiled_members(forum):
    store = post.Store(forum.runtime)
    _, copies = group_post.submit(forum, store, 'reviewers', 'operator', BODY)
    reports = filed(store, copies[:1])
    group_post.advance(forum, store,
                       lambda member, copy: 'owned pane is dead' if member == copies[1].to else None)
    notice, = notices(store)
    assert 'member failed' in notice.body
    assert reports[0].id in notice.body and f'{copies[1].to}: owned pane is dead' in notice.body
    assert 'Not yet filed: reviewer-3, reviewer-4' in notice.body
    filed(store, copies[1:])
    group_post.advance(forum, post.Store(forum.runtime))
    assert notices(post.Store(forum.runtime)) == [notice]


def test_filed_report_wins_over_a_later_member_failure(forum):
    store = post.Store(forum.runtime)
    _, copies = group_post.submit(forum, store, 'reviewers', 'operator', BODY)
    filed(store, copies)
    group_post.advance(forum, store, lambda member, copy: 'owned pane is dead')
    notice, = notices(store)
    assert 'all filed' in notice.body and 'Failed members:' not in notice.body


@pytest.mark.parametrize('published', range(6))
def test_crash_at_each_fanout_publication_recovers_fixed_intents(forum, monkeypatch, published):
    store = post.Store(forum.runtime)
    original = group_post._publish
    calls = 0

    def cut(store, message):
        nonlocal calls
        if calls == published:
            raise RuntimeError('synthetic crash')
        calls += 1
        original(store, message)

    monkeypatch.setattr(group_post, '_publish', cut)
    if published < 5:
        with pytest.raises(RuntimeError, match='synthetic crash'):
            group_post.submit(forum, store, 'reviewers', 'operator', BODY)
    else:
        group_post.submit(forum, store, 'reviewers', 'operator', BODY)
    path, = (forum.runtime / 'post/groups').glob('*.json')
    intent = json.loads(path.read_text())
    expected = {item['id'] for item in [intent['root'], *intent['copies']]}
    monkeypatch.setattr(group_post, '_publish', original)
    group_post.advance(forum, post.Store(forum.runtime))
    assert {m.id for m in post.Store(forum.runtime).all()} == expected
    assert path.read_text() == json.dumps(intent, ensure_ascii=False, indent=2) + '\n'


@pytest.mark.parametrize('after_publication', [False, True])
def test_notice_crash_reuses_its_saved_identity(forum, monkeypatch, after_publication):
    store = post.Store(forum.runtime)
    _, copies = group_post.submit(forum, store, 'reviewers', 'operator', BODY)
    filed(store, copies)
    original = group_post._publish

    def cut(store, message):
        if message.sender == post.SHIP:
            if after_publication:
                original(store, message)
            raise RuntimeError('synthetic notice crash')
        original(store, message)

    monkeypatch.setattr(group_post, '_publish', cut)
    with pytest.raises(RuntimeError, match='synthetic notice crash'):
        group_post.advance(forum, store)
    path, = (forum.runtime / 'post/groups').glob('*.json')
    intent = json.loads(path.read_text())['notice']
    monkeypatch.setattr(group_post, '_publish', original)
    group_post.advance(forum, post.Store(forum.runtime))
    notice, = notices(post.Store(forum.runtime))
    assert notice.id == intent['id'] and notice.body == intent['body']


def test_repeated_and_concurrent_forwarding_reuses_one_run(forum):
    store = post.Store(forum.runtime)
    root = store.new('order', 'operator', 'lead', BODY)

    def forward(_):
        return group_post.submit(forum, post.Store(forum.runtime), 'reviewers', 'lead', BODY, re=root.id)

    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(forward, range(8)))
    assert all(result == results[0] for result in results)
    assert len(post.Store(forum.runtime).all()) == 5


def test_different_body_or_group_snapshot_does_not_rebind_a_run(forum):
    store = post.Store(forum.runtime)
    root, _ = group_post.submit(forum, store, 'reviewers', 'operator', BODY)
    with pytest.raises(shipmod.ShipError, match='identical order'):
        group_post.submit(forum, store, 'reviewers', 'lead', BODY + 'change', re=root.id)
    group = replace(forum.groups['reviewers'], members=('reviewer-1',))
    changed = replace(forum, groups={'reviewers': group})
    with pytest.raises(shipmod.ShipError, match='different group fanout'):
        group_post.submit(changed, store, 'reviewers', 'lead', BODY, re=root.id)
    assert len(store.all()) == 5


def test_recovery_retains_the_original_membership_snapshot(forum):
    store = post.Store(forum.runtime)
    _, copies = group_post.submit(forum, store, 'reviewers', 'operator', BODY)
    filed(store, copies)
    group_post.advance(replace(forum, groups={}), store)
    assert 'all filed' in notices(store)[0].body


@pytest.mark.parametrize('kind', ['report', 'question', 'answer', 'ack', 'resend'])
def test_non_order_group_cli_fails_before_post_publication(forum, monkeypatch, kind):
    monkeypatch.setattr(cli, 'load_ship', lambda args: forum)
    monkeypatch.delenv('QD_SEAT', raising=False)
    args = cli.build_parser().parse_args(['post', kind, '--to', 'reviewers', 'Synthetic brief'])
    with pytest.raises(shipmod.ShipError, match=f'{kind} is unsupported'):
        cli.cmd_post(args)
    assert post.Store(forum.runtime).all() == []
    assert not (forum.runtime / 'post/groups').exists()


def test_group_cli_and_lead_forward_use_the_same_original_request(forum, monkeypatch):
    monkeypatch.setattr(cli, 'load_ship', lambda args: forum)
    monkeypatch.setattr(cli, 'delivery_note', lambda ship, to: 'synthetic delivery pending')
    monkeypatch.delenv('QD_SEAT', raising=False)
    args = cli.build_parser().parse_args(['post', 'order', '--to', 'reviewers', 'Synthetic brief'])
    assert '4 body-identical copies stored' in cli.cmd_post(args)
    store = post.Store(forum.runtime)
    root, = store.open_for('lead')
    monkeypatch.setenv('QD_SEAT', 'lead')
    args = cli.build_parser().parse_args(['post', 'order', '--to', 'reviewers', '--re', root.id, root.body])
    assert root.id in cli.cmd_post(args)
    assert len(store.all()) == 5


def test_group_body_file_keeps_original_whitespace(forum, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, 'load_ship', lambda args: forum)
    monkeypatch.setattr(cli, 'delivery_note', lambda ship, to: 'synthetic delivery pending')
    monkeypatch.setenv('QD_SEAT', 'lead')
    store = post.Store(forum.runtime)
    root = store.new('order', 'operator', 'lead', '\n  ' + BODY + '\n')
    body_file = tmp_path / 'brief.txt'
    body_file.write_text(root.body)
    args = cli.build_parser().parse_args(['post', 'order', '--to', 'reviewers', '--re', root.id,
                                         '--body-file', str(body_file)])
    cli.cmd_post(args)
    copies = [m for m in store.all() if m.kind == 'order' and m.re == root.id]
    assert len(copies) == 4 and all(m.body == root.body for m in copies)
    # The existing individual-seat command still performs its usual normalization.
    args.to = 'reviewer-1'
    assert cli.read_body(args) == root.body.strip()


def test_group_body_file_preserves_crlf_bytes(forum, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, 'load_ship', lambda args: forum)
    monkeypatch.setattr(cli, 'delivery_note', lambda ship, to: 'synthetic delivery pending')
    monkeypatch.delenv('QD_SEAT', raising=False)
    body_file = tmp_path / 'brief.txt'
    original = b'  Synthetic brief.\r\nSecond line.\r\n'
    body_file.write_bytes(original)
    args = cli.build_parser().parse_args(['post', 'order', '--to', 'reviewers', '--body-file', str(body_file)])
    cli.cmd_post(args)
    assert all(m.body.encode() == original for m in post.Store(forum.runtime).all())


def test_group_stdin_preserves_crlf_before_universal_newline_decoding(forum, monkeypatch):
    monkeypatch.setattr(cli, 'load_ship', lambda args: forum)
    monkeypatch.setattr(cli, 'delivery_note', lambda ship, to: 'synthetic delivery pending')
    monkeypatch.delenv('QD_SEAT', raising=False)
    original = ' \tUnicode α\r\nSecond line.\r\n  '.encode()
    stream = io.TextIOWrapper(io.BytesIO(original), encoding='utf-8', newline=None)
    monkeypatch.setattr(cli.sys, 'stdin', stream)
    args = cli.build_parser().parse_args(['post', 'order', '--to', 'reviewers', '-'])
    cli.cmd_post(args)
    assert all(m.body.encode() == original for m in post.Store(forum.runtime).all())


@pytest.mark.parametrize('raw', [b'  \t\r\n ', b'bad\xfftext'])
def test_invalid_group_stdin_publishes_no_posts(forum, monkeypatch, raw):
    monkeypatch.setattr(cli, 'load_ship', lambda args: forum)
    monkeypatch.delenv('QD_SEAT', raising=False)
    monkeypatch.setattr(cli.sys, 'stdin', io.TextIOWrapper(io.BytesIO(raw), encoding='utf-8'))
    args = cli.build_parser().parse_args(['post', 'order', '--to', 'reviewers', '-'])
    with pytest.raises(shipmod.ShipError):
        cli.cmd_post(args)
    assert post.Store(forum.runtime).all() == []
    assert not (forum.runtime / 'post/groups').exists()
