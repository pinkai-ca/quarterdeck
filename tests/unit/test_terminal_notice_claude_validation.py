"""Claude validation of the terminal-notice carriage: structure terminal notices are carried by metadata alone.

Wholly synthetic posts and journals. No native program, provider or delivery
action runs here; attempts are recorded only to stand for earlier deliveries.
The fresh-start cases go through the seat's own launch carriage.
"""

import json
import os
from types import SimpleNamespace

import pytest

from quarterdeck import cascade, cascade_runtime, group_post, post, thread_context
from quarterdeck.harness.opencode_thinking import Client as OpenCodeThinking
from quarterdeck.harness.pi_thinking import Client as PiThinking
from quarterdeck.ship import Cascade, Group
from .test_pending_delivery import setup  # noqa: F401
from .thread_reading import row_for, thread_text
from quarterdeck import post_threads

CONTINUATIONS = pytest.mark.parametrize(
    'continuation', [PiThinking.continuation, OpenCodeThinking.continuation], ids=['pi', 'opencode'])
STRUCTURES = pytest.mark.parametrize('structure', ['group', 'cascade'])


# -- synthetic structures -----------------------------------------------------------------


def recovery(store, seat, text):
    """Post a continuation exactly as the thinking recovery does: a ship report re the claimed order."""
    at = post.now()
    notice = post.Message(store._new_id('report', post.SHIP, at), 'report', post.SHIP, seat, at, text,
                          re=store.claimed(seat, at))
    store.write(notice)
    return notice


def delivered(runtime, seat, *messages):
    attempts = post.Attempts(runtime)
    for message in messages:
        attempts.close(attempts.open(message, seat, '%synthetic', 1234, 'synthetic-old-launch'), 'delivered')


def deliver_all(runtime, store, seat, *, unread=()):
    """Stand for earlier deliveries of everything addressed to the seat but what stays unread."""
    delivered(runtime, seat, *(m for m in store.for_seat(seat) if m.kind != 'ack' and m.id not in unread))


def start_group(runtime, store, lead):
    ship = SimpleNamespace(runtime=runtime, groups={'reviewers': Group('reviewers', ('one', 'two'), lead)})
    root, copies = group_post.submit(ship, store, 'reviewers', 'issuer', 'Review a fictional garden plan.')
    return ship, root, copies


def finish_group(ship, store, copies, lead, *, failed=False):
    for copy in copies[:1] if failed else copies:
        store.new('report', copy.to, lead, 'Synthetic review filed.', re=copy.id)
    group_post.advance(ship, store, lambda member, copy: 'owned pane is dead' if failed else None)
    root_id = copies[0].re
    saved = json.loads((runtime_of(store) / 'post' / 'groups' / f'{root_id}.json').read_text(encoding='utf-8'))
    notice = store.get(saved['notice']['id'])
    return notice


def runtime_of(store):
    return store.dir.parent.parent


def start_cascade(runtime, store, orchestrator):
    ship = SimpleNamespace(runtime=runtime, operator='operator',
                           cascade=Cascade(('one', 'two', 'three'), orchestrator, ('low',)))
    root = store.new('question', 'issuer', orchestrator, 'Invent a fictional garden plan.')
    cascade.start(SimpleNamespace(ship=ship, store=store), root.id, orchestrator)
    return ship, root


def finish_cascade(ship, store, root, *, stopped=False):
    path = cascade.folder(runtime_of(store), root.id)
    runner = cascade.Runner(ship, store, lambda name, order: None,
                            lambda name, wanted: dict(requested=wanted, actual=wanted,
                                                      detail='Synthetic effort confirmed.'),
                            refusal_check=lambda name, order: None)
    if stopped:
        runner.finish(path, cascade.load(path), 'Synthetic seat unavailable.')
    else:
        for _ in range(3):
            runner.advance(path)
            order = store.get(cascade.load(path)['steps'][-1]['order'])
            store.new('report', order.to, post.SHIP,
                      '## Cascade note\nAdd a path.\n## Answer\nA garden with a path.', re=order.id)
            runner.advance(path)
    return store.get(cascade.load(path)['intents']['final']['id'])


def structure_with_recovery(runtime, store, seat, structure, continuation, *, finished=True, variant=False):
    """A claimed structure root, a recovery continuation on it, then (optionally) its terminal notice."""
    if structure == 'group':
        ship, root, copies = start_group(runtime, store, seat)
    else:
        ship, root = start_cascade(runtime, store, seat)
    store.new('ack', seat, 'issuer', 'Started.', re=root.id)
    continued = recovery(store, seat, continuation)
    assert continued.re == root.id, 'the recovery continuation lands directly on the claimed structure root'
    terminal = None
    if finished:
        terminal = (finish_group(ship, store, copies, seat, failed=variant) if structure == 'group'
                    else finish_cascade(ship, store, root, stopped=variant))
        assert (terminal.sender, terminal.kind, terminal.to, terminal.re) == (post.SHIP, 'report', seat, root.id)
    return root, continued, terminal


def journal(runtime, structure, root):
    if structure == 'group':
        return runtime / 'post' / 'groups' / f'{root.id}.json'
    return runtime / 'ring' / root.id / 'state.json'


def snapshot(runtime):
    found = {}
    for path in sorted(runtime.rglob('*')):
        key = str(path.relative_to(runtime))
        if path.is_symlink():
            found[key] = ('link', os.readlink(path))
        elif path.is_file():
            found[key] = ('file', path.read_bytes(), os.stat(path).st_nlink)
        else:
            found[key] = ('dir',)
    return found


def rewrite(path, data):
    """Replace a journal in place, as an ordinary regular single-link file."""
    path.unlink()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


# -- fresh starts through the seat's own carriage ------------------------------------------


@STRUCTURES
@CONTINUATIONS
@pytest.mark.parametrize('variant', [False, True], ids=['complete', 'failed-or-stopped'])
def test_fresh_start_carries_terminal_once_and_delivered_recovery_continuation_never(
        setup, structure, continuation, variant):
    svc, seat, live, record = setup
    runtime = svc.ship.runtime
    root, continued, terminal = structure_with_recovery(runtime, svc.store, seat.name, structure, continuation,
                                                        variant=variant)
    deliver_all(runtime, svc.store, seat.name)
    before = post.Attempts(runtime).latest()
    text, incoming = seat.first_message()
    assert incoming == [root]
    assert row_for(text, root.id)['amendments'].count(terminal.id) == 1
    assert terminal.body.rstrip() not in text and root.body not in text, 'the index carries ids, not bodies'
    assert continuation not in text, 'a fresh context never receives a continuation of a conversation it lacks'
    whole = thread_text(svc.store, root.id, seat.name)
    assert whole.count(terminal.body.rstrip()) == 1 and root.body in whole  # a lead also sent its members' copies
    assert whole.count(continuation.rstrip()) == 1, 'every message stays readable'
    assert whole.index(post_threads.CONTINUATION_LABEL) < whole.index(continuation.rstrip()), \
        'a delivered continuation is labelled as not addressed to this conversation'
    assert svc.store.is_open(root) and svc.store.get(continued.id) == continued
    assert post.Attempts(runtime).latest() == before, 'selection never relabels delivery'


@STRUCTURES
@CONTINUATIONS
def test_fresh_start_on_running_structure_carries_root_alone(setup, structure, continuation):
    svc, seat, live, record = setup
    runtime = svc.ship.runtime
    root, continued, _ = structure_with_recovery(runtime, svc.store, seat.name, structure, continuation,
                                                 finished=False)
    deliver_all(runtime, svc.store, seat.name)
    text, incoming = seat.first_message()
    assert incoming == [root] and continuation not in text


@STRUCTURES
@CONTINUATIONS
def test_unread_recovery_continuation_still_arrives_once_at_fresh_start(setup, structure, continuation):
    svc, seat, live, record = setup
    runtime = svc.ship.runtime
    root, continued, terminal = structure_with_recovery(runtime, svc.store, seat.name, structure, continuation)
    deliver_all(runtime, svc.store, seat.name, unread={continued.id})  # the continuation never reached the seat
    text, incoming = seat.first_message()
    assert incoming == [root, continued]
    row = row_for(text, root.id)
    assert row['amendments'].count(terminal.id) == 1 and row['amendments'].count(continued.id) == 1
    whole = thread_text(svc.store, root.id, seat.name)
    assert whole.count(continuation.rstrip()) == 1 and whole.count(terminal.body.rstrip()) == 1
    assert post_threads.CONTINUATION_LABEL not in whole, 'an unread continuation is no earlier conversation\'s'


@STRUCTURES
@pytest.mark.parametrize('kind', ['unread', 'queued'])
def test_nonterminal_ship_report_arrives_once_through_extras_for_both_structures(tmp_path, structure, kind):
    store = post.Store(tmp_path)
    root, continued, terminal = structure_with_recovery(tmp_path, store, 'lead', structure,
                                                        PiThinking.continuation)
    if kind == 'queued':
        attempts = post.Attempts(tmp_path)
        attempts.close(attempts.open(continued, 'lead', '%synthetic', 1234, 'synthetic-launch'), 'queued')
    selected = thread_context.select(store, 'lead', [continued, continued])
    assert selected.incoming == [root, terminal, continued]
    assert thread_context.select(store, 'lead', [continued], consumed={continued.id}).incoming == [root, terminal]


# -- body independence ---------------------------------------------------------------------


class BodyBlind:
    """A message whose body cannot be read: the exception must decide without it."""

    def __init__(self, message):
        self._message = message

    def __getattr__(self, name):
        if name == 'body':
            raise AssertionError('terminal-notice selection read a message body')
        return getattr(self._message, name)


@STRUCTURES
def test_terminal_decision_never_reads_a_message_body(tmp_path, structure):
    store = post.Store(tmp_path)
    root, continued, terminal = structure_with_recovery(tmp_path, store, 'lead', structure,
                                                        PiThinking.continuation)
    blind = {m.id: BodyBlind(m) for m in store.all()}
    decide = thread_context._terminal_notice
    assert decide(store, blind[terminal.id], blind[root.id], 'lead', blind) is True
    assert decide(store, blind[continued.id], blind[root.id], 'lead', blind) is False


@STRUCTURES
def test_selection_reads_only_its_journal_and_never_calls_full_validators(tmp_path, structure, monkeypatch):
    store = post.Store(tmp_path)
    root, continued, terminal = structure_with_recovery(tmp_path, store, 'lead', structure,
                                                        PiThinking.continuation)

    def forbidden(*args, **kwargs):
        raise AssertionError('a full body-comparing journal validator was called')

    for target, name in ((group_post, '_load'), (cascade, 'load'), (cascade.Runner, 'verify'),
                         (cascade.Runner, 'advance'), (cascade.Runner, 'finish'), (group_post, 'advance')):
        monkeypatch.setattr(target, name, forbidden)
    read = []
    original = cascade_runtime.read_file
    monkeypatch.setattr(cascade_runtime, 'read_file',
                        lambda path, **kwargs: read.append((path, kwargs)) or original(path, **kwargs))
    before = snapshot(tmp_path)
    assert thread_context.select(store, 'lead').incoming == [root, terminal]
    assert read and {path for path, _ in read} == {journal(tmp_path, structure, root)}
    assert all(type(kwargs.get('max_bytes')) is int and kwargs['max_bytes'] > 0 for _, kwargs in read), \
        'every journal read is bounded'
    assert snapshot(tmp_path) == before


@STRUCTURES
def test_journal_bodies_are_ignored_and_body_lookalikes_are_history(tmp_path, structure):
    store = post.Store(tmp_path)
    root, continued, terminal = structure_with_recovery(tmp_path, store, 'lead', structure,
                                                        PiThinking.continuation)
    path = journal(tmp_path, structure, root)
    data = json.loads(path.read_text(encoding='utf-8'))
    if structure == 'group':
        data['notice']['body'] = 'Unrelated saved text.'
        data['root']['body'] = 'Another saved text.'
        for copy in data['copies']:
            copy['body'] = 'Yet another saved text.'
    else:
        data['intents']['final']['body'] = 'Unrelated saved text.'
    rewrite(path, data)
    if structure == 'group':
        with pytest.raises(group_post.ShipError):
            group_post._load(path)  # the full validator compares bodies; selection must not
    # A ship report byte-identical in body to the terminal, on the same root, is not the terminal.
    lookalike = store.new('report', post.SHIP, 'lead', terminal.body, re=root.id)
    delivered(tmp_path, 'lead', root, continued, terminal, lookalike)
    before = snapshot(tmp_path)
    assert thread_context.select(store, 'lead').incoming == [root, terminal]
    assert snapshot(tmp_path) == before


# -- the consistency boundary --------------------------------------------------------------


GROUP_MUTATIONS = {
    'notice-null': lambda d, ids: d.update(notice=None),
    'notice-other-id': lambda d, ids: d['notice'].update(id=ids['continued']),
    'notice-other-to': lambda d, ids: d['notice'].update(to='another'),
    'notice-other-re': lambda d, ids: d['notice'].update(re=ids['continued']),
    'notice-other-subject': lambda d, ids: d['notice'].update(subject='Other'),
    'notice-other-sender': lambda d, ids: d['notice'].update({'from': 'issuer'}),
    'notice-missing-at': lambda d, ids: d['notice'].pop('at'),
    'notice-not-object': lambda d, ids: d.update(notice=ids['terminal']),
    'lead-other': lambda d, ids: d.update(lead='another'),
    'group-empty': lambda d, ids: d.update(group=''),
    'group-not-text': lambda d, ids: d.update(group=7),
    'root-other-id': lambda d, ids: d['root'].update(id=ids['continued']),
    'root-other-due': lambda d, ids: d['root'].update(due='2099-01-01'),
    'format-2': lambda d, ids: d.update(format=2),
    'format-true': lambda d, ids: d.update(format=True),
    'format-text': lambda d, ids: d.update(format='1'),
    'format-missing': lambda d, ids: d.pop('format'),
    'copies-empty': lambda d, ids: d.update(copies=[]),
    'copies-not-list': lambda d, ids: d.update(copies={}),
    'copy-unpublished': lambda d, ids: d['copies'][0].update(id='ord-19990101-0000-lead-0000'),
    'copy-duplicated': lambda d, ids: d['copies'].append(dict(d['copies'][0])),
    'copy-is-root': lambda d, ids: d['copies'].append(dict(d['root'])),
    'copy-is-notice': lambda d, ids: d['copies'].append(dict(d['notice'])),
    'copy-other-to': lambda d, ids: d['copies'][0].update(to='another'),
    'copy-id-unhashable': lambda d, ids: d['copies'][0].update(id=['x']),
    'failed-unknown-reason': lambda d, ids: d.update(failed={d['copies'][0]['id']: dict(
        member=d['copies'][0]['to'], reason='invented')}),
    'failed-unhashable-reason': lambda d, ids: d.update(failed={d['copies'][0]['id']: dict(
        member=d['copies'][0]['to'], reason=['owned pane is dead'])}),
    'failed-unknown-identity': lambda d, ids: d.update(failed={ids['continued']: dict(
        member='one', reason='owned pane is dead')}),
    'failed-other-member': lambda d, ids: d.update(failed={d['copies'][0]['id']: dict(
        member=d['copies'][1]['to'], reason='owned pane is dead')}),
    'failed-extra-key': lambda d, ids: d.update(failed={d['copies'][0]['id']: dict(
        member=d['copies'][0]['to'], reason='owned pane is dead', note='x')}),
    'failed-not-object': lambda d, ids: d.update(failed=[]),
    'top-level-list': lambda d, ids: [d.clear()],
}

CASCADE_MUTATIONS = {
    'status-running': lambda d, ids: d.update(status='running'),
    'status-other': lambda d, ids: d.update(status='finished'),
    'status-missing': lambda d, ids: d.pop('status'),
    'question-other': lambda d, ids: d.update(question=ids['continued']),
    'orchestrator-other': lambda d, ids: d['declaration'].update(orchestrator='another'),
    'seats-include-orchestrator': lambda d, ids: d['declaration'].update(seats=['one', 'two', 'lead']),
    'seats-duplicate': lambda d, ids: d['declaration'].update(seats=['one', 'one', 'two']),
    'seats-two': lambda d, ids: d['declaration'].update(seats=['one', 'two']),
    'seats-empty-name': lambda d, ids: d['declaration'].update(seats=['one', 'two', '']),
    'seats-not-list': lambda d, ids: d['declaration'].update(seats='one,two,three'),
    'declaration-not-object': lambda d, ids: d.update(declaration=[]),
    'final-missing': lambda d, ids: d['intents'].pop('final'),
    'final-other-id': lambda d, ids: d['intents']['final'].update(id=ids['continued']),
    'final-other-to': lambda d, ids: d['intents']['final'].update(to='another'),
    'final-other-re': lambda d, ids: d['intents']['final'].update(re=ids['continued']),
    'final-other-kind': lambda d, ids: d['intents']['final'].update(kind='answer'),
    'final-not-object': lambda d, ids: d['intents'].update(final=ids['terminal']),
    'intents-not-object': lambda d, ids: d.update(intents=[]),
    'format-2': lambda d, ids: d.update(format=2),
    'format-true': lambda d, ids: d.update(format=True),
    'top-level-list': lambda d, ids: [d.clear()],
}


def _mutated(tmp_path, structure, mutate):
    store = post.Store(tmp_path)
    root, continued, terminal = structure_with_recovery(tmp_path, store, 'lead', structure,
                                                        PiThinking.continuation)
    path = journal(tmp_path, structure, root)
    data = json.loads(path.read_text(encoding='utf-8'))
    mutate(data, dict(continued=continued.id, terminal=terminal.id))
    rewrite(path, data if data else [])
    delivered(tmp_path, 'lead', root, continued, terminal)
    return store, root


@pytest.mark.parametrize('name', sorted(GROUP_MUTATIONS))
def test_inconsistent_group_metadata_grants_no_exception_and_repairs_nothing(tmp_path, name):
    store, root = _mutated(tmp_path, 'group', GROUP_MUTATIONS[name])
    before = snapshot(tmp_path)
    assert thread_context.select(store, 'lead').incoming == [root]
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('name', sorted(CASCADE_MUTATIONS))
def test_inconsistent_cascade_metadata_grants_no_exception_and_repairs_nothing(tmp_path, name):
    store, root = _mutated(tmp_path, 'cascade', CASCADE_MUTATIONS[name])
    before = snapshot(tmp_path)
    assert thread_context.select(store, 'lead').incoming == [root]
    assert snapshot(tmp_path) == before


def _raw_empty(path):
    path.write_bytes(b'')


def _raw_truncated(path):
    raw = path.read_bytes()
    path.unlink()
    path.write_bytes(raw[:len(raw) // 2])


def _raw_not_utf8(path):
    path.unlink()
    path.write_bytes(b'{"format": 1, "group": "\xff"}')


def _raw_symlink(path):
    copy = path.with_name(path.name + '.elsewhere')
    os.rename(path, copy)
    path.symlink_to(copy.name)


def _raw_hard_link(path):
    os.link(path, path.with_name(path.name + '.second'))


def _raw_directory(path):
    path.unlink()
    path.mkdir()


def _raw_parent_link(path):
    folder = path.parent
    moved = folder.with_name(folder.name + '-elsewhere')
    os.rename(folder, moved)
    folder.symlink_to(moved.name)


def _raw_competing(path, root, runtime, structure):
    other = journal(runtime, 'cascade' if structure == 'group' else 'group', root)
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text('{}\n')


RAW = {'empty': _raw_empty, 'truncated': _raw_truncated, 'not-utf8': _raw_not_utf8,
       'symlink': _raw_symlink, 'hard-link': _raw_hard_link, 'directory': _raw_directory,
       'parent-link': _raw_parent_link, 'competing-structure': None}


@STRUCTURES
@pytest.mark.parametrize('name', sorted(RAW))
def test_unreadable_or_competing_journal_grants_no_exception_and_repairs_nothing(tmp_path, structure, name):
    store = post.Store(tmp_path)
    root, continued, terminal = structure_with_recovery(tmp_path, store, 'lead', structure,
                                                        PiThinking.continuation)
    delivered(tmp_path, 'lead', root, continued, terminal)
    path = journal(tmp_path, structure, root)
    if name == 'competing-structure':
        _raw_competing(path, root, tmp_path, structure)
    else:
        RAW[name](path)
    before = snapshot(tmp_path)
    assert thread_context.select(store, 'lead').incoming == [root]
    assert snapshot(tmp_path) == before


@STRUCTURES
def test_permission_denied_journal_grants_no_exception(tmp_path, structure):
    if os.geteuid() == 0:
        pytest.skip('root reads a mode-000 file; the ordinary seat user cannot')
    store = post.Store(tmp_path)
    root, continued, terminal = structure_with_recovery(tmp_path, store, 'lead', structure,
                                                        PiThinking.continuation)
    delivered(tmp_path, 'lead', root, continued, terminal)
    path = journal(tmp_path, structure, root)
    path.chmod(0)
    try:
        assert thread_context.select(store, 'lead').incoming == [root]
    finally:
        path.chmod(0o600)


@STRUCTURES
def test_terminal_is_carried_only_to_its_own_seat_and_root(tmp_path, structure):
    store = post.Store(tmp_path)
    root, continued, terminal = structure_with_recovery(tmp_path, store, 'lead', structure,
                                                        PiThinking.continuation)
    blind = {m.id: BodyBlind(m) for m in store.all()}
    decide = thread_context._terminal_notice
    assert decide(store, blind[terminal.id], blind[root.id], 'another', blind) is False
    other_root = store.new(root.kind, 'issuer', 'lead', 'A different fictional request.')
    blind[other_root.id] = BodyBlind(other_root)
    assert decide(store, blind[terminal.id], blind[other_root.id], 'lead', blind) is False
