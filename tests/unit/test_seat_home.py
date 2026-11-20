"""Every seat's home is its owner's alone: made 0700 for every program, mended to 0700 at launch.

The step proves the home an owned real directory beneath its opened parent, opens it without following a link and
sets its mode on that descriptor. A link in its place, a folder swapped in after the proof, or a mode that will not
hold refuses and changes nothing; a launch that expects the home built refuses an absent one in a line naming it.
Nothing here runs a program or reads a credential.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from quarterdeck import durable, endpoint, seat_files, seat_home
from quarterdeck.ship import Pool, SeatSpec, ShipError


@pytest.fixture(autouse=True)
def ship_umask():
    """The live ship's umask (0002): without the step, every home it made was 0775."""
    previous = os.umask(0o002)
    try:
        yield
    finally:
        os.umask(previous)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def seat_folder(tmp_path: Path, name: str = 'synthetic-seat') -> Path:
    folder = tmp_path / 'ship' / 'runtime' / 'seats' / name
    folder.mkdir(parents=True)
    return folder


def behind(elsewhere: Path) -> dict:
    return {str(item.relative_to(elsewhere)): (stat.S_IMODE(item.lstat().st_mode),
                                               item.read_bytes() if item.is_file() else None)
            for item in (elsewhere, *elsewhere.rglob('*'))}


def linked_home(tmp_path: Path) -> tuple[Path, Path]:
    home = seat_folder(tmp_path) / 'home'
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    (elsewhere / 'settings.json').write_text('{"synthetic": true}\n')
    os.chmod(elsewhere, 0o755)
    home.symlink_to(elsewhere, target_is_directory=True)
    return home, elsewhere


# -- the step itself ---------------------------------------------------------------------------------------------

def test_an_absent_home_is_made_owner_only(tmp_path):
    home = seat_folder(tmp_path) / 'home'
    seat_home.owner_only(home)
    assert home.is_dir() and not home.is_symlink() and mode(home) == 0o700


def test_an_absent_seat_folder_is_made_and_the_home_in_it_owner_only(tmp_path):
    home = tmp_path / 'ship' / 'runtime' / 'seats' / 'synthetic-seat' / 'home'
    seat_home.owner_only(home)
    assert mode(home) == 0o700


@pytest.mark.parametrize('widened', [0o755, 0o777, 0o775, 0o750])
def test_a_widened_home_is_mended_and_its_contents_are_left_as_they_are(tmp_path, widened):
    home = seat_folder(tmp_path) / 'home'
    home.mkdir()
    (home / 'inner').mkdir()
    os.chmod(home / 'inner', 0o755)
    (home / 'settings.json').write_text('{}\n')
    os.chmod(home, widened)
    seat_home.owner_only(home)
    assert mode(home) == 0o700
    assert mode(home / 'inner') == 0o755, 'what a program makes inside its home is left as it makes it'
    assert (home / 'settings.json').read_text() == '{}\n'


def test_a_home_already_owner_only_is_not_written(tmp_path, monkeypatch):
    home = seat_folder(tmp_path) / 'home'
    home.mkdir(mode=0o700)
    calls = []
    monkeypatch.setattr(seat_home.os, 'fchmod', lambda *args: calls.append(args))
    seat_home.owner_only(home)
    assert calls == [] and mode(home) == 0o700


@pytest.mark.parametrize('built', [False, True])
def test_a_link_in_place_of_the_home_is_refused_and_nothing_behind_it_changes(tmp_path, built):
    home, elsewhere = linked_home(tmp_path)
    before = behind(elsewhere)
    with pytest.raises(ShipError, match='not an owned real directory'):
        seat_home.owner_only(home, built=built)
    assert behind(elsewhere) == before and mode(elsewhere) == 0o755
    assert home.is_symlink()


def test_a_dangling_link_in_place_of_the_home_is_refused_and_nothing_is_made(tmp_path):
    home = seat_folder(tmp_path) / 'home'
    target = tmp_path / 'never-made'
    home.symlink_to(target, target_is_directory=True)
    with pytest.raises(ShipError, match='not an owned real directory'):
        seat_home.owner_only(home)
    assert not target.exists() and home.is_symlink()


def test_a_file_in_place_of_the_home_is_refused(tmp_path):
    home = seat_folder(tmp_path) / 'home'
    home.write_text('not a folder\n')
    os.chmod(home, 0o644)
    with pytest.raises(ShipError, match='not an owned real directory'):
        seat_home.owner_only(home)
    assert mode(home) == 0o644 and home.read_text() == 'not a folder\n'


def test_a_link_swapped_in_after_the_proof_is_refused_by_the_descriptor(tmp_path, monkeypatch):
    home = seat_folder(tmp_path) / 'home'
    home.mkdir()
    os.chmod(home, 0o755)
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    (elsewhere / 'settings.json').write_text('{}\n')
    os.chmod(elsewhere, 0o755)
    real_open = os.open

    def swapping_open(path, flags, *args, dir_fd=None, **kwargs):
        if dir_fd is not None and path == 'home' and not home.is_symlink():
            home.rmdir()  # between stat's proof and the open
            home.symlink_to(elsewhere, target_is_directory=True)
        return real_open(path, flags, *args, dir_fd=dir_fd, **kwargs)

    monkeypatch.setattr(seat_home.os, 'open', swapping_open)
    before = behind(elsewhere)
    with pytest.raises(ShipError, match='not an owned real directory'):
        seat_home.owner_only(home)
    assert behind(elsewhere) == before


def test_a_folder_swapped_in_after_the_proof_is_refused_and_left_as_it_is(tmp_path, monkeypatch):
    home = seat_folder(tmp_path) / 'home'
    home.mkdir()
    os.chmod(home, 0o755)
    real_open = os.open
    swapped = []

    def swapping_open(path, flags, *args, dir_fd=None, **kwargs):
        if dir_fd is not None and path == 'home' and not swapped:
            home.rename(home.with_name('home-proved'))
            home.mkdir()
            os.chmod(home, 0o755)
            swapped.append(True)
        return real_open(path, flags, *args, dir_fd=dir_fd, **kwargs)

    monkeypatch.setattr(seat_home.os, 'open', swapping_open)
    with pytest.raises(ShipError, match='changed between its proof and its opening'):
        seat_home.owner_only(home)
    assert mode(home) == 0o755


def test_a_mode_that_will_not_hold_refuses(tmp_path, monkeypatch):
    home = seat_folder(tmp_path) / 'home'
    home.mkdir()
    os.chmod(home, 0o755)
    monkeypatch.setattr(seat_home.os, 'fchmod', lambda descriptor, mode: None)
    with pytest.raises(ShipError, match='still not mode 0700'):
        seat_home.owner_only(home)


def test_the_mode_is_set_through_the_descriptor_never_by_path(tmp_path, monkeypatch):
    home = seat_folder(tmp_path) / 'home'
    home.mkdir()
    os.chmod(home, 0o777)

    def refused(*args, **kwargs):
        raise AssertionError('chmod by path')

    monkeypatch.setattr(seat_home.os, 'chmod', refused)
    monkeypatch.setattr(Path, 'chmod', refused)
    seat_home.owner_only(home)
    assert mode(home) == 0o700


@pytest.mark.parametrize('missing', ['home', 'seat folder'])
def test_an_absent_home_where_one_is_expected_is_refused_in_a_line_that_names_it(tmp_path, missing):
    folder = tmp_path / 'ship' / 'runtime' / 'seats' / 'synthetic-seat'
    if missing == 'home':
        folder.mkdir(parents=True)
    home = folder / 'home'
    with pytest.raises(seat_home.Absent) as raised:
        seat_home.owner_only(home, built=True)
    message = str(raised.value)
    assert str(home) in message and 'absent' in message and '\n' not in message
    assert not home.exists() and not home.is_symlink()
    assert folder.exists() == (missing == 'home'), 'nothing is made for a home that should have been there'


def test_the_read_only_check_names_an_absent_home_and_leaves_a_present_one_alone(tmp_path):
    home = seat_folder(tmp_path) / 'home'
    with pytest.raises(seat_home.Absent, match=str(home)):
        seat_home.require_built(home)
    home.mkdir()
    os.chmod(home, 0o755)
    seat_home.require_built(home)
    assert mode(home) == 0o755, 'the read-only check changes nothing; the launch mends'


# -- the seat code makes every home 0700 ---------------------------------------------------------------------------

def test_the_no_link_walk_makes_a_seats_home_owner_only_and_nothing_else(tmp_path):
    folder = seat_folder(tmp_path)
    seat_files.make_folder(folder / 'home' / 'config' / 'program')
    assert mode(folder / 'home') == 0o700
    assert mode(folder / 'home' / 'config') == 0o775 and mode(folder / 'home' / 'config' / 'program') == 0o775
    seat_files.make_folder(folder / 'carriage' / 'home')
    assert mode(folder / 'carriage') == 0o775 and mode(folder / 'carriage' / 'home') == 0o775


def test_a_file_written_into_an_absent_home_makes_the_home_owner_only(tmp_path):
    home = seat_folder(tmp_path) / 'home'
    durable.replace_file(home / 'conversations.owned', 'synthetic\n')
    assert mode(home) == 0o700


def build_claude(tmp_path, home):
    from quarterdeck.harness.claude import ClaudeHarness
    pool_dir = tmp_path / 'pool'
    pool_dir.mkdir()
    (pool_dir / '.credentials.json').write_text('{}')
    seat = SeatSpec('synthetic-seat', 'crew', 'worker', 'claude', ('synthetic-pool',), cwd=tmp_path / 'work')
    ClaudeHarness().build_home(home, seat, Pool('synthetic-pool', 'claude', config_dir=pool_dir), None)


def build_codex(tmp_path, home):
    from quarterdeck.harness.codex import CodexHarness
    pool_dir = tmp_path / 'pool'
    pool_dir.mkdir()
    (pool_dir / 'auth.json').write_text('{}')
    seat = SeatSpec('synthetic-seat', 'crew', 'worker', 'codex', ('synthetic-pool',), cwd=tmp_path / 'work')
    CodexHarness().build_home(home, seat, Pool('synthetic-pool', 'codex', config_dir=pool_dir), None)


def served(monkeypatch):
    monkeypatch.setattr(endpoint, 'models', lambda url, timeout=0, **kwargs: [endpoint.Served('synthetic-model', 98304)])


def build_pi(tmp_path, home, monkeypatch):
    from quarterdeck.harness.pi import PiHarness
    served(monkeypatch)
    seat = SeatSpec('synthetic-seat', 'crew', 'worker', 'pi', ('local-a',), model='synthetic-model', cwd=tmp_path / 'work')
    PiHarness().build_home(home, seat, Pool('local-a', 'local', endpoint='http://127.0.0.1:8000/v1'), None)


def build_opencode(tmp_path, home, monkeypatch):
    from quarterdeck.harness.opencode import OpenCodeHarness
    served(monkeypatch)
    seat = SeatSpec('synthetic-seat', 'crew', 'worker', 'opencode', ('local-a',), model='synthetic-model',
                    cwd=tmp_path / 'work')
    OpenCodeHarness().build_home(home, seat, Pool('local-a', 'local', endpoint='http://127.0.0.1:8000/v1'), None)


def build_antigravity(tmp_path, home):
    from quarterdeck.harness.antigravity import AntigravityHarness
    seat = SeatSpec('synthetic-seat', 'crew', 'worker', 'antigravity', ('google',), model='gemini-4-argon-high',
                    cwd=tmp_path / 'work')
    AntigravityHarness().build_home(home, seat, Pool('google', 'antigravity'), None)


def build_grok(tmp_path, home, monkeypatch):
    from quarterdeck.harness import grok, grok_notice
    from tests.conftest import synthetic_grok
    synthetic_grok(tmp_path, monkeypatch)
    monkeypatch.setattr(grok, 'SYSTEM_CONFIG', tmp_path / 'synthetic-system')
    monkeypatch.setattr(grok_notice, 'trust_scope', lambda cwd, environment: (cwd, False))
    for name in grok.INHERITED_REDIRECTS:
        monkeypatch.delenv(name, raising=False)
    pool_home = tmp_path / 'synthetic-pool'
    pool_home.mkdir()
    (pool_home / 'auth.json').write_text(json.dumps({'synthetic-grant': {
        'refresh_token': 'synthetic-refresh', 'expires_at': '2040-02-01T00:00:00Z'}}))
    seat = SeatSpec('synthetic-seat', 'crew', 'worker', 'grok', ('synthetic-plan',), cwd=tmp_path)
    grok.GrokHarness().build_home(home, seat, Pool('synthetic-plan', 'xai', config_dir=pool_home), None)


def build_muse(tmp_path, home, monkeypatch):
    from quarterdeck.harness.muse import MuseHarness
    monkeypatch.setattr('quarterdeck.harness.muse_program.binary',
                        lambda **kwargs: Path('/synthetic/bin/muse-bin-1.4.20-R1.1'))
    template = tmp_path / 'template'
    template.mkdir()
    (template / 'settings.json').write_text(json.dumps(dict(provider='echo', model='synthetic-catalog-model')))
    seat = SeatSpec('synthetic-seat', 'crew', 'worker', 'muse', ('synthetic-pool',), cwd=tmp_path / 'work')
    MuseHarness().build_home(home, seat, Pool('synthetic-pool', 'meta', config_dir=tmp_path / 'unused'), template)


def build_fake(tmp_path, home):
    from quarterdeck.harness.fake import FakeHarness
    seat = SeatSpec('synthetic-seat', 'crew', 'worker', 'fake', ('fake',), cwd=tmp_path / 'work')
    FakeHarness().build_home(home, seat, Pool('fake', 'synthetic'), None)


BUILDERS = {'claude': build_claude, 'codex': build_codex, 'pi': build_pi, 'opencode': build_opencode,
            'antigravity': build_antigravity, 'grok': build_grok, 'muse': build_muse, 'fake': build_fake}


def build(program, tmp_path, home, monkeypatch):
    builder = BUILDERS[program]
    if 'monkeypatch' in builder.__code__.co_varnames:
        builder(tmp_path, home, monkeypatch)
    else:
        builder(tmp_path, home)


def test_every_program_is_covered():
    from quarterdeck import harness
    assert set(BUILDERS) == set(harness.MODULES)


@pytest.mark.parametrize('program', sorted(BUILDERS))
def test_each_programs_own_build_makes_its_home_owner_only(tmp_path, monkeypatch, program):
    home = seat_folder(tmp_path) / 'home'
    (tmp_path / 'work').mkdir()
    build(program, tmp_path, home, monkeypatch)
    assert home.is_dir() and mode(home) == 0o700


@pytest.mark.parametrize('program', sorted(BUILDERS))
def test_each_programs_home_as_a_launch_makes_it_is_owner_only_and_a_rebuild_keeps_it(tmp_path, monkeypatch, program):
    home = seat_folder(tmp_path) / 'home'
    (tmp_path / 'work').mkdir()
    seat_home.owner_only(home)  # what every launch does before the program's build (Seat._up)
    build(program, tmp_path, home, monkeypatch)
    assert mode(home) == 0o700
    os.chmod(home, 0o775)  # a home the seat code made before this change
    seat_home.owner_only(home, built=True)
    assert mode(home) == 0o700


def test_muses_launch_refuses_an_absent_home_at_once_in_the_line_that_names_it(tmp_path, monkeypatch):
    from quarterdeck.harness.muse import MuseHarness
    monkeypatch.setattr('quarterdeck.harness.muse_program.binary',
                        lambda **kwargs: Path('/synthetic/bin/muse-bin-1.4.20-R1.1'))
    home = seat_folder(tmp_path) / 'home'
    brief = tmp_path / 'brief.txt'
    brief.write_text('A fictional brief.')
    seat = SeatSpec('synthetic-seat', 'crew', 'worker', 'muse', ('synthetic-pool',), cwd=tmp_path,
                    model='synthetic-catalog-model')
    read = []
    real_read = Path.read_text
    monkeypatch.setattr(Path, 'read_text', lambda self, *a, **k: read.append(self) or real_read(self, *a, **k))
    with pytest.raises(seat_home.Absent) as raised:
        MuseHarness().launch(seat, Pool('synthetic-pool', 'meta'), home, brief, None, None)
    assert str(home) in str(raised.value) and '\n' not in str(raised.value)
    assert not home.exists()
    assert not any(path.is_relative_to(home) for path in read), 'refused before any file in it is read'
