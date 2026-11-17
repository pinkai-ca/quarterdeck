"""A yes written by hand is named as one in the desk's notice of a raise.

A yes through the gate records its terminal, and the desk's order names it. A yes written by hand, as a seat writing
the answer record itself would leave it, has no terminal: the desk's order says so outright, the one sign of such a
yes, so the desk need not work it out. A throwaway project and office under tmp_path.
"""
import json
from types import SimpleNamespace

from quarterdeck import post, raise_notice, raise_ship
from tests.unit.test_raise_ship import args, project  # noqa: F401  (the throwaway office, by name)

HANDWRITTEN = 'no terminal recorded: this yes did not pass the terminal check'


def orders(office):
    return [m for m in post.Store(office.runtime).all() if m.subject == raise_notice.SUBJECT]


def test_a_yes_through_the_gate_still_names_its_terminal(project):
    root, office, calls = project
    raise_ship.run(args(), interactive=True, read=lambda _: 'yes', write=lambda _: None)
    order, = orders(office)
    assert 'on a yes from /dev/pts/synthetic' in order.body and HANDWRITTEN not in order.body


def test_a_handwritten_yes_is_named_as_one(project, tmp_path):
    root, office, calls = project
    answer = tmp_path / 'handwritten.answer.json'
    answer.write_text(json.dumps({'format': 1, 'answer': 'yes'}))  # no 'terminal', as a hand would leave it
    draft = SimpleNamespace(name='mission', folder=root / 'ships' / 'mission')
    raise_ship.tell_desk(office, draft, answer)
    order, = orders(office)
    assert f'on a yes with {HANDWRITTEN} (answer record {answer})' in order.body
    assert 'a terminal not recorded' not in order.body
