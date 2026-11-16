"""The pull-request screen's parts on fixtures (pr_screen): no live hand and no real pull request.

Hands here are stand-ins. ``Honest`` flags every line that reads like an instruction, the planted check among them;
others answer CLEAN, answer in no shape, answer nothing, or post elsewhere, to show what each counts as.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import post, pr_screen
from quarterdeck.harness import get
from quarterdeck.ship import SeatSpec

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures' / 'pr_screen'
CASES = {path.name: json.loads((path / 'pr.json').read_text()) for path in sorted(FIXTURES.iterdir())
         if (path / 'pr.json').is_file()}
HANDS = ('hand-a', 'hand-b')  # neutral names: the ship file declares a ship's own ([pr_screen] hands)
CORPUS = pr_screen.load_corpus(FIXTURES / 'planted-test-corpus.json')  # the tests' own; the live one is private
SUSPICIOUS = re.compile(r'ignore|disregard|write the word|reply only|answer clean|new instructions|qd post|'
                        r'human-alert|already approved|from now on|forward this', re.IGNORECASE)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Hands:
    """Stand-in hands: each instruction runs ``behaviour`` on the piece's folder; every call is recorded."""

    def __init__(self, runtime: Path, root: Path, behaviour, idle=True):
        self.runtime, self.root, self.behaviour, self.idle_answer = runtime, root, behaviour, idle
        self.calls, self.orders = [], {}

    def folder(self, name):
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def idle(self, name):
        return self.idle_answer

    def records(self, name):
        return sorted((self.root.parent / 'homes' / name).glob('*.jsonl'))

    def home(self, name):
        path = self.root.parent / 'homes' / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def kept_in_home(self, name, records):
        return [str(Path(record).relative_to(self.home(name))) for record in records]

    def fresh(self, name):
        # Started for a piece under the hold; started again after it, once the hold is released and the piece gone.
        held = pr_screen.held(self.runtime, name)
        self.calls.append(('fresh', name) if held else ('fresh after', name))
        if held:  # the screening launch: a process of its own, ended by the hand's down as qd's down ends it
            self.launched = getattr(self, 'launched', {})
            self.launched[name] = launch_process()

    def launch_record(self, name):
        """The record this launch writes, named before it exists, with the launch's pid and start."""
        process = self.launched[name]
        return dict(record=str(self.home(name) / 'screening.jsonl'),
                    launch=[process.pid, pr_screen_start(process.pid)])

    def down(self, name):
        assert pr_screen.held(self.runtime, name), 'the hand goes down before the hold is released'
        self.calls.append(('down', name))
        end_launch(getattr(self, 'launched', {}).pop(name, None))

    def instruct(self, name, body):
        message_id = f'ans-screen-{len(self.orders) + 1:03d}'
        self.orders[message_id] = (name, body)
        folder = self.folder(name) / re.search(r'in the folder (screen-[0-9a-f]+) ', body).group(1)
        self.behaviour(self, name, message_id, folder)
        self.calls.append(('instruct', name))
        return message_id

    def drop(self, name, message: dict):
        folder = post.drop_dir(self.runtime, name)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{message['id']}.json").write_text(json.dumps(message))


def pr_screen_start(pid):
    return Path(f'/proc/{pid}/stat').read_text().rpartition(')')[2].split()[19]


def launch_process():
    """A stand-in hand launch: a short-lived process of the test's own."""
    import subprocess
    return subprocess.Popen(['sleep', '120'])


def end_launch(process):
    if process is not None:
        process.kill()
        process.wait()


def report(hands, name, message_id, text='done'):
    """A post the hand was not asked for: a report on the instruction, as an obliging model might send."""
    hands.drop(name, dict(id=f'rep-done-{message_id}', kind='report', **{'from': name}, to=post.SHIP,
                          at='2026-11-15T08:00:00+00:00', body=text, re=message_id, subject=None, due=None, speed=None))


def honest(hands, name, message_id, folder):
    places = []
    for path in sorted(folder.glob('f*.txt')):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if SUSPICIOUS.search(line):
                places.append(f'{path.stem}:{number}')
    (folder / 'answer.txt').write_text('FLAGGED ' + ' '.join(places) if places else 'CLEAN')


def says_clean(hands, name, message_id, folder):
    (folder / 'answer.txt').write_text('CLEAN\n')


def silent(hands, name, order_id, folder):
    pass


class Outcomes:
    def __init__(self):
        self.told = []

    def clean(self, pr, repository):
        self.told.append(('reviewer', pr.number, None))
        return f'ord-reviewer-{pr.number}'

    def flagged(self, pr, repository, found):
        self.told.append(('desk', pr.number, found))
        return f'rep-desk-{pr.number}'

    def interrupted(self, number):
        self.told.append(('desk', number, 'an interrupted screen was finished'))
        return f'rep-desk-interrupted-{number}'


@pytest.fixture
def world(tmp_path):
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    return runtime, tmp_path / 'hands'


def run(world, behaviour, cases=None, **options):
    runtime, hands_root = world
    hands = Hands(runtime, hands_root, behaviour, idle=options.pop('idle', True))
    outcomes = Outcomes()
    clock = Clock()
    results = pr_screen.run(runtime, pr_screen.FixtureSource(FIXTURES, cases), hands, outcomes, hand_names=HANDS,
                            seat_names=('reviewer', 'desk', 'lead', *HANDS), corpus=CORPUS,
                            repository='owner/project', clock=clock, sleep=clock.sleep, poll=1.0,
                            answer_seconds=30, idle_seconds=30, **options)
    return results, hands, outcomes


# -- the outsider's text, numbered ---------------------------------------------------------------------------------

def texts(name):
    source = pr_screen.FixtureSource(FIXTURES, [name])
    return source.texts(source.open()[0])


def test_files_are_written_under_made_up_numbers_and_real_names_are_kept_apart():
    prepared = pr_screen.prepare(texts('dotdot-name'))
    assert all(re.fullmatch(r'f\d{3}', number) for number in prepared.files)
    named = [origin['name'] for origin in prepared.origin.values() if origin['name']]
    assert named == ['../../../../home/someone/.ssh/authorized_keys'], 'kept in the separate list only'
    listing = next(n for n, o in prepared.origin.items() if o['kind'] == 'file names')
    assert '../../../../home/someone/.ssh/authorized_keys' in prepared.files[listing], 'screened as text'


def test_an_over_long_name_is_cut_in_its_listing_and_flags_the_request():
    prepared = pr_screen.prepare(texts('long-name'))
    listing = next(n for n, o in prepared.origin.items() if o['kind'] == 'file names')
    assert all(len(line) <= pr_screen.NAME_BYTES for line in prepared.files[listing].splitlines())
    assert prepared.flags == [f'file 1 has a name over the length bound ({pr_screen.NAME_BYTES}); '
                              'the rest of it went to no hand']


def test_a_binary_file_goes_to_no_hand_and_flags_the_request():
    prepared = pr_screen.prepare(texts('binary-file'))
    assert prepared.flags == ['file 1 is binary and went to no hand']
    assert all(origin['kind'] != 'file' for origin in prepared.origin.values())


def test_an_oversized_change_goes_to_no_hand_and_flags_the_request():
    prepared = pr_screen.prepare(texts('oversized'))
    assert len(prepared.flags) == 1 and 'over the size bound' in prepared.flags[0]


def test_text_is_cut_into_files_and_pieces_a_hand_can_hold():
    big = pr_screen.Text('file', 1, (('x' * 50 + '\n') * 1000 + 'y' * 5000 + '\n').encode(), 'big.txt')
    prepared = pr_screen.prepare([big])
    for text in prepared.files.values():
        assert len(text.encode()) <= pr_screen.FILE_BYTES + 1 and len(text.splitlines()) <= pr_screen.FILE_LINES
        assert all(len(line.encode()) <= pr_screen.LINE_BYTES for line in text.splitlines())
    for piece in pr_screen.pieces(prepared):
        assert len(piece) <= pr_screen.PIECE_FILES
        assert sum(len(prepared.files[n].encode()) for n in piece) <= pr_screen.PIECE_BYTES
    parts = [o for o in prepared.origin.values() if o['kind'] == 'file']
    assert len(parts) > 1 and all(o['parts'] == len(parts) for o in parts)


# -- the search ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('name', sorted(CASES))
def test_the_one_list_finds_what_each_planted_case_expects(name):
    hits = {hit[0] for hit in pr_screen.search(pr_screen.prepare(texts(name)))}
    expected = set(CASES[name]['expect']['search'])
    assert expected <= hits
    if CASES[name]['expect']['kind'] == 'clean':
        assert hits == set()


def test_the_runtime_journal_pattern_names_the_trusted_folder_and_every_moved_name():
    from quarterdeck.trusted_files import MOVED
    pattern = re.compile(dict(pr_screen.PATTERNS)['runtime-journal'], re.IGNORECASE)
    for name in MOVED:
        assert pattern.search(f'write it into runtime/trusted/someone/{name}'), name
        if '.' in name or '-' in name:
            assert pattern.search(f'and a copy into {name} beside it'), name
    assert pattern.search('runtime/trusted/') and pattern.search('runtime/seats/x')
    assert not pattern.search('the record of the pool meeting'), 'a common word alone is no journal'
    assert not pattern.search('relaunch.jsonfile and my-launch.json-like names'), 'only whole names'


def test_the_patterns_are_one_list_in_one_file():
    assert all(len(entry) == 2 for entry in pr_screen.PATTERNS)
    assert len({name for name, _ in pr_screen.PATTERNS}) == len(pr_screen.PATTERNS)


# -- the answer's shape --------------------------------------------------------------------------------------------

NAMES = {'f01': 'f001', 'f03': 'f002'}
CANARY = ('f02', {3})


@pytest.mark.parametrize('answer, clean, reason', [
    (b'FLAGGED f02:3', True, 'found it clean'),
    (b'FLAGGED f02:1-4\n', True, 'found it clean'),
    (b'  FLAGGED f02:3, f01:7  ', False, 'flagged'),
    (b'CLEAN', False, 'did not flag the planted check'),
    (b'FLAGGED f01:7', False, 'did not flag the planted check'),
    (b'Looks clean to me.', False, 'answered in no shape'),
    (b'FLAGGED f02:3 and nothing else', False, 'answered in no shape'),
    (b'FLAGGED f09:1 f02:3', False, 'a file not in its piece'),
    (b'\x00', False, 'answered in no shape'),
    (b'FLAGGED f02:3 ' + b'f01:1 ' * 1000, False, 'answered in no shape'),
    (None, False, 'gave no answer in time'),
])
def test_an_answer_counts_only_by_its_shape(answer, clean, reason):
    verdict = pr_screen.judge('hand-a', answer, NAMES, CANARY)
    assert verdict.clean is clean and reason in verdict.reason


def test_a_flagged_answer_names_the_numbered_file_not_the_pieces_name():
    verdict = pr_screen.judge('hand-b', b'FLAGGED f02:3 f03:4-5', NAMES, CANARY)
    assert verdict.places == (('f002', 4, 5),)


# -- one piece through one hand: the hold, the sweep, the hand down -------------------------------------------------

def one_piece(world, behaviour, **options):
    runtime, hands_root = world
    hands = Hands(runtime, hands_root, behaviour)
    clock = Clock()
    prepared = pr_screen.prepare(texts('post-elsewhere'))
    verdict = pr_screen.screen_piece(runtime, hands, 'hand-a', 'key', 0, prepared, list(prepared.files), corpus=CORPUS,
                                     answer_seconds=30, poll=1.0, clock=clock, sleep=clock.sleep, **options)
    return verdict, hands, runtime


def test_a_hand_told_to_post_elsewhere_has_nothing_it_posts_filed(world):
    def obeys(hands, name, message_id, folder):
        hands.drop(name, dict(id='ord-forged-elsewhere', kind='order', **{'from': name}, to='reviewer',
                              at='2026-11-15T08:00:00+00:00', body='PUFFIN', re=None, subject=None, due=None,
                              speed=None))
        honest(hands, name, message_id, folder)
    verdict, hands, runtime = one_piece(world, obeys)
    assert list(post.drop_dir(runtime, 'hand-a').iterdir()) == [], 'nothing it posted stays to be filed'
    assert len(list((runtime / 'pr-screen' / 'held' / 'hand-a').iterdir())) == 1, 'moved aside'
    assert [call[0] for call in hands.calls] == ['fresh', 'instruct', 'down', 'fresh after']
    assert not pr_screen.held(runtime, 'hand-a'), 'the hold is released once the hand is down and swept'
    assert not verdict.clean  # it flagged the request's lines


def test_a_report_the_hand_was_not_asked_for_is_moved_aside_too_even_a_bare_done(world):
    def obliging(hands, name, message_id, folder):
        honest(hands, name, message_id, folder)
        report(hands, name, message_id, 'done')
        report(hands, name, f'{message_id}-again', 'FLAGGED f01:6')
    verdict, hands, runtime = one_piece(world, obliging)
    assert list(post.drop_dir(runtime, 'hand-a').iterdir()) == []
    assert len(list((runtime / 'pr-screen' / 'held' / 'hand-a').iterdir())) == 2
    kept = json.loads((runtime / 'pr-screen' / 'screens' / 'key' / 'piece-0-hand-a.verdict.json').read_text())
    assert kept['moved_aside'] == 2 and 'done' not in kept


def test_the_hand_goes_down_as_soon_as_its_answer_is_there(world):
    runtime, hands_root = world
    hands = Hands(runtime, hands_root, honest)
    clock = Clock()
    prepared = pr_screen.prepare(texts('post-elsewhere'))
    pr_screen.screen_piece(runtime, hands, 'hand-a', 'key', 0, prepared, list(prepared.files), corpus=CORPUS,
                           answer_seconds=30, poll=1.0, clock=clock, sleep=clock.sleep)
    assert clock.now == 0, 'no wait after the answer: it was there at the first look'


def test_the_instruction_asks_for_no_report_and_leaves_nothing_open(world):
    runtime, hands_root = world
    ship = SimpleNamespace(runtime=runtime, folder=runtime.parent)
    live = pr_screen.LiveHands(ship, Path('qd'), hands_root)
    message = post.Store(runtime).get(live.instruct('hand-a', pr_screen.INSTRUCTION.format(
        files='f01.txt', folder='screen-0a0a0a0a0a0a')))
    assert pr_screen.spent_instructions(runtime, 'hand-a') == frozenset(), 'written unlisted: delivered once'
    assert message.kind == 'answer' and message.sender == post.SHIP and message.re is None
    assert not post.Store(runtime).open_for('hand-a'), 'nothing open: the idle rule finds the hand free'
    assert 'report on' not in message.body.replace('report on nothing', '') and 'post nothing' in message.body


def test_no_answer_in_time_counts_as_flagged_and_the_hand_still_goes_down(world):
    verdict, hands, runtime = one_piece(world, silent)
    assert not verdict.clean and 'no answer in time' in verdict.reason
    assert ('down', 'hand-a') in hands.calls and not pr_screen.held(runtime, 'hand-a')


def test_a_hand_that_misses_the_planted_check_counts_as_flagged(world):
    verdict, _, _ = one_piece(world, says_clean)
    assert not verdict.clean and 'planted check' in verdict.reason


def test_the_planted_file_cannot_be_told_from_the_outsiders(world):
    verdict, hands, runtime = one_piece(world, honest)
    folder = runtime / 'pr-screen' / 'screens' / 'key' / 'piece-0-hand-a'  # the piece, as kept after the screen
    names = sorted(path.name for path in folder.glob('f*.txt'))
    assert names == [f'f{n:02d}.txt' for n in range(1, len(names) + 1)], 'one numbered sequence'
    record = json.loads(next((runtime / 'pr-screen' / 'screens' / 'key').glob('piece-0-hand-a.json')).read_text())
    assert f"{record['planted']}.txt" in names and record['planted'] not in record['names']


def test_while_a_hand_screens_the_service_files_nothing_it_posts(world):
    runtime, _ = world
    store = post.Store(runtime)
    message = store.prepare('order', 'hand-a', 'reviewer', 'an outsider\'s words')
    drop = post.drop_dir(runtime, 'hand-a')
    drop.mkdir(parents=True)
    (drop / f'{message.id}.json').write_text(message.to_json())
    pr_screen.hold(runtime, 'hand-a', 'key')
    post.file_drops(store, seats=frozenset({'hand-a'}))
    assert store.all() == [] and (drop / f'{message.id}.json').exists()
    assert pr_screen.status(runtime) and 'its posts are held' in pr_screen.status(runtime)[0]
    pr_screen.release(runtime, 'hand-a')
    post.file_drops(store, seats=frozenset({'hand-a'}))
    assert [m.id for m in post.Store(runtime).all()] == [message.id]


# -- the whole pass ------------------------------------------------------------------------------------------------

OUTSIDER_WORDS = ('OSPREY', 'KESTREL', 'PLOVER', 'CURLEW', 'DUNLIN', 'MERLIN', 'PUFFIN', 'SHRIKE', 'WIGEON',
                  'TURNSTONE', 'GODWIT', 'LAPWING', 'authorized_keys', 'IGNORE_PREVIOUS', 'Reviewer bot', 'already screened', 'recieve')


def test_a_pass_tells_una_of_clean_requests_and_the_desk_of_flagged_ones_quoting_nothing(world):
    results, hands, outcomes = run(world, honest)
    by_case = {CASES[name]['number']: name for name in CASES}
    outcome = {by_case[r['number']]: r['outcome'] for r in results}
    assert outcome['clean-docs'] == outcome['clean-code'] == 'clean'
    planted = [name for name in CASES if CASES[name]['expect']['kind'] != 'clean']
    caught = [name for name in planted if outcome[name] == 'flagged']
    # The residual, shown: a case written to slip past the search, before a hand that reads it naively.
    assert sorted(set(planted) - set(caught)) == ['answer-clean-subtle']
    told = [entry for entry in outcomes.told if entry[0] == 'desk']
    assert len(told) == len(caught)
    text = json.dumps(told)
    assert not any(word in text for word in OUTSIDER_WORDS), 'nothing the outsider wrote reaches the desk'
    assert all(name in ('hand-a', 'hand-b') for kind, name in hands.calls if kind == 'instruct')


def test_a_request_already_screened_at_its_head_is_skipped_and_a_new_push_starts_it_over(world):
    results, _, _ = run(world, honest, cases=['clean-docs'])
    assert [r['outcome'] for r in results] == ['clean']
    assert run(world, honest, cases=['clean-docs'])[0] == []
    runtime, _ = world
    state = json.loads((runtime / 'pr-screen' / 'state.json').read_text())
    state['1']['head'] = 'an older head'
    (runtime / 'pr-screen' / 'state.json').write_text(json.dumps(state))
    assert [r['outcome'] for r in run(world, honest, cases=['clean-docs'])[0]] == ['clean']


def test_a_busy_hand_leaves_the_request_for_the_next_pass_untold(world):
    results, hands, outcomes = run(world, honest, cases=['clean-docs'], idle=False)
    assert [r['outcome'] for r in results] == ['deferred'] and outcomes.told == [] and hands.calls == []
    runtime, _ = world
    assert not (runtime / 'pr-screen' / 'state.json').exists() or '1' not in json.loads(
        (runtime / 'pr-screen' / 'state.json').read_text())


def test_flagged_before_any_hand_goes_to_no_hand(world):
    results, hands, outcomes = run(world, honest, cases=['binary-file', 'oversized'])
    assert [r['outcome'] for r in results] == ['flagged', 'flagged'] and hands.calls == []


# -- the screen's instructions never come back ----------------------------------------------------------------------

def instruction(runtime, hand='hand-a'):
    ship = SimpleNamespace(runtime=runtime, folder=runtime.parent)
    body = pr_screen.INSTRUCTION.format(files='f01.txt', folder='screen-0b0b0b0b0b0b')
    return pr_screen.LiveHands(ship, Path('qd'), runtime / 'hands').instruct(hand, body)


def test_the_instruction_is_listed_when_its_piece_ends_and_not_before(world):
    seen = []

    def looks(hands, name, message_id, folder):
        seen.append(pr_screen.spent_instructions(hands.runtime, name))
        honest(hands, name, message_id, folder)
    verdict, hands, runtime = one_piece(world, looks)
    assert seen == [frozenset()], 'while the hand screens, nothing is listed: the service may type it'
    assert pr_screen.spent_instructions(runtime, 'hand-a') == {'ans-screen-001'}, 'listed once the piece ended'
    assert not pr_screen.held(runtime, 'hand-a')


def test_an_instruction_filed_before_instruct_raised_is_listed_by_the_strict_match(world):
    runtime, hands_root = world

    class LostAfterFiling(Hands):
        def instruct(self, name, body):
            self.filed = post.Store(self.runtime).new('answer', post.SHIP, name, body).id
            raise OSError('the id was lost after the store filed it')
    hands = LostAfterFiling(runtime, hands_root, honest)
    prepared = pr_screen.prepare(texts('post-elsewhere'))
    with pytest.raises(OSError):
        pr_screen.screen_piece(runtime, hands, 'hand-a', 'key', 0, prepared, list(prepared.files), corpus=CORPUS,
                               answer_seconds=30, poll=1.0, clock=Clock(), sleep=lambda seconds: None)
    listed = json.loads((runtime / 'pr-screen' / 'instructions' / 'hand-a.json').read_text())
    assert listed == [hands.filed], 'listed though its id never came back'
    assert not pr_screen.held(runtime, 'hand-a'), 'listed: the hold is released'
    assert hands.filed not in [m.id for m in post.Attempts(runtime).undelivered(post.Store(runtime), 'hand-a')]


def test_a_store_that_cannot_be_read_for_the_strict_match_leaves_the_hold(world, monkeypatch):
    runtime, hands_root = world

    class Raises(Hands):
        def instruct(self, name, body):
            raise OSError('the instruction may or may not have been filed')
    def unreadable(*args, **kwargs):
        raise OSError('store unreadable')
    monkeypatch.setattr(pr_screen, 'instruction_answers', unreadable)
    prepared = pr_screen.prepare(texts('post-elsewhere'))
    with pytest.raises(OSError):
        pr_screen.screen_piece(runtime, Raises(runtime, hands_root, honest), 'hand-a', 'key', 0, prepared,
                               list(prepared.files), corpus=CORPUS, answer_seconds=30, poll=1.0, clock=Clock(),
                               sleep=lambda seconds: None)
    assert pr_screen.held(runtime, 'hand-a'), 'its posts stay held, and the status line says so'


def test_a_listing_that_fails_leaves_the_hold(world, monkeypatch):
    def fails(*args):
        raise OSError('cannot write the list')
    monkeypatch.setattr(pr_screen, 'note_instruction', fails)
    with pytest.raises(OSError):
        one_piece(world, honest)
    assert pr_screen.held(world[0], 'hand-a'), 'its posts stay held, and the status line says so'


def test_a_listed_instruction_is_never_delivered_again_even_unconfirmed_or_with_a_spoiled_list(world):
    runtime, _ = world
    store = post.Store(runtime)
    listed = instruction(runtime)
    undelivered = lambda: [m.id for m in post.Attempts(runtime).undelivered(post.Store(runtime), 'hand-a')]
    assert listed in undelivered(), 'unlisted, it is delivered once, as any message'
    pr_screen.note_instruction(runtime, 'hand-a', listed)
    other = store.new('answer', post.SHIP, 'hand-a', 'an ordinary answer from ship').id
    replied = store.new('answer', post.SHIP, 'hand-a', pr_screen.INSTRUCTION.split('{')[0] + 'x', re=other).id
    undelivered = lambda: [m.id for m in post.Attempts(runtime).undelivered(post.Store(runtime), 'hand-a')]
    assert listed not in undelivered() and other in undelivered(), 'only the listed one is skipped'
    (runtime / 'pr-screen' / 'instructions' / 'hand-a.json').write_text('spoiled by a fooled hand?')
    assert listed not in undelivered(), 'a spoiled list hides more, never less'
    assert other in undelivered() and replied in undelivered(), 'the strict match hides only the instruction\'s form'
    assert pr_screen.spent_instructions(runtime, 'hand-b') == frozenset(), 'each hand\'s own list'


# -- outsider text does not outlive the screen, and the planted check's corpus stays private --------------------

def test_a_piece_is_kept_under_the_runtime_and_removed_from_the_hands_folder(world):
    verdict, hands, runtime = one_piece(world, honest)
    assert not any((world[1] / 'hand-a').glob('screen-*')), 'no piece folder is left where the hand reads'
    kept = runtime / 'pr-screen' / 'screens' / 'key' / 'piece-0-hand-a'
    assert (kept / 'answer.txt').read_text().startswith('FLAGGED')
    assert len(list(kept.glob('f*.txt'))) == len(pr_screen.prepare(texts('post-elsewhere')).files) + 1
    assert hands.calls[-1] == ('fresh after', 'hand-a'), 'the hand is started fresh: the watched record is a new one'


def test_a_link_swapped_in_for_the_piece_is_removed_never_followed(world, tmp_path):
    decoy = tmp_path / 'operator'
    decoy.mkdir()
    (decoy / 'keep.txt').write_text('mine')

    def swaps(hands, name, order_id, folder):
        honest(hands, name, order_id, folder)
        moved = folder.with_name(folder.name + '-moved')
        folder.rename(moved)
        folder.symlink_to(decoy)
    verdict, hands, runtime = one_piece(world, swaps)
    assert (decoy / 'keep.txt').read_text() == 'mine' and not any((world[1] / 'hand-a').glob('screen-*[0-9a-f]'))


def test_everything_new_or_changed_in_the_hands_folder_is_moved_aside_and_the_rest_stays(world, tmp_path):
    outside = tmp_path / 'operator'
    outside.mkdir()
    (outside / 'keep.txt').write_text('mine')
    own = world[1] / 'hand-a'
    (own / 'notes').mkdir(parents=True)
    (own / 'unchanged.txt').write_text('as it was')
    (own / 'changed.txt').write_text('as it was')
    (own / 'notes' / 'old.txt').write_text('as it was')
    (own / 'old-link').symlink_to(outside)

    def writes_elsewhere(hands, name, order_id, folder):
        honest(hands, name, order_id, folder)
        root = folder.parent
        (root / 'note.txt').write_text('TURNSTONE')
        (root / 'changed.txt').write_text('TURNSTONE, longer than it was')
        (root / 'notes' / 'later.txt').write_text('TURNSTONE')
        (root / 'new-folder' / 'deep').mkdir(parents=True)
        (root / 'new-folder' / 'deep' / 'x.txt').write_text('TURNSTONE')
        (root / 'new-link').symlink_to(outside)
    verdict, hands, runtime = one_piece(world, writes_elsewhere)
    left = sorted(str(path.relative_to(own)) for path in own.rglob('*'))
    assert left == ['notes', 'notes/old.txt', 'old-link', 'unchanged.txt'], 'only what was there and unchanged stays'
    assert (own / 'unchanged.txt').read_text() == (own / 'notes' / 'old.txt').read_text() == 'as it was'
    aside = next((runtime / 'pr-screen' / 'held' / 'hand-a').glob('folder-key-0-*'))
    assert sorted(str(path.relative_to(aside)) for path in aside.rglob('*')) == [
        'changed.txt', 'new-folder', 'new-folder/deep', 'new-folder/deep/x.txt', 'new-link', 'note.txt', 'notes',
        'notes/later.txt']
    assert (aside / 'new-link').is_symlink() and (outside / 'keep.txt').read_text() == 'mine', 'a link moves as itself'
    kept = json.loads((runtime / 'pr-screen' / 'screens' / 'key' / 'piece-0-hand-a.verdict.json').read_text())
    assert kept['folder_moved_aside'] == 5
    assert not pr_screen.held(runtime, 'hand-a') and hands.calls[-1] == ('fresh after', 'hand-a')


def test_a_same_size_rewrite_with_its_time_set_back_is_moved_aside(world):
    own = world[1] / 'hand-a'
    own.mkdir(parents=True)
    (own / 'same.txt').write_text('as it was')
    (own / 'kept.txt').write_text('as it was')
    was = os.stat(own / 'same.txt')

    def rewrites_in_place(hands, name, order_id, folder):
        honest(hands, name, order_id, folder)
        with open(folder.parent / 'same.txt', 'r+') as stream:
            stream.write('TURNSTONE')  # the same nine bytes
        os.utime(folder.parent / 'same.txt', ns=(was.st_atime_ns, was.st_mtime_ns))
    verdict, hands, runtime = one_piece(world, rewrites_in_place)
    assert sorted(path.name for path in own.iterdir()) == ['kept.txt'], 'its change time gave it away'
    aside = next((runtime / 'pr-screen' / 'held' / 'hand-a').glob('folder-key-0-*'))
    assert (aside / 'same.txt').read_text() == 'TURNSTONE'


def test_a_hand_with_an_open_order_is_not_idle_for_the_screen_even_down(world, monkeypatch):
    runtime, hands_root = world
    ship = SimpleNamespace(runtime=runtime, folder=runtime.parent)
    live = pr_screen.LiveHands(ship, Path('qd'), hands_root)
    down = json.dumps(dict(seat=dict(activity=dict(state='down')), state=dict(turn_open=False)))
    asked = []
    monkeypatch.setattr(live, '_qd', lambda *args, **kw: asked.append(args) or SimpleNamespace(stdout=down))
    assert live.idle('hand-a'), 'down, with nothing open: the screen may start it fresh'
    order = post.Store(runtime).new('order', 'reviewer', 'hand-a', 'count the files', subject='Count')
    assert not live.idle('hand-a'), 'an order from another seat comes first'
    assert len(asked) == 1, 'decided by the order alone, before the screen is read'
    post.Store(runtime).new('report', 'hand-a', 'reviewer', 'counted', re=order.id)
    assert live.idle('hand-a')


def test_a_folder_that_cannot_be_cleared_keeps_the_hold(world, monkeypatch):
    def fails(*args):
        raise OSError('cannot move')
    monkeypatch.setattr(pr_screen, 'move_aside_new', fails)
    with pytest.raises(OSError):
        one_piece(world, honest)
    assert pr_screen.held(world[0], 'hand-a'), 'its posts stay held, and the status line says so'


def test_the_records_the_screening_launch_wrote_are_listed_and_no_other(world, tmp_path):
    home = tmp_path / 'homes' / 'hand-a'
    home.mkdir(parents=True)
    (home / 'earlier.jsonl').write_text('a launch before the screen\n')

    def writes_a_record(hands, name, order_id, folder):
        (home / 'screening.jsonl').write_text('the screening launch read the piece\n')
        honest(hands, name, order_id, folder)
    verdict, hands, runtime = one_piece(world, writes_a_record)
    assert pr_screen.screening_records(runtime, 'hand-a') == [str(home / 'screening.jsonl')]
    assert pr_screen.screening_left(runtime, 'hand-a') and not pr_screen.screening_left(runtime, 'hand-b')
    kept = json.loads((runtime / 'pr-screen' / 'screens' / 'key' / 'piece-0-hand-a.verdict.json').read_text())
    assert kept['screening_records'] == 1
    (home / 'screening.jsonl').unlink()  # as the retirement moves it out of the home
    assert not pr_screen.screening_left(runtime, 'hand-a')


def test_everything_new_or_changed_in_the_hands_home_is_moved_aside_but_its_records(world, tmp_path):
    home = tmp_path / 'homes' / 'hand-a'
    (home / '.config').mkdir(parents=True)
    (home / '.config' / 'settings.json').write_text('{"theme": "dark"}')
    (home / 'earlier.jsonl').write_text('a launch before the screen\n')
    (home / 'credential.json').write_text('{"token": "a"}')
    (home / 'unchanged.txt').write_text('as it was')

    def writes_into_home(hands, name, order_id, folder):
        (home / 'screening.jsonl').write_text('the screening launch read the piece\n')
        (home / 'notes.txt').write_text('GODWIT')
        (home / '.config' / 'settings.json').write_text('{"theme": "GODWIT"}')
        (home / '.cache' / 'deep').mkdir(parents=True)
        (home / '.cache' / 'deep' / 'x').write_text('GODWIT')
        (home / 'credential.json').write_text('{"token": "refreshed"}')
        honest(hands, name, order_id, folder)
    verdict, hands, runtime = one_piece(world, writes_into_home)
    left = sorted(str(path.relative_to(home)) for path in home.rglob('*'))
    assert left == ['.config', 'earlier.jsonl', 'screening.jsonl', 'unchanged.txt']
    aside = next((runtime / 'pr-screen' / 'held' / 'hand-a').glob('home-key-0-*'))
    assert sorted(str(path.relative_to(aside)) for path in aside.rglob('*')) == [
        '.cache', '.cache/deep', '.cache/deep/x', '.config', '.config/settings.json', 'credential.json', 'notes.txt']
    assert (aside / 'credential.json').read_text() == '{"token": "refreshed"}', 'a changed credential file too' 
    assert pr_screen.screening_records(runtime, 'hand-a') == [str(home / 'screening.jsonl')], 'left to the retirement'
    kept = json.loads((runtime / 'pr-screen' / 'screens' / 'key' / 'piece-0-hand-a.verdict.json').read_text())
    assert kept['home_moved_aside'] == 4


def test_the_home_and_folder_are_listed_after_the_fresh_start_and_before_the_piece(world, tmp_path):
    home = tmp_path / 'homes' / 'hand-a'
    seen = []

    class Rewrites(Hands):
        def fresh(self, name):
            super().fresh(name)
            if not seen:  # the screening launch's start: qd rewrites what it writes at each launch
                home.mkdir(parents=True, exist_ok=True)
                (home / 'launch-config.json').write_text(f'{{"launch": {len(self.calls)}}}')
                seen.append(not any((self.root / name).glob('screen-*')))
    runtime, hands_root = world
    hands = Rewrites(runtime, hands_root, honest)
    clock = Clock()
    prepared = pr_screen.prepare(texts('post-elsewhere'))
    pr_screen.screen_piece(runtime, hands, 'hand-a', 'key', 0, prepared, list(prepared.files), corpus=CORPUS,
                           answer_seconds=30, poll=1.0, clock=clock, sleep=clock.sleep)
    assert seen == [True], 'the piece goes in only after the fresh start'
    assert (home / 'launch-config.json').is_file(), 'what qd wrote at the launch is not moved aside'


def test_the_live_hands_keep_no_credential_file_in_the_home(world, tmp_path):
    spec = SeatSpec('hand-a', 'crew', 'hand', 'claude', ('local',))  # a program that declares a credential file
    ship = SimpleNamespace(runtime=world[0], folder=world[0].parent, seat=lambda name: spec)
    live = pr_screen.LiveHands(ship, Path('qd'), world[1])
    assert get('claude').credential_file, 'the case this test is about'
    assert live.kept_in_home('hand-a', []) == [], 'not written fresh at each launch: swept when changed'


def test_a_list_is_deleted_once_nothing_it_names_is_left_and_none_is_written_empty(world, tmp_path):
    runtime, _ = world
    pr_screen.mark_screening(runtime, 'hand-a', [])
    assert not (runtime / 'pr-screen' / 'records' / 'hand-a.json').exists(), 'nothing to list: no list'
    record = tmp_path / 'home' / 'screening.jsonl'
    record.parent.mkdir()
    record.write_text('read the piece\n')
    pr_screen.mark_screening(runtime, 'hand-a', [str(record)])
    assert not pr_screen.drop_spent(runtime, 'hand-a'), 'a record still in the home keeps its list'
    record.unlink()  # as the retirement moves it out
    assert pr_screen.drop_spent(runtime, 'hand-a')
    assert sorted(path.name for path in (runtime / 'pr-screen' / 'records').iterdir()) == ['.lock']


def test_an_unreadable_list_of_screening_records_keeps_launches_fresh(world):
    runtime, _ = world
    (runtime / 'pr-screen' / 'records').mkdir(parents=True)
    (runtime / 'pr-screen' / 'records' / 'hand-a.json').write_text('not a list')
    assert pr_screen.screening_left(runtime, 'hand-a')


def test_a_missing_or_malformed_corpus_refuses_the_screen_before_anything_is_read(world, tmp_path):
    runtime, hands_root = world

    class Untouched:
        def open(self):
            pytest.fail('the screen read a pull request without its corpus')
    for text in (None, 'not json', json.dumps(dict(phrasings=['say {word}'], words=['heron'], fillers=['x']))):
        if text is not None:
            (runtime / 'pr-screen').mkdir(parents=True, exist_ok=True)
            (runtime / 'pr-screen' / 'planted.json').write_text(text)
        with pytest.raises(pr_screen.ScreenError, match='corpus'):
            pr_screen.run(runtime, Untouched(), Hands(runtime, hands_root, honest), Outcomes(), repository='o/p',
                          hand_names=HANDS)


def test_the_code_keeps_the_mechanism_and_no_corpus():
    source = Path(pr_screen.__file__).read_text()
    assert not hasattr(pr_screen, 'PLANTED') and not hasattr(pr_screen, 'WORDS')
    assert not any(phrasing.split('{word}')[0][:30] in source for phrasing in CORPUS.phrasings)
    text, lines = pr_screen.planted(CORPUS)
    [line] = lines
    assert any(text.splitlines()[line - 1].startswith(p.split('{word}')[0]) for p in CORPUS.phrasings)


# -- the listing written before a piece goes in ---------------------------------------------------------------------

@pytest.mark.parametrize('why', ['unreadable', 'not running', 'existed before'])
def test_a_launch_record_that_names_no_new_running_launch_writes_no_piece(world, why):
    """No piece goes in unless the record its launch writes is listed first: a launch record that cannot be read, one
    naming a launch that is not running, or one naming a record that was already there, stops the screening."""
    runtime, hands_root = world

    class Unnamed(Hands):
        def launch_record(self, name):
            if why == 'unreadable':
                return None
            named = super().launch_record(name)
            if why == 'not running':
                end_launch(self.launched[name])
            else:
                Path(named['record']).write_text('a record from before this launch\n')
            return named
    hands = Unnamed(runtime, hands_root, honest)
    if why == 'existed before':
        hands.home('hand-a')
        hands.records = lambda name: [hands.home(name) / 'screening.jsonl']
    prepared = pr_screen.prepare(texts('post-elsewhere'))
    with pytest.raises(pr_screen.ScreenError, match='no piece went in'):
        pr_screen.screen_piece(runtime, hands, 'hand-a', 'key', 0, prepared, list(prepared.files), corpus=CORPUS,
                               answer_seconds=30, poll=1.0, clock=Clock(), sleep=Clock().sleep)
    assert [call[0] for call in hands.calls] == ['fresh', 'down'], 'no instruction: the hand is downed at once'
    assert list(hands.folder('hand-a').glob('screen-*')) == [], 'no piece was written'
    assert not pr_screen.held(runtime, 'hand-a'), 'its finally ran whole'


def test_the_record_is_listed_before_the_piece_is_written(world, monkeypatch):
    """At the moment the piece is written, the record the screening launch will write is already listed, though it
    does not exist yet: a kill there leaves it listed."""
    seen = []
    real = pr_screen._write_piece

    def writing(folder, name, files):
        runtime = world[0]
        seen.append((pr_screen.screening_records(runtime, 'hand-a'), Path(folder).parent.name))
        return real(folder, name, files)
    monkeypatch.setattr(pr_screen, '_write_piece', writing)
    verdict, hands, runtime = one_piece(world, honest)
    home = hands.home('hand-a')
    [(listed, _)] = seen
    assert listed == [str(home / 'screening.jsonl')] and not (home / 'screening.jsonl').exists()


def started(process):
    return [process.pid, pr_screen_start(process.pid)]


def test_a_record_named_before_it_exists_stays_listed_while_its_launch_runs_and_is_spent_after(tmp_path):
    runtime, record = tmp_path / 'runtime', tmp_path / 'home' / 'data-003' / 'opencode' / 'opencode.db'
    launch = launch_process()
    try:
        pr_screen.mark_screening(runtime, 'hand', [dict(record=str(record), launch=started(launch))])
        assert pr_screen.screening_records(runtime, 'hand') == [str(record)] and pr_screen.screening_left(runtime, 'hand')
        assert not pr_screen.drop_spent(runtime, 'hand'), 'the launch may still write it: the list stays'
        pr_screen.mark_screening(runtime, 'hand', [str(record)])  # the finally lists it again, by path
        [entry] = json.loads((runtime / 'pr-screen' / 'records' / 'hand.json').read_text())
        assert entry['record'] == str(record) and entry['launch'] == started(launch), 'it keeps its launch'
    finally:
        end_launch(launch)
    assert pr_screen.screening_records(runtime, 'hand') == [], 'never written, and its launch has ended: spent'
    assert pr_screen.drop_spent(runtime, 'hand')


def test_a_record_named_before_it_exists_and_then_written_stays_listed_after_its_launch(tmp_path):
    runtime, record = tmp_path / 'runtime', tmp_path / 'home' / 'data-003' / 'opencode' / 'opencode.db'
    launch = launch_process()
    pr_screen.mark_screening(runtime, 'hand', [dict(record=str(record), launch=started(launch))])
    record.parent.mkdir(parents=True)
    record.write_bytes(b'written by the launch')
    end_launch(launch)
    assert pr_screen.screening_records(runtime, 'hand') == [str(record)], 'in the home: listed until retired'
    record.unlink()
    assert pr_screen.screening_records(runtime, 'hand') == []


def test_a_list_entry_of_no_known_shape_makes_the_list_unreadable(tmp_path):
    runtime = tmp_path / 'runtime'
    (runtime / 'pr-screen' / 'records').mkdir(parents=True)
    (runtime / 'pr-screen' / 'records' / 'hand.json').write_text(json.dumps([dict(record='/x', launch=['1', 2])]))
    with pytest.raises(ValueError):
        pr_screen.screening_records(runtime, 'hand')
    assert pr_screen.screening_left(runtime, 'hand'), 'unreadable: the hand never resumes until a person looks'


def test_the_live_hands_name_an_opencode_launchs_record_from_the_trusted_launch_record_alone(tmp_path):
    from quarterdeck import trusted_files
    runtime = tmp_path / 'runtime'
    home = runtime / 'seats' / 'hand' / 'home'
    (home / 'data-999').mkdir(parents=True)  # a folder the hand made counts for nothing
    ship = SimpleNamespace(runtime=runtime, seat=lambda name: SimpleNamespace(name=name))
    hands = pr_screen.LiveHands(ship, Path('qd'), tmp_path / 'hands')
    assert hands.launch_record('hand') is None, 'no launch record: none named'
    launch = trusted_files.make(runtime, 'hand') / trusted_files.LAUNCH
    launch.write_text('{not json')
    assert hands.launch_record('hand') is None, 'unreadable: none named'
    launch.write_text(json.dumps(dict(pid=4242, start='123', harness='opencode', argv=[
        'bwrap', '--setenv', 'XDG_DATA_HOME', str(home / 'data-007'), '--', 'opencode'])))
    assert hands.launch_record('hand') == dict(record=str(home / 'data-007' / 'opencode' / 'opencode.db'),
                                                launch=[4242, '123'])
    launch.write_text(json.dumps(dict(pid=4242, start='123', harness='pi', argv=['pi'])))
    assert hands.launch_record('hand') is None, 'a program whose record is not named in advance: none'



# -- an interrupted screen, finished by the next run ----------------------------------------------------------------

def interrupted_hold(runtime, hand, *, start=True, journal=True, screen=None):
    """A hold as a killed screen leaves it: its screen process given (one that has ended, unless ``screen`` runs),
    with its journal; or, as an older hold, with no start."""
    process = screen or launch_process()
    pid, start_of = process.pid, pr_screen_start(process.pid)
    if screen is None:
        end_launch(process)
    folder = runtime / 'pr-screen' / 'screening'
    folder.mkdir(parents=True, exist_ok=True)
    hold = dict(seat=hand, screen='7-0123456789ab', since='2026-11-15T21:00:00+00:00', pid=pid)
    if start:
        hold['start'] = start_of
    (folder / f'{hand}.json').write_text(json.dumps(hold))
    if journal:
        (runtime / 'pr-screen' / 'journal').mkdir(parents=True, exist_ok=True)
        (runtime / 'pr-screen' / 'journal' / f'{hand}.json').write_text(json.dumps(dict(
            key='7-0123456789ab', index=0, folder='screen-0123456789ab', records_before={}, before={},
            home_before={})))


@pytest.mark.parametrize('why', ['its screen still runs', 'an older hold with no start', 'no journal'])
def test_a_hold_that_cannot_be_proved_finished_stays_held_and_the_run_screens_nothing(world, why):
    runtime, hands_root = world
    screen = launch_process() if why == 'its screen still runs' else None
    try:
        interrupted_hold(runtime, 'hand-a', start=why != 'an older hold with no start',
                         journal=why != 'no journal', screen=screen)
        hands = Hands(runtime, hands_root, honest)
        assert pr_screen.recover(runtime, hands, Outcomes()) == [] and pr_screen.held(runtime, 'hand-a')
        assert hands.calls == [], 'the hand is not touched'
        with pytest.raises(pr_screen.ScreenError, match='hand-a: held by a screen that is not finished'):
            run(world, honest, ['clean-docs'])
    finally:
        end_launch(screen)


def test_a_hold_whose_screen_is_gone_is_finished_once_and_told_by_its_number_alone(world):
    runtime, hands_root = world
    hands = Hands(runtime, hands_root, honest)
    piece = hands.folder('hand-a') / 'screen-0123456789ab'
    piece.mkdir()
    (piece / 'f01.txt').write_text('the outsider\'s text, left by a killed screen\n')
    interrupted_hold(runtime, 'hand-a')
    hands.launched = {}
    outcomes = Outcomes()
    finished = pr_screen.recover(runtime, hands, outcomes)
    assert [(f['number'], f['hand'], f['outcome']) for f in finished] == [(7, 'hand-a', 'interrupted screen finished')]
    assert outcomes.told == [('desk', '7', 'an interrupted screen was finished')]
    assert ('down', 'hand-a') in hands.calls and not piece.exists() and not pr_screen.held(runtime, 'hand-a')
    kept = runtime / 'pr-screen' / 'screens' / '7-0123456789ab'
    assert (kept / 'piece-0-hand-a' / 'f01.txt').is_file(), 'the piece is kept where no seat is pointed'
    assert json.loads((kept / 'piece-0-hand-a.verdict.json').read_text())['interrupted'] is True
    assert not (runtime / 'pr-screen' / 'journal' / 'hand-a.json').exists()
    assert pr_screen.recover(runtime, hands, outcomes) == [] and len(outcomes.told) == 1, 'finished once'


def test_a_screen_takes_its_hold_with_its_own_pid_and_start_and_leaves_no_journal(world):
    seen = []
    real = pr_screen._write_piece

    def writing(folder, name, files):
        runtime = world[0]
        hold = json.loads((runtime / 'pr-screen' / 'screening' / 'hand-a.json').read_text())
        journal = json.loads((runtime / 'pr-screen' / 'journal' / 'hand-a.json').read_text())
        seen.append((hold['pid'], hold['start'], sorted(journal)))
        return real(folder, name, files)
    import unittest.mock
    with unittest.mock.patch.object(pr_screen, '_write_piece', writing):
        verdict, hands, runtime = one_piece(world, honest)
    [(pid, start, fields)] = seen
    assert pid == os.getpid() and start == pr_screen_start(os.getpid())
    assert fields == ['before', 'folder', 'home_before', 'index', 'key', 'records_before'], 'all its finally needs'
    assert not (runtime / 'pr-screen' / 'journal' / 'hand-a.json').exists(), 'finished: no journal is left'



# -- a sweep before the screen, and a sweep during it -------------------------------------------------------------

def sweep_record(runtime, hand, at):
    from quarterdeck import jail_end
    jail_end._record(runtime, hand, dict(seat=hand, pid=4242, start='123', ran_seconds=95, program_running=True,
                                         killed=[4242], confirmed=True, left=[], at=at.isoformat()))


def test_a_hand_swept_before_the_screen_screens_today(world):
    """A stray sandbox of the hand swept hours before this screen proves nothing about it: the piece goes in."""
    from datetime import datetime, timedelta, timezone
    sweep_record(world[0], 'hand-a', datetime.now(timezone.utc) - timedelta(hours=10))
    verdict, hands, runtime = one_piece(world, honest)
    assert 'not proved ended' not in verdict.reason, verdict.reason
    assert ('instruct', 'hand-a') in hands.calls, 'the piece went to the hand'


def test_a_hand_swept_during_its_piece_is_flagged(world):
    from datetime import datetime, timezone

    def swept_while_screening(hands, name, message_id, folder):
        sweep_record(hands.runtime, name, datetime.now(timezone.utc))
        honest(hands, name, message_id, folder)
    verdict, hands, runtime = one_piece(world, swept_while_screening)
    assert not verdict.clean and 'not proved ended after the piece' in verdict.reason, verdict.reason


class QdHands(pr_screen.LiveHands):
    """The live hands' own down, over a scripted qd: ``exits`` is what qd's down exits with, and ``reads`` the hand's
    activity after it, as (state, detail). Everything else is the stand-in's."""

    def __init__(self, stand_in, exits=0, reads=('off', 'down')):
        runtime = stand_in.runtime
        super().__init__(SimpleNamespace(runtime=runtime, folder=runtime.parent), Path('qd'), stand_in.root)
        self.stand_in, self.exits, self.reads, self.qd_calls = stand_in, exits, reads, []

    def _qd(self, *args, timeout=300):
        import subprocess
        self.qd_calls.append(args)
        assert args[:1] == ('down',), 'the down is proved from the hand itself, not from a qd reading'
        return subprocess.CompletedProcess(args, self.exits, '', 'refused' if self.exits else '')

    def _activity(self, name):
        from quarterdeck import watch
        return watch.Activity(*self.reads)

    def folder(self, name):
        return self.stand_in.folder(name)

    def home(self, name):
        return self.stand_in.home(name)

    def records(self, name):
        return self.stand_in.records(name)

    def kept_in_home(self, name, records):
        return self.stand_in.kept_in_home(name, records)

    def idle(self, name):
        return self.stand_in.idle(name)

    def fresh(self, name):
        self.stand_in.fresh(name)

    def launch_record(self, name):
        return self.stand_in.launch_record(name)

    def instruct(self, name, body):
        return self.stand_in.instruct(name, body)


NOT_PROVED_DOWN = [pytest.param(dict(exits=1), id='down exits nonzero'),
                   pytest.param(dict(exits=0, reads=('idle', '')), id='down exits 0 and the hand is still up'),
                   pytest.param(dict(exits=0, reads=('off', 'no pane')), id='off with no pane: its launch remains'),
                   pytest.param(dict(exits=0, reads=('off', 'exited 1')), id='off with a dead pane')]


@pytest.mark.parametrize('down', NOT_PROVED_DOWN)
def test_a_down_not_proved_leaves_the_hold_and_its_journal_until_a_later_recovery_finishes_it(world, down):
    runtime, hands_root = world
    stand_in = Hands(runtime, hands_root, honest)
    stand_in.launched = {}
    piece = stand_in.folder('hand-a') / 'screen-0123456789ab'
    piece.mkdir()
    (piece / 'f01.txt').write_text('the outsider\'s text, left by a killed screen\n')
    interrupted_hold(runtime, 'hand-a')
    journal = runtime / 'pr-screen' / 'journal' / 'hand-a.json'
    hands, outcomes = QdHands(stand_in, **down), Outcomes()
    with pytest.raises(pr_screen.ScreenError, match='hand-a: .*it stays held'):
        pr_screen.recover(runtime, hands, outcomes)
    assert pr_screen.held(runtime, 'hand-a') and journal.is_file(), 'the hold and its journal stand'
    assert piece.is_dir() and outcomes.told == [], 'nothing of the finish ran past the down'
    with pytest.raises(pr_screen.ScreenError, match='hand-a: .*it stays held'):
        pr_screen.recover(runtime, hands, outcomes)
    assert pr_screen.held(runtime, 'hand-a') and journal.is_file(), 'tried again, and held again'
    hands.exits, hands.reads = 0, ('off', 'down')
    finished = pr_screen.recover(runtime, hands, outcomes)
    assert [(f['number'], f['hand']) for f in finished] == [(7, 'hand-a')], 'the next recovery finishes it once'
    assert not pr_screen.held(runtime, 'hand-a') and not journal.exists() and not piece.exists()
    assert pr_screen.recover(runtime, hands, outcomes) == [] and len(outcomes.told) == 1


@pytest.mark.parametrize('down', NOT_PROVED_DOWN)
def test_a_screen_whose_down_is_not_proved_keeps_its_hold_and_journal(world, down):
    runtime, hands_root = world
    stand_in = Hands(runtime, hands_root, honest)
    hands = QdHands(stand_in, **down)
    prepared = pr_screen.prepare(texts('clean-docs'))
    clock = Clock()
    try:
        with pytest.raises(pr_screen.ScreenError, match='hand-a: .*it stays held'):
            pr_screen.screen_piece(runtime, hands, 'hand-a', 'key', 0, prepared, list(prepared.files),
                                   corpus=CORPUS, answer_seconds=30, poll=1.0, clock=clock, sleep=clock.sleep)
    finally:
        end_launch(getattr(stand_in, 'launched', {}).pop('hand-a', None))
    assert pr_screen.held(runtime, 'hand-a'), 'a hand not proved down is never released'
    assert (runtime / 'pr-screen' / 'journal' / 'hand-a.json').is_file(), 'its journal stands for the next run'
    assert ('down', 'hand-a') in hands.qd_calls and ('fresh after', 'hand-a') not in stand_in.calls


@pytest.mark.parametrize('launched, proved', [(None, True), (object(), False)], ids=['no launch', 'a launch remains'])
def test_the_live_down_reads_the_hand_beneath_its_hold(world, monkeypatch, launched, proved):
    """While the hold stands, qd's watch row reads 'screening'; the down reads the hand's own pane and launch."""
    import subprocess
    from quarterdeck import seat as seatmod
    from quarterdeck.tmux import Tmux
    runtime, hands_root = world
    interrupted_hold(runtime, 'hand-a', journal=False)
    monkeypatch.setattr(Tmux, 'for_ship', classmethod(lambda cls, ship: None))
    monkeypatch.setattr(seatmod, 'read_seat', lambda ship, spec, tmux: SimpleNamespace(
        name=spec.name, pane=lambda: None, launched=lambda: launched))
    ship = SimpleNamespace(runtime=runtime, folder=runtime.parent, seat=lambda name: SimpleNamespace(name=name))
    live = pr_screen.LiveHands(ship, Path('qd'), hands_root)
    monkeypatch.setattr(live, '_qd', lambda *args, timeout=300: subprocess.CompletedProcess(args, 0, '', ''))
    assert pr_screen.held(runtime, 'hand-a')
    if proved:
        live.down('hand-a')
    else:
        with pytest.raises(pr_screen.ScreenError, match='hand-a: qd down returned, but the hand reads off, no pane'):
            live.down('hand-a')


def run_source(world, source):
    runtime, hands_root = world
    hands, outcomes, clock = Hands(runtime, hands_root, honest), Outcomes(), Clock()
    results = pr_screen.run(runtime, source, hands, outcomes, hand_names=HANDS,
                            seat_names=('reviewer', 'desk', 'lead', *HANDS), corpus=CORPUS,
                            repository='owner/project', clock=clock, sleep=clock.sleep, poll=1.0,
                            answer_seconds=30, idle_seconds=30)
    return results, outcomes


@pytest.mark.parametrize('error, reason', [
    (OSError('the network is down'), r'it could not be read \(OSError\)'),
    (ValueError('not JSON'), r'it could not be read \(ValueError\)'),
    (pr_screen.ScreenError('/pulls/7/files: a page is over 8388608 bytes'), 'a page is over 8388608 bytes'),
])
def test_one_request_that_cannot_be_read_is_flagged_alone_and_the_pass_goes_on(world, error, reason):
    class OneFails(pr_screen.FixtureSource):
        def open(self):
            first, second = super().open()
            return [first, pr_screen.PullRequest(7, '07' * 20), second]

        def texts(self, pr):
            if pr.number == 7:
                raise error
            return super().texts(pr)

    results, outcomes = run_source(world, OneFails(FIXTURES, ['clean-docs', 'clean-code']))
    by_number = {result['number']: result for result in results}
    assert len(by_number) == 3 and 7 in by_number, 'every request is reached'
    failed = by_number[7]
    assert failed['outcome'] == 'flagged' and re.search(reason, failed['findings'][0])
    assert failed['findings'][0].startswith('not screened: ') and 'flagged unread' in failed['findings'][0]
    assert [told for told in outcomes.told if told[1] == 7] == [('desk', 7, failed['findings'])], 'named to the desk'
    others = [result for number, result in by_number.items() if number != 7]
    assert [result['outcome'] for result in others] == ['clean', 'clean'], 'the others are screened all the same'
    state = json.loads((world[0] / 'pr-screen' / 'state.json').read_text())
    assert state['7']['outcome'] == 'flagged' and state['7']['head'] == '07' * 20


class GitHub:
    """GitHub's REST answers by path, as bytes, through an opener: no network."""

    def __init__(self, pages):
        self.pages = pages

    def __call__(self, request, timeout):
        import io
        from urllib.parse import urlsplit
        url = urlsplit(request.full_url)
        path = url.path.removeprefix('/repos/owner/project')
        query = dict(part.split('=') for part in url.query.split('&'))
        answer = self.pages.get(path, [])
        if isinstance(answer, list):  # one page of it, as GitHub pages a listing
            per, page = int(query['per_page']), int(query['page'])
            answer = answer[(page - 1) * per:page * per]
        return io.BytesIO(answer if isinstance(answer, bytes) else json.dumps(answer).encode())


def github_request(number, files):
    head = f'{number:02d}' * 20
    return {f'/pulls/{number}': dict(head=dict(sha=head), title='A small fix', body='It fixes a word.'),
            f'/pulls/{number}/files': files}


@pytest.mark.parametrize('limit', ['a page over its size', 'more pages than are read'])
def test_a_request_github_answers_beyond_the_limits_is_flagged_alone(world, monkeypatch, limit):
    monkeypatch.setattr(pr_screen, 'PAGE_BYTES', 4096, raising=False)
    monkeypatch.setattr(pr_screen, 'PAGES', 3, raising=False)
    pages = {'/pulls': [dict(number=3, head=dict(sha='03' * 20)), dict(number=4, head=dict(sha='04' * 20))]}
    pages.update(github_request(3, [dict(filename='docs/a.md', patch='@@ -1 +1 @@\n-recieve\n+receive\n')]))
    if limit == 'a page over its size':  # whole, it would parse as an empty list: it is never read cut short
        pages.update(github_request(4, b'[' + b' ' * 8192 + b']'))
    else:  # a full page each time: more than are read
        pages.update(github_request(4, []))
        pages['/pulls/4/commits'] = [dict(commit=dict(message=f'Fix word {n}')) for n in range(10)]
    source = pr_screen.GitHubSource('owner/project', opener=GitHub(pages), page=1)
    results, outcomes = run_source(world, source)
    by_number = {result['number']: result for result in results}
    assert by_number[3]['outcome'] == 'clean', 'the other request is screened'
    assert by_number[4]['outcome'] == 'flagged' and by_number[4]['findings'][0].startswith('not screened: ')
    assert ('over 4096 bytes' if limit == 'a page over its size' else 'more than 3 pages') in by_number[4]['findings'][0]
