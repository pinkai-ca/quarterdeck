"""The door types a message with no line ending in a space; the stored file keeps them, and qd thread prints it whole.

No program's box can show whether a line ends in a space, and the door's comparison refuses such a payload rather
than guess (harness.types._box_matches): on a composer that paints its padding, Grok Build's and OpenCode's (both
measured), a message with one never matched. Synthetic messages and frames; no program runs.
"""
from datetime import timedelta
import json
from types import SimpleNamespace

from quarterdeck import brief_change, pending, post, post_threads
from quarterdeck.door import Door
from quarterdeck.harness import get
from quarterdeck.post_message import _typed
from tests.contract.test_padded_box import capture
from .test_pending_delivery import input_row, send, setup  # noqa: F401 -- setup is a fixture

# The four kinds the crew's messages hold: a line of spaces only, text then a space, a line of a fenced code block,
# and a markdown hard break (two spaces before a non-empty line).
BODY = ('Synthetic first line, then one space \n'
        '    \n'
        '```\n'
        'synthetic code with an end space \n'
        '```\n'
        'A synthetic hard break  \n'
        'its next line\n'
        'Synthetic last line')


def message(store, **fields):
    return store.new('report', 'synthetic-sender', 'synthetic-reader', BODY, subject='A synthetic subject ', **fields)


def test_each_kind_types_without_end_spaces_and_the_file_keeps_them(tmp_path):
    store = post.Store(tmp_path / 'runtime')
    sent = message(store)
    for text in (post.typed([sent]), post.typed([sent], second=(sent.id,))):
        lines = text.split('\n')
        assert not [line for line in lines if line.endswith(' ')], 'no typed line ends in a space'
        assert lines[-3:] == ['its next line', 'Synthetic last line', '']
        assert '' in lines and 'synthetic code with an end space' in lines and 'A synthetic hard break' in lines
        assert any(line.endswith('· A synthetic subject') for line in lines), 'the header too'
    assert store.get(sent.id).body == BODY, 'the stored file keeps every space'
    printed = post_threads.render(post_threads.whole(store, sent.id, 'synthetic-reader'))
    assert 'Synthetic first line, then one space \n    \n' in printed and 'A synthetic hard break  \n' in printed, \
        'qd thread prints the file whole'


def test_the_old_typed_forms_stay_among_the_delivered_forms(tmp_path):
    sent = message(post.Store(tmp_path / 'runtime'))
    forms = post.delivered_forms(sent)
    assert forms[:2] == (post.typed([sent]), post.typed([sent], second=(sent.id,)))
    old = _typed([sent], ends=False)
    assert old in forms and _typed([sent], second=(sent.id,), ends=False) in forms
    assert any(line.endswith(' ') for line in old.split('\n')), 'the form typed before: its end spaces kept'
    assert len(forms) == len(set(forms))


def grok_frame(text, width=100):
    """Grok Build's composer as tests/fixtures/grok/1.0.77/draft-multiline.json draws it, painted to its border; a
    paste's final LF is its one blank row below, as Grok Build paints it."""
    inner, tail = width - 6, ' Grok 4.7 (high) · always-approve ─╯'
    lines = text.split('\n')
    return '\n'.join(['  /synthetic/project', '', '  ╭' + '─' * inner + '╮',
                      *('  │' + (prefix + line).ljust(inner) + '│'
                        for prefix, line in zip([' ❯ '] + ['   '] * len(lines), lines)),
                      '  ╰' + '─' * (width - 5 - len(tail)) + tail, '',
                      '  Enter:send  │  Alt+Enter:newline  │  Shift+Tab:mode  │  Ctrl+x:shortcuts'])


def padded(program, text):
    if program == 'grok':
        return grok_frame(text)
    return capture('opencode', text.rstrip('\n'), padding='full', width=100)  # every row painted to the pane's width


def door(program, frame, monkeypatch):
    monkeypatch.setattr(Door, 'wall', lambda self, screen: None)
    seat = SimpleNamespace(harness=get(program), session=SimpleNamespace(human=False), spec=SimpleNamespace(),
                           pane=lambda: SimpleNamespace(id='%1', width=100, dead=False),
                           tmux=SimpleNamespace(capture=lambda *a, **k: frame), home=None)
    return Door(seat)


def test_a_padded_composer_confirms_such_a_message_as_typed_and_never_its_old_form(tmp_path, monkeypatch):
    sent = message(post.Store(tmp_path / 'runtime'))
    new, old = post.typed([sent]), _typed([sent], ends=False)
    for program in ('grok', 'opencode'):
        harness = get(program)
        reading = door(program, padded(program, new), monkeypatch)
        assert reading.matches(harness.box(new), reading.screen()), f'{program}: typed without end spaces, confirmed'
        reading = door(program, padded(program, old), monkeypatch)
        assert not reading.matches(harness.box(old), reading.screen()), f'{program}: the old form never could be'


def test_a_delivery_typed_in_the_old_form_is_still_found_by_the_one_look(setup, monkeypatch):
    svc, seat, live, record = setup
    sent, proof = send(svc, seat, body=BODY)
    assert not [line for line in live['box'].split('\n') if line.endswith(' ')], 'typed without end spaces'
    later = post.now() + timedelta(seconds=pending.WAIT_SECONDS + 5)
    monkeypatch.setattr(post, 'now', lambda: later)
    input_row(record, _typed([sent], ends=False), later)  # the record of a delivery typed before the change
    assert pending.one_look(svc, seat) == [f'{sent.short} to worker: delivered (the record shows it)']


def test_a_standing_orders_head_is_typed_without_end_spaces(setup, monkeypatch):
    svc, seat, live, record = setup
    head = (f'{brief_change.BRIEF_HEAD}; what changed, then your message (qd brief worker prints the whole):\n'
            '@@ -1 +1 @@\n-A synthetic rule \n+A synthetic rule, changed \n' + brief_change.BRIEF_HEAD_END + '\n')
    monkeypatch.setattr(brief_change, 'head', lambda runtime, name: (head, 'synthetic-brief'))
    monkeypatch.setattr(brief_change, 'mark', lambda *args: None)
    send(svc, seat, body='Synthetic body')
    assert live['box'].startswith(brief_change.BRIEF_HEAD) and '+A synthetic rule, changed\n' in live['box']
    assert not [line for line in live['box'].split('\n') if line.endswith(' ')]
