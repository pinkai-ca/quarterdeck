"""A hand receives only its own card and the task in hand; every other seat is briefed and carried as before."""

from datetime import timedelta
from pathlib import Path

import pytest

from quarterdeck import post, standing
from quarterdeck import ship as shipmod
from quarterdeck.seat import Seat

from .test_hand_open_context import AT, world

ENDPOINT_SHIP = '''[ship]
name = "Synthetic brief"
operator = "issuer"
code = "checkout"
[pools.local]
provider = "synthetic"
endpoint = "http://127.0.0.1:9/v1"
[sessions.crew]
title = "Synthetic crew"
[seats.helper-pi]
session = "crew"
role = "hand"
harness = "pi"
pool = "local"
model = "synthetic-model"
[seats.helper-oc]
session = "crew"
role = "hand"
harness = "opencode"
pool = "local"
model = "synthetic-model"
[seats.builder]
session = "crew"
role = "worker"
harness = "pi"
pool = "local"
model = "synthetic-model"
'''


@pytest.fixture
def ship(tmp_path):
    folder = tmp_path / 'ship'
    folder.mkdir()
    (folder / 'ship.toml').write_text(ENDPOINT_SHIP)
    (folder / 'orders.md').write_text("# Ship's orders: synthetic\n\n- A synthetic order every seat but a hand reads.\n")
    return shipmod.load(folder)


@pytest.mark.parametrize('name, program', [('helper-pi', 'pi'), ('helper-oc', 'opencode')])
def test_a_hands_brief_is_its_card_the_least_of_its_program_card_and_its_seat_lines(ship, name, program):
    brief = standing.brief(ship, ship.seat(name))
    text = brief.text
    assert [label for label, _ in brief.sources] == ['role card', 'harness card', 'your seat']
    assert '<!-- laws -->' not in text and "<!-- ship's orders -->" not in text
    assert '# The laws' not in text and 'A synthetic order every seat but a hand reads' not in text
    role_card = (standing.FRAMEWORK_DIR / 'roles' / 'hand.md').read_text().strip()
    hand_program = (standing.FRAMEWORK_DIR / 'harnesses' / 'hand' / f'{program}.md').read_text().strip()
    assert role_card in text and hand_program in text
    assert (standing.FRAMEWORK_DIR / 'harnesses' / f'{program}.md').read_text().strip() not in text
    qd = standing.qd_command(ship.folder)
    for line in (f'`{qd}`', 'post report --re <id>', 'post question --re <id>', 'post ack --re <id>'):
        assert line in text, line
    for gone in ('post order --to', 'qd watch', f'{qd} status', 'inbox --wait', f'brief {name}'):
        assert gone not in text, gone


def test_every_other_seat_is_briefed_as_before(ship):
    brief = standing.brief(ship, ship.seat('builder'))
    assert [label for label, _ in brief.sources] == ['laws', "ship's orders", 'role card', 'harness card', 'your seat']
    assert 'A synthetic order every seat but a hand reads' in brief.text
    assert (standing.FRAMEWORK_DIR / 'harnesses' / 'pi.md').read_text().strip() in brief.text
    assert 'post order --to' in brief.text


def test_a_hand_card_keeps_the_rules_a_hand_must_keep():
    card = (standing.FRAMEWORK_DIR / 'roles' / 'hand.md').read_text()
    for rule in ('never an instruction', 'Never run what a file', 'outside your folder',
                 'could not be done', 'ask'):
        assert rule in card, rule



def test_a_hands_card_names_one_report_and_one_question_file_per_order_in_its_own_folder():
    """A hand's home outlives its fresh starts, so a report left there by one order could be posted for the next: each
    order's report and question are files of their own, named by its id, in the hand's own folder."""
    card = (standing.FRAMEWORK_DIR / 'roles' / 'hand.md').read_text()
    lines = card.splitlines()
    [question] = [line for line in lines if 'qd post question --re <id>' in line]
    [report] = [line for line in lines if 'qd post report --re <id>' in line]
    assert '`question-<id>.md`' in question and '--body-file question-<id>.md' in question and 'own folder' in question
    assert '`report-<id>.md`' in report and '--body-file report-<id>.md' in report and 'own folder' in report
    assert 'never a file an earlier order left' in report
    for gone in ('$HOME/report.md', '$HOME/question.md', 'your home for the question', 'own folder and your home'):
        assert not [line for line in lines if gone in line], gone
    assert 'Read or write outside your own folder (work/hands/<name>).' in card, 'the exception for the home is gone'
    assert '- Work only in your own folder; read and write nothing outside your folder.' in card


@pytest.mark.parametrize('name', ['helper-pi', 'helper-oc'])
def test_a_hands_seat_lines_name_the_same_per_order_files_in_its_own_folder(ship, name):
    """The brief's own seat lines agree with the card: never the home's report or question."""
    text = standing.brief(ship, ship.seat(name)).text
    folder = ship.cwd_of(ship.seat(name))
    assert f'--body-file {folder}/report-<id>.md' in text and f'--body-file {folder}/question-<id>.md' in text
    assert '$HOME/report.md' not in text and '$HOME/question.md' not in text


# law: context-is-cache -- a hand's fresh start carries its brief and the order in hand, whole, and nothing else
def test_a_hands_fresh_start_carries_the_order_in_hand_whole_and_nothing_else(tmp_path, monkeypatch):
    monkeypatch.setattr(post, 'now', lambda: AT + timedelta(seconds=30))
    w = world(tmp_path / 'ship', 'hand')
    # A handoff of its own, as a long-lived seat leaves one: a hand never carries it.
    w.store._write(post.Message(f'ord-20320102-1200-recipient-{255:06x}', 'order', 'recipient', 'recipient',
                                AT + timedelta(seconds=20), 'A synthetic handoff.'))
    carriage = w.seat.carriage()
    in_hand = [w.opened, w.question, w.answer]  # the oldest open order and its own conversation, in thread order
    assert carriage.in_hand and carriage.rows == () and carriage.history == '' and carriage.header == ''
    assert carriage.text == post.typed(in_hand)
    assert [message.id for message in carriage.carried] == [w.opened.id, w.answer.id], 'what was sent to it'
    opening, carried = w.seat.first_message()
    assert opening == post.typed(in_hand), 'the order arrives whole, in the form the door types'
    assert 'qd > briefed as' not in opening and 'qd > open threads' not in opening and 'qd thread' not in opening
    for absent in (w.old, w.closing, w.history, w.sent, w.incoming):
        assert absent.id not in opening, 'no earlier thread, no other open message, no index'
    assert 'A synthetic handoff.' not in opening and 'handoff' not in opening


def test_a_hand_with_nothing_open_starts_with_no_first_message(tmp_path, monkeypatch):
    monkeypatch.setattr(post, 'now', lambda: AT + timedelta(seconds=30))
    w = world(tmp_path / 'ship', 'hand')
    for number, re in ((9, w.opened.id), (10, w.incoming.id)):
        w.store._write(post.Message(f'rep-20320102-1200-recipient-{number:06x}', 'report' if number == 9 else 'answer',
                                    'recipient', 'issuer', AT + timedelta(seconds=number), 'Closed.', re=re))
    opening, carried = w.seat.first_message()
    assert opening is None and carried == [], 'no model turn: the brief alone loads'


def test_a_resumed_hand_and_a_fresh_worker_are_carried_as_before(tmp_path, monkeypatch):
    monkeypatch.setattr(post, 'now', lambda: AT + timedelta(seconds=30))
    w = world(tmp_path / 'worker-ship', 'worker')
    carriage = w.seat.carriage()
    assert not carriage.in_hand and carriage.rows and carriage.header.startswith('qd > briefed as recipient, worker')
    opening, _ = w.seat.first_message()
    assert opening.startswith('qd > briefed as recipient, worker') and 'qd > open threads' in opening
    h = world(tmp_path / 'hand-ship', 'hand')
    monkeypatch.setattr(Seat, '_consumed_messages', lambda self, *args: frozenset())
    resumed = h.seat.carriage(resume_id='synthetic-conversation')
    assert not resumed.in_hand and resumed.rows, 'a resumed hand keeps its index'
