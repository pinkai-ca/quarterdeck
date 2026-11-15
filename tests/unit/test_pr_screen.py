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

    def fresh(self, name):
        # Started for a piece under the hold; started again after it, once the hold is released and the piece gone.
        self.calls.append(('fresh', name) if pr_screen.held(self.runtime, name) else ('fresh after', name))

    def down(self, name):
        assert pr_screen.held(self.runtime, name), 'the hand goes down before the hold is released'
        self.calls.append(('down', name))

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
                  'TURNSTONE', 'authorized_keys', 'IGNORE_PREVIOUS', 'Reviewer bot', 'already screened', 'recieve')


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
