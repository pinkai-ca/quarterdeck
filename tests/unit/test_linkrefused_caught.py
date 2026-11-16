"""A link a seat's jail plants where the service writes is refused (seat_files.LinkRefused, a ShipError), and each
site that handled only an OSError now handles that refusal as it does an OSError: named in its own words, never
raised out of an act, a bring-up or an ownership reading. Every link is planted in the test's own throwaway ship."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from quarterdeck import acts, credential, record_owner, seat_files
from .test_systems_acts import DIALOG, ship  # noqa: F401

DECOY = "a file outside the seat the service must never write through\n"
REFUSED = 'a link where the service appends; nothing written through it'


CALLER = 'systems-synthetic'


def systems(svc):
    """A systems seat of the fixture's own form, under a name of the test's own."""
    spec = next(spec for spec in svc.ship.seats.values() if spec.role == 'systems')
    svc.ship.seats[CALLER] = replace(spec, name=CALLER)
    return [spec.name for spec in svc.ship.seats.values() if spec.role == 'desk']


def decoy(tmp_path: Path) -> Path:
    path = tmp_path / 'outside' / 'decoy.txt'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DECOY)
    return path


def test_an_act_whose_record_is_a_planted_link_is_refused_and_nothing_sent(ship, tmp_path):  # noqa: F811
    svc, seat, live = ship
    desks = systems(svc)
    outside = decoy(tmp_path)
    acts.path(seat).symlink_to(outside)
    line = svc.answer(dict(kind="press", seat="worker", choice="Continue", caller=CALLER))
    assert line.startswith("refused: the act could not be recorded (") and REFUSED in line
    assert line.endswith("; nothing is pressed")
    live["screen"] = "› \n\n  test low · /work\n"
    line = svc.answer(dict(kind="type", seat="worker", line="go on", caller=CALLER))
    assert line.startswith("refused: the act could not be recorded (") and line.endswith("; nothing is typed")
    assert live["sent"] == [] and outside.read_text() == DECOY


def test_an_outcome_whose_record_became_a_planted_link_is_named_after_the_act(ship, tmp_path):  # noqa: F811
    svc, seat, live = ship
    desks = systems(svc)
    outside = decoy(tmp_path)

    def plant(kind, value):  # between the act's line and its outcome, the record turns into a link
        live.update(screen="• Working (1s • esc to interrupt)\n› \n\n  test low · /work\n")
        acts.path(seat).unlink()
        acts.path(seat).symlink_to(outside)

    live["after"] = plant
    line = svc.answer(dict(kind="press", seat="worker", choice="Continue", caller=CALLER))
    assert live["sent"] == [("keys", ("1",))], 'pressed once, as recorded before'
    assert line.startswith("worker: pressed 'Continue' by key 1") and "; its outcome could not be recorded (" in line
    assert REFUSED in line and outside.read_text() == DECOY


def test_a_desk_notice_whose_note_is_a_planted_link_is_posted_and_named(ship, tmp_path):  # noqa: F811
    svc, seat, live = ship
    desks = systems(svc)
    outside = decoy(tmp_path)
    acts.notices(seat).symlink_to(outside)
    live["screen"] = DIALOG.replace("Synthetic question from the program?", "Do you trust the authors of this project?")
    line = svc.answer(dict(kind="press", seat="worker", choice="Continue", caller=CALLER))
    assert "the desk is told (" in line and "but the notice could not be noted (" in line and REFUSED in line
    assert [m.to for m in svc.store.all()] == desks and outside.read_text() == DECOY


def test_a_bring_up_through_a_planted_folder_link_is_retained_and_named(tmp_path):
    seat_home = tmp_path / 'runtime' / 'seats' / 'synthetic' / 'home'
    seat_home.mkdir(parents=True)
    outside = tmp_path / 'outside-folder'
    outside.mkdir()
    (outside / '.credentials.json').write_text(DECOY)
    (seat_home / '.claude').symlink_to(outside, target_is_directory=True)  # the jail's link where a folder belongs
    source = tmp_path / 'pool' / '.credentials.json'
    source.parent.mkdir()
    source.write_text('{"synthetic": "pool credential"}\n')
    said = credential._bring_up(source, seat_home / '.claude' / '.credentials.json', 'synthetic', 'synthetic-pool')
    assert said.startswith('synthetic copy not brought up to synthetic-pool: ') and said.endswith('; retained')
    assert 'link' in said
    assert sorted(p.name for p in outside.iterdir()) == ['.credentials.json'] and (outside / '.credentials.json').read_text() == DECOY


def test_an_ownership_list_behind_a_planted_home_link_is_a_transient_unknown(ship, tmp_path):  # noqa: F811
    svc, seat, live = ship
    outside = tmp_path / 'outside-home'
    outside.mkdir()
    (outside / 'conversations.owned').write_text(str(outside / 'decoy.jsonl') + '\n')
    if seat.home.exists():
        seat.home.rmdir()
    seat.home.symlink_to(outside, target_is_directory=True)
    [owned] = record_owner.records(seat)
    assert owned.transient and owned.reader is None and 'link' in owned.reason
    assert f'inspect ownership for {seat.name} with qd watch {seat.name}' in owned.reason
