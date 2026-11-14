"""Claude gates for the desk raising a ship: the person's consent and who may give it, on a throwaway project with a
throwaway office. Every bring-up is a stand-in, in this process and in any child, so no crew ever starts.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

from quarterdeck import raise_ship, seat as seatmod, setup, ship
from quarterdeck.ship import ShipError
from tests.conftest import ROOT
from tests.unit.test_raise_ship import args, project  # noqa: F401  (a throwaway office; Operations.up is a stand-in)


def as_desk(monkeypatch, office, seat='desk'):
    monkeypatch.setenv('QD_SHIP', str(office.folder))
    monkeypatch.setenv('QD_SEAT', seat)


def records(office, name='mission'):
    folder = office.runtime / 'raise' / name
    return sorted(p.name for p in folder.glob('*.json')) if folder.exists() else []


def tree(folder: Path) -> dict:
    return {str(p.relative_to(folder)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in folder.rglob('*') if p.is_file() and '.git' not in p.relative_to(folder).parts}


# -- the desk's raise of a new proposal only asks -----------------------------------------------------------------

def test_the_desks_raise_of_a_new_proposal_returns_the_whole_request_and_raises_nothing(project, monkeypatch):
    root, office, calls = project
    as_desk(monkeypatch, office)
    lines = raise_ship.run(args(), interactive=True, read=lambda _: pytest.fail('the desk is never prompted'),
                           write=lambda _: pytest.fail('nothing is shown as a prompt'))
    draft = raise_ship.proposal(args(), office)
    assert lines[0] == draft.summary and draft.files['ship.toml'] in lines[0], 'the complete declaration'
    assert 'pending the person' in lines[1]
    assert lines[2].endswith(f'--approve {draft.digest} --accept yes') and 'QD_SEAT absent' in lines[2]
    assert not (root / 'ships').exists() and calls == []
    assert records(office) == [f'{draft.digest}.request.json'], 'a request, and no answer'


# -- only the person's shell approves; a seat never supplies its own yes ---------------------------------------------

@pytest.mark.parametrize('answer', ['yes', 'no'])
def test_a_seat_cannot_answer_by_flag(project, monkeypatch, answer):
    root, office, calls = project
    as_desk(monkeypatch, office)
    with pytest.raises(ShipError, match='cannot answer for the person'):
        raise_ship.run(args('shuttle', 'mission', '--accept', answer), interactive=False)
    assert not (root / 'ships').exists() and calls == []
    assert not any('answer' in name for name in records(office))


def test_a_seat_cannot_answer_by_writing_the_answer_file_itself(project, monkeypatch):
    root, office, calls = project
    as_desk(monkeypatch, office)
    raise_ship.run(args(), interactive=False)
    digest = raise_ship.proposal(args(), office).digest
    # The desk writes the same record the person's yes would leave, then names the digest.
    forged = office.runtime / 'raise' / 'mission' / f'{digest}.answer-00000001.json'
    forged.write_text(json.dumps({'format': 1, 'digest': digest, 'answer': 'yes', 'by': 'person'}, sort_keys=True,
                                 indent=2) + '\n')
    try:
        raise_ship.run(args('shuttle', 'mission', '--approve', digest), interactive=False)
    except ShipError:
        pass
    assert not (root / 'ships' / 'mission').exists() and calls == [], 'a yes the desk wrote itself raised a ship'


PROGRAM = '''
import os, subprocess, sys, time
from pathlib import Path
go = Path(sys.argv[1])
while not go.exists():
    time.sleep(0.01)
env = {k: v for k, v in os.environ.items() if k not in ('QD_SEAT', 'QD_SHIP')}
child = """
import sys
from quarterdeck import cli, setup
setup.Operations.up = lambda self, current: ['synthetic crew up']
sys.exit(cli.main(sys.argv[1:]))
"""
sys.exit(subprocess.run([sys.executable, '-c', child, *sys.argv[2:]], env=env).returncode)
'''


def test_a_seat_cannot_answer_by_dropping_its_own_environment(project, monkeypatch, tmp_path):
    """The desk's program runs qd raise with QD_SEAT and QD_SHIP removed: it is still the desk, not the person."""
    root, office, calls = project
    go = tmp_path / 'go'
    command = ['--ship', str(office.folder), 'raise', 'shuttle', 'mission', '--pool', 'anthropic=claude',
               '--pool', 'openai=codex', '--accept', 'yes']
    env = dict(os.environ, QD_SHIP=str(office.folder), QD_SEAT='desk',
               PYTHONPATH=os.pathsep.join(filter(None, [str(ROOT / 'src'), str(ROOT), os.environ.get('PYTHONPATH')])))
    program = subprocess.Popen([sys.executable, '-c', PROGRAM, str(go), *command], env=env)
    try:
        # The desk's launch record names this program, as qd up writes it.
        start = seatmod.process_start(program.pid)
        folder = office.runtime / 'seats' / 'desk'
        folder.mkdir(parents=True, exist_ok=True)
        seatmod.Launched(pane='%1', pid=program.pid, at=time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime()),
                         pool='claude', resume=None, argv=['claude'], version=None, harness='claude',
                         start=start).write(folder / 'launch.json')
        go.write_text('')
        code = program.wait(60)
    finally:
        if program.poll() is None:
            program.kill()
            program.wait()
    assert not (root / 'ships' / 'mission').exists(), 'the desk raised a ship on a yes it gave itself'
    assert code != 0
    assert not any('answer' in name for name in records(office))


def test_a_persons_shell_without_an_answer_is_never_taken_as_yes(project):
    root, office, calls = project
    lines = raise_ship.run(args(), interactive=False)
    assert 'pending the person' in lines[1] and not (root / 'ships').exists() and calls == []


# -- the exact answer is recorded, an exact approval reused, any difference asks again ---------------------------------

def test_the_exact_answer_is_recorded_and_only_the_exact_proposal_reuses_a_yes(project, monkeypatch):
    root, office, calls = project
    digest = raise_ship.proposal(args(), office).digest
    raise_ship.run(args('shuttle', 'mission', '--accept', 'no'), interactive=False)
    saved = [json.loads((office.runtime / 'raise' / 'mission' / n).read_text()) for n in records(office) if 'answer' in n]
    assert saved == [{'format': 1, 'digest': digest, 'answer': 'no', 'by': 'person'}]
    assert not (root / 'ships').exists()
    as_desk(monkeypatch, office)
    assert 'declined' in raise_ship.run(args('shuttle', 'mission', '--approve', digest), interactive=False)[1]
    assert not (root / 'ships').exists() and calls == []

    monkeypatch.delenv('QD_SEAT')
    monkeypatch.delenv('QD_SHIP')
    raise_ship.run(args('shuttle', 'mission', '--approve', digest, '--accept', 'yes'), interactive=False)
    answers = [json.loads((office.runtime / 'raise' / 'mission' / n).read_text())['answer']
               for n in records(office) if 'answer' in n]
    assert answers == ['no', 'yes'], 'each answer kept, none rewritten'
    assert calls and calls[-1].folder == root / 'ships' / 'mission'

    # Any difference is a new proposal: asked again, never covered by the earlier yes.
    as_desk(monkeypatch, office)
    changed = args('shuttle', 'mission', '--model', 'captain=another-model')
    other = raise_ship.proposal(changed, office).digest
    assert other != digest
    with pytest.raises(ShipError, match='proposal changed'):
        raise_ship.run(args('shuttle', 'mission', '--model', 'captain=another-model', '--approve', digest),
                       interactive=False)
    lines = raise_ship.run(changed, interactive=False)
    assert 'pending the person' in lines[1] and f'--approve {other}' in lines[2]


# -- a crew seat of any ship is refused outright -----------------------------------------------------------------------

def crew_ship(root: Path) -> Path:
    folder = root / 'ships' / 'drydock-copy'
    shutil.copytree(ROOT / 'ships' / 'drydock', folder)
    return folder


@pytest.mark.parametrize('seat', ['captain', 'desk', 'worker-1'])
def test_a_seat_of_a_crew_ship_is_refused_outright_even_one_named_desk(project, monkeypatch, seat):
    root, office, calls = project
    folder = crew_ship(root)
    monkeypatch.setenv('QD_SHIP', str(folder))
    monkeypatch.setenv('QD_SEAT', seat)
    for extra in ((), ('--ship', str(office.folder))):
        parsed = args()
        if extra:
            parsed.ship = extra[1]
        with pytest.raises(ShipError):
            raise_ship.run(parsed, interactive=False)
    assert records(office) == [] and calls == [] and sorted(p.name for p in (root / 'ships').iterdir()) == ['drydock-copy']


def test_an_office_seat_that_is_not_the_desk_and_a_seat_without_its_ship_are_refused(project, monkeypatch):
    root, office, calls = project
    with office.file.open('a') as stream:
        stream.write('\n[seats.counsel]\nsession = "office"\nrole = "counsel"\nharness = "claude"\npool = "claude"\n')
    office = ship.load(office.folder)
    as_desk(monkeypatch, office, 'counsel')
    with pytest.raises(ShipError, match='only the person or the office desk'):
        raise_ship.run(args(), interactive=False)
    monkeypatch.delenv('QD_SHIP')
    monkeypatch.setenv('QD_SEAT', 'desk')
    with pytest.raises(ShipError):
        raise_ship.run(args(), interactive=False)
    assert records(office) == [] and calls == []


# -- a matching ship is resumed without edits, a differing one refused, nothing overwritten -----------------------------

def test_a_matching_ship_resumes_untouched_and_a_differing_or_foreign_one_is_refused_untouched(project):
    root, office, calls = project
    raise_ship.run(args('shuttle', 'mission', '--accept', 'yes'), interactive=False)
    folder = root / 'ships' / 'mission'
    head, before = ship._git(folder, 'rev-parse', 'HEAD'), tree(folder)
    assert 'resuming without edits' in raise_ship.run(args(), interactive=False)[0]
    assert tree(folder) == before and ship._git(folder, 'rev-parse', 'HEAD') == head

    orders = folder / 'orders.md'
    orders.write_text(orders.read_text() + '\nA line the person added.\n')
    edited = tree(folder)
    with pytest.raises(ShipError, match='nothing is overwritten'):
        raise_ship.run(args(), interactive=False)
    assert tree(folder) == edited and ship._git(folder, 'rev-parse', 'HEAD') == head

    foreign = root / 'ships' / 'elsewhere'
    foreign.mkdir()
    (foreign / 'notes.txt').write_text('not a ship of the office\n')
    snapshot = tree(foreign)
    with pytest.raises(ShipError):
        raise_ship.run(args('shuttle', 'elsewhere', '--accept', 'yes'), interactive=False)
    assert tree(foreign) == snapshot and not (foreign / '.git').exists()


# -- the person's pools and sign-ins are referenced, never copied --------------------------------------------------------

def test_pools_are_referenced_by_path_and_no_sign_in_is_copied(project, tmp_path):
    root, office, calls = project
    marker = 'synthetic-credential-marker-not-for-any-ship'
    signin = tmp_path / 'person-signins'
    for name in ('claude', 'codex'):
        (signin / name).mkdir(parents=True)
        (signin / name / 'auth.json').write_text(json.dumps({'synthetic': marker}))
    with office.file.open('a') as stream:
        stream.write(f'\n[pools.claude-two]\nprovider = "anthropic"\nconfig_dir = "{signin / "claude"}"\n'
                     f'\n[pools.codex-two]\nprovider = "openai"\nconfig_dir = "{signin / "codex"}"\n')
    office = ship.load(office.folder)
    parsed = args('shuttle', 'mission', '--accept', 'yes')
    parsed.pool = ['anthropic=claude-two', 'openai=codex-two']
    raise_ship.run(parsed, interactive=False)
    folder = root / 'ships' / 'mission'
    raised = ship.load(folder)
    assert {p.config_dir for p in raised.pools.values()} == {signin / 'claude', signin / 'codex'}
    assert not any(p.is_symlink() for p in folder.rglob('*'))
    holding = [p for p in folder.rglob('*') if p.is_file() and marker.encode() in p.read_bytes()]
    holding += [p for p in office.runtime.rglob('*') if p.is_file() and marker.encode() in p.read_bytes()]
    assert holding == [], 'a sign-in was copied'


@pytest.mark.parametrize('key', ['file:/synthetic/key', 'xai-synthetic-literal-key'])
def test_a_key_pool_is_raised_only_by_its_environment_reference(project, key):
    root, office, calls = project
    with office.file.open('a') as stream:
        stream.write(f'\n[pools.keyed]\nprovider = "anthropic"\napi_key = "{key}"\n')
    office = ship.load(office.folder) if key.startswith('file:') else office
    parsed = args('shuttle', 'mission', '--accept', 'yes')
    parsed.pool = ['anthropic=keyed', 'openai=codex']
    with pytest.raises(ShipError):
        raise_ship.run(parsed, interactive=False)
    assert not (root / 'ships').exists() and calls == []
