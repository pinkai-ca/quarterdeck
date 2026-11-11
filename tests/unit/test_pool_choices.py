"""Inline choices own their program options; legacy names retain seat defaults."""

import pytest

from quarterdeck.ship import ShipError, load


def declaration(tmp_path, choices):
    (tmp_path / 'ship.toml').write_text('''[ship]
name="synthetic"
[pools.first]
provider="openai"
[pools.second]
provider="anthropic"
[pools.third]
provider="anthropic"
[sessions.crew]
[seats.worker]
session="crew"
harness="codex"
model="synthetic-source"
effort="high"
pool=''' + choices + '''
[seats.worker.settings]
source_only=true
''')
    return load(tmp_path)


def test_explicit_program_options_do_not_inherit_the_previous_program(tmp_path):
    ship = declaration(tmp_path, '["first", {pool="second",harness="claude",model="synthetic-target"}]')
    spec = ship.seat('worker')
    target = spec.for_pool('second')
    assert spec.pools == ('first', 'second')
    assert (target.harness, target.model, target.effort, target.settings) == ('claude', 'synthetic-target', None, {})
    assert target.for_pool('first').model == 'synthetic-source'
    assert target.for_pool('first').settings == {'source_only': True}


@pytest.mark.parametrize('choices, reason', [
    ('["first", "first"]', 'unique'),
    ('["first", {pool="first",harness="codex",model="other"}]', 'unique'),
    ('["first", {pool="second",harness="claude"}]', 'explicit harness and model'),
    ('["first", {pool="second",harness="codex",model="other"}]', 'subscription for Anthropic; use its native harness claude'),
    ('["first", {pool="missing",harness="claude",model="other"}]', 'not declared'),
    ('["first", {pool="second",harness="claude",model="other",settings=17}]', 'settings'),
])
def test_ambiguous_or_incompatible_choices_are_refused(tmp_path, choices, reason):
    with pytest.raises(ShipError, match=reason):
        declaration(tmp_path, choices)


def test_existing_scalar_and_string_list_declarations_keep_their_defaults(tmp_path):
    for choice in ('"first"', '["first"]'):
        spec = declaration(tmp_path, choice).seat('worker')
        assert spec.for_pool('first') == spec and not spec.pool_choices
