"""Ordinary synthetic launch assembly; no native program or tmux server starts."""

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from quarterdeck import carriage_plan as cp, post, recovery, standing
from quarterdeck.seat import Seat
from quarterdeck.ship import Pool, SeatSpec, Session, Ship, ShipError


def prepared(tmp_path, monkeypatch, *, policy=6000, profile=None, version='fake'):
    spec = SeatSpec('builder', 'crew', 'worker', 'fake', ('synthetic-pool',), sandbox=False)
    declaration = Ship(tmp_path, 'synthetic', 'operator', tmp_path,
                       {'synthetic-pool': Pool('synthetic-pool', 'synthetic')},
                       {'crew': Session('crew', 'Synthetic crew')}, {'builder': spec}, carry_bytes=policy)
    seat = Seat(declaration, spec, SimpleNamespace())
    seat.harness.readable_capacity = profile
    monkeypatch.setattr(seat, '_accept_pool', lambda: None)
    monkeypatch.setattr(recovery, 'require_clear', lambda _seat: None)
    monkeypatch.setattr(seat, '_observe_selection', lambda: (version, None))
    monkeypatch.setattr(seat, '_survives', lambda *_args: None)
    brief = tmp_path / 'synthetic-brief.md'
    brief.write_text('Invented standing instructions.\n')
    monkeypatch.setattr(standing, 'render', lambda *_args: brief)
    opened = []
    def open_window(argv, env, path):
        opened.append((argv, env, path))
        return SimpleNamespace(id='%synthetic-pane', pid=4321)
    monkeypatch.setattr(seat, '_open_window', open_window)
    return seat, opened


def publish_post(seat, name, body, *, sender='lead'):
    message = post.Message(id=name, kind='order', sender=sender, to='builder',
                           at=datetime(2020, 1, 1, tzinfo=timezone.utc), body=body, subject='Synthetic assignment')
    post.Store(seat.ship.runtime).write(message)
    return message


def first(opened):
    argv = opened[0][0]
    return argv[argv.index('--first') + 1]


def test_short_all_fit_launch_keeps_canonical_input(tmp_path, monkeypatch):
    seat, opened = prepared(tmp_path, monkeypatch)
    publish_post(seat, 'ordinary-short', 'Invented ordinary task.')
    expected = seat.carriage().text
    seat._up(resume=False)
    assert first(opened) == expected
    assert not (seat.folder / 'carried').exists()


def test_policy_launch_publishes_bounded_whole_and_overflow_before_window(tmp_path, monkeypatch):
    seat, opened = prepared(tmp_path, monkeypatch)
    messages = [publish_post(seat, 'handoff-order', 'Synthetic handoff.\n' * 30, sender='builder')]
    messages += [publish_post(seat, f'ordinary-large-{n}', 'Invented large assignment.\n' * 1200) for n in range(20)]
    originals = {path: path.read_bytes() for path in post.Store(seat.ship.runtime).dir.iterdir()}
    original_open = seat._open_window
    def inspect_open(*args):
        generations = list((seat.folder / 'carried').iterdir())
        assert len(generations) == 1
        assert (generations[0] / 'overflow.txt').is_file()
        return original_open(*args)
    monkeypatch.setattr(seat, '_open_window', inspect_open)
    seat._up(resume=False)
    generation, = (seat.folder / 'carried').iterdir()
    whole = (generation / 'whole.txt').read_text()
    assert 'Synthetic handoff.' in whole
    assert cp.size(first(opened)) + cp.size(whole) <= 6000
    assert all(path.stat().st_size <= 6000 for path in generation.iterdir())
    assert 'policy bound' in first(opened)
    assert 'overflow need not be read' in first(opened).lower()
    assert {path: path.read_bytes() for path in originals} == originals
    attempts = post.Attempts(seat.ship.runtime).all()
    assert {entry.message for entry in attempts} == {message.id for message in messages}
    assert {entry.result for entry in attempts} == {'launch'}


@pytest.mark.parametrize('version,uses_files', [('fake', True), ('other-version', False), (None, False)])
def test_exact_measured_ceiling_only_reduces_policy(tmp_path, monkeypatch, version, uses_files):
    profile = cp.ReadableCapacity('fake', 6000, 'synthetic measurement', 'synthetic brief and turn reserve')
    seat, opened = prepared(tmp_path, monkeypatch, policy=100000, profile=profile, version=version)
    publish_post(seat, 'larger-than-ceiling', 'ordinary synthetic words\n' * 500)
    seat._up(resume=False)
    assert (seat.folder / 'carried').exists() is uses_files
    assert bool(opened)
    if uses_files:
        assert 'measured adapter ceiling' in first(opened)


def test_impossible_account_stops_before_generation_or_window(tmp_path, monkeypatch):
    seat, opened = prepared(tmp_path, monkeypatch, policy=100)
    publish_post(seat, 'large-for-tiny-policy', 'ordinary words\n' * 1000)
    with pytest.raises(ShipError, match='minimal first-message account'):
        seat._up(resume=False)
    assert opened == []
    assert not (seat.folder / 'carried').exists()
    assert post.Attempts(seat.ship.runtime).all() == []
