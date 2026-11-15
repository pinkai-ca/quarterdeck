"""[pr_screen] in the ship file: the screen's two hands and its reader, judged when the ship file is loaded."""

from __future__ import annotations

import pytest

from quarterdeck import pr_screen, ship as shipmod
from quarterdeck.ship import ShipError

BASE = '''
[ship]
name = "example"
operator = "operator"

[pools.fake-a]
provider = "synthetic"

[pools.fake-b]
provider = "synthetic"

[sessions.crew]
title = "Crew"

[seats.hand-a]
session = "crew"
role = "hand"
harness = "fake"
pool = "fake-a"
model = "model-a"
sandbox = true

[seats.hand-b]
session = "crew"
role = "hand"
harness = "fake"
pool = "fake-b"
model = "model-b"
sandbox = true

[seats.hand-c]
session = "crew"
role = "hand"
harness = "fake"
pool = "fake-a"
model = "model-c"
sandbox = true

[seats.loose]
session = "crew"
role = "hand"
harness = "fake"
pool = "fake-b"
model = "model-d"

[seats.reviewer]
session = "crew"
role = "first-officer"
harness = "fake"
pool = "fake-a"

[pools.subscription]
provider = "openai"

[seats.refusing-reader]
session = "crew"
role = "reviewer"
harness = "codex"
pool = "subscription"

[seats.refusing-hand]
session = "crew"
role = "hand"
harness = "codex"
pool = "subscription"
sandbox = true

[seats.desk]
session = "crew"
role = "desk"
harness = "fake"
pool = "fake-a"
'''


def load(tmp_path, table: str):
    (tmp_path / 'ship.toml').write_text(BASE + table)
    return shipmod.load(tmp_path)


def test_two_jailed_hands_on_two_models_and_a_reader_are_the_screen(tmp_path):
    ship = load(tmp_path, '\n[pr_screen]\nhands = ["hand-a", "hand-b"]\nreader = "reviewer"\n')
    assert ship.pr_screen.hands == ('hand-a', 'hand-b') and ship.pr_screen.reader == 'reviewer'
    assert ship.pr_screen_desk == 'desk', 'the seat whose role is desk, unless desk names another'


def test_no_table_is_no_screen_said_plainly(tmp_path, capsys):
    ship = load(tmp_path, '')
    assert ship.pr_screen is None and ship.pr_screen_desk is None
    assert pr_screen.main(['--ship', str(tmp_path), '--repository', 'owner/project']) == 2
    assert 'declares no [pr_screen] table' in capsys.readouterr().err


@pytest.mark.parametrize('table, refusal', [
    ('hands = ["hand-a"]\nreader = "reviewer"', 'exactly two different seats'),
    ('hands = ["hand-a", "hand-b", "hand-c"]\nreader = "reviewer"', 'exactly two different seats'),
    ('hands = ["hand-a", "hand-a"]\nreader = "reviewer"', 'exactly two different seats'),
    ('hands = ["hand-a", "nobody"]\nreader = "reviewer"', "'nobody', which is not a declared seat"),
    ('hands = ["hand-a", "loose"]\nreader = "reviewer"', 'loose is not jailed'),
    ('hands = ["hand-a", "hand-c"]\nreader = "reviewer"', 'run on the same model'),
    ('hands = ["hand-a", "hand-b"]\nreader = "hand-a"', 'must not be one of its hands'),
    ('hands = ["hand-a", "hand-b"]\nreader = "refusing-reader"', "safeguard can refuse, and so lose"),
    ('hands = ["hand-a", "refusing-hand"]\nreader = "reviewer"', "hands: refusing-hand runs on .* safeguard can refuse"),
    ('hands = ["hand-a", "hand-b"]', 'reader must name a declared seat'),
    ('hands = ["hand-a", "hand-b"]\nreader = "reviewer"\nextra = 1', 'pr_screen takes hands'),
])
def test_a_table_that_does_not_fit_is_refused_at_load(tmp_path, table, refusal):
    with pytest.raises(ShipError, match=refusal):
        load(tmp_path, f'\n[pr_screen]\n{table}\n')
