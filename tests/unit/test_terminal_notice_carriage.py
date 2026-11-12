"""Ordinary synthetic post selection; no native programs or delivery actions."""

from types import SimpleNamespace

import pytest

from quarterdeck import cascade, group_post, post, thread_context
from quarterdeck.ship import Cascade, Group


def delivered(store, *messages):
    attempts = post.Attempts(store.dir.parent.parent)
    for message in messages:
        attempt = attempts.open(message, message.to, '%synthetic', 1234, 'synthetic-old-launch')
        attempts.close(attempt, 'delivered')
    return attempts


def group_notice(tmp_path, *, failed=False, sender='issuer'):
    ship = SimpleNamespace(runtime=tmp_path, groups={
        'reviewers': Group('reviewers', ('one', 'two'), 'lead')})
    store = post.Store(tmp_path)
    root, copies = group_post.submit(ship, store, 'reviewers', sender, 'Review a fictional garden plan.')
    for copy in copies[:1] if failed else copies:
        store.new('report', copy.to, 'lead', 'Synthetic review filed.', re=copy.id)
    group_post.advance(ship, store, lambda member, copy: 'owned pane is dead' if failed else None)
    notice, = [m for m in store.for_seat('lead') if m.sender == post.SHIP and m.kind == 'report']
    return store, root, notice


def cascade_notice(tmp_path, *, stopped=False):
    ship = SimpleNamespace(runtime=tmp_path, operator='operator',
                           cascade=Cascade(('one', 'two', 'three'), 'lead', ('low',)))
    store = post.Store(tmp_path)
    root = store.new('question', 'issuer', 'lead', 'Invent a fictional garden plan.')
    cascade.start(SimpleNamespace(ship=ship, store=store), root.id, 'lead')
    path = cascade.folder(tmp_path, root.id)
    runner = cascade.Runner(ship, store, lambda name, order: None,
                            lambda name, wanted: dict(requested=wanted, actual=wanted,
                                                      detail='Synthetic effort confirmed.'),
                            refusal_check=lambda name, order: None)
    if stopped:
        runner.finish(path, cascade.load(path), 'Synthetic seat unavailable.')
    else:
        for _ in range(3):
            runner.advance(path)
            data = cascade.load(path)
            order = store.get(data['steps'][-1]['order'])
            store.new('report', order.to, post.SHIP,
                      '## Cascade note\nAdd a path.\n## Answer\nA garden with a path.', re=order.id)
            runner.advance(path)
    notice, = [m for m in store.for_seat('lead') if m.sender == post.SHIP]
    return store, root, notice


@pytest.mark.parametrize('sender', ['issuer', post.SHIP])
def test_delivered_recovery_on_claimed_open_order_is_history_even_for_ship_root(tmp_path, sender):
    store = post.Store(tmp_path)
    root = store.new('order', sender, 'lead', 'Build a fictional widget.')
    store.new('ack', 'lead', sender, 'Started.', re=root.id)
    recovery = store.new('report', post.SHIP, 'lead', 'Synthetic continuation.', re=root.id)
    attempts = delivered(store, root, recovery)
    assert thread_context.select(store, 'lead').incoming == [root]
    assert store.is_open(root) and store.get(recovery.id) == recovery
    assert attempts.latest()[recovery.id] == 'delivered'


@pytest.mark.parametrize('failed', [False, True])
def test_real_saved_group_terminal_is_carried_fresh_but_other_same_root_report_is_history(tmp_path, failed):
    store, root, terminal = group_notice(tmp_path, failed=failed, sender=post.SHIP)
    recovery = store.new('report', post.SHIP, 'lead', 'Synthetic continuation.', re=root.id)
    attempts = delivered(store, root, terminal, recovery)
    before = attempts.latest()
    assert thread_context.select(store, 'lead').incoming == [root, terminal]
    assert thread_context.select(store, 'lead', [terminal, terminal]).incoming == [root, terminal]
    assert thread_context.select(store, 'lead', consumed={terminal.id}).incoming == [root]
    assert attempts.latest() == before


@pytest.mark.parametrize('stopped', [False, True])
def test_real_saved_cascade_terminal_is_carried_fresh_but_other_same_root_report_is_history(tmp_path, stopped):
    store, root, terminal = cascade_notice(tmp_path, stopped=stopped)
    recovery = store.new('report', post.SHIP, 'lead', 'Synthetic continuation.', re=root.id)
    delivered(store, root, terminal, recovery)
    assert thread_context.select(store, 'lead').incoming == [root, terminal]
    assert thread_context.select(store, 'lead', consumed={root.id, terminal.id}).incoming == [root]
    store.new('answer', 'lead', 'issuer', 'Concluded.', re=root.id)
    assert thread_context.select(store, 'lead').incoming == []


@pytest.mark.parametrize('queued', [False, True])
def test_nonterminal_ship_report_still_arrives_once_through_unread_or_queued_extra(tmp_path, queued):
    store, root, terminal = group_notice(tmp_path)
    recovery = store.new('report', post.SHIP, 'lead', 'Synthetic continuation.', re=root.id)
    if queued:
        attempt = post.Attempts(tmp_path).open(recovery, 'lead', '%synthetic', 1234, 'synthetic-launch')
        post.Attempts(tmp_path).close(attempt, 'queued')
    assert thread_context.select(store, 'lead', [recovery, recovery]).incoming == [root, terminal, recovery]
    assert thread_context.select(store, 'lead', [recovery], consumed={recovery.id}).incoming == [root, terminal]


def test_running_cascade_report_is_history_and_requester_and_nested_root_rules_still_apply(tmp_path):
    store = post.Store(tmp_path)
    ship = SimpleNamespace(runtime=tmp_path, operator='operator',
                           cascade=Cascade(('one', 'two', 'three'), 'lead'))
    root = store.new('question', 'issuer', 'lead', 'Plan a fictional garden.')
    cascade.start(SimpleNamespace(ship=ship, store=store), root.id, 'lead')
    recovery = store.new('report', post.SHIP, 'lead', 'Synthetic continuation.', re=root.id)
    amendment = store.new('report', 'issuer', 'lead', 'Include a path.', re=root.id)
    nested = store.new('order', 'another', 'lead', 'Measure the path.', re=root.id)
    nested_amendment = store.new('answer', 'another', 'lead', 'Use metres.', re=nested.id)
    delivered(store, root, recovery, amendment, nested, nested_amendment)
    assert store.is_open(root)
    assert thread_context.select(store, 'lead').incoming == [root, amendment, nested, nested_amendment]


def test_excluded_terminal_stays_absent_without_closing_group_root(tmp_path):
    store, root, terminal = group_notice(tmp_path)
    assert thread_context.select(store, 'lead', [terminal], excluded={terminal.id}).incoming == [root]
    assert store.is_open(root)
