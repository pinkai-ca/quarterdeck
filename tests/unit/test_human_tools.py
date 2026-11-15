"""Native question policy follows actual session ownership after configuration layering."""

from dataclasses import replace
import json
import tomllib
import copy
from fnmatch import fnmatchcase

import pytest

from quarterdeck import endpoint, standing
from quarterdeck.seat import Seat
from quarterdeck.ship import Pool, SeatSpec, Session, Ship
from quarterdeck.ship import ShipError


def resolved(tmp_path, name, human, settings=None):
    pool = Pool('pool', name, endpoint='http://example.invalid/v1' if name == 'opencode' else None)
    spec = SeatSpec('worker', 'arbitrary', 'captain', name, ('pool',), settings=settings or {})
    ship = Ship(tmp_path, 'synthetic', 'operator', tmp_path, {'pool': pool},
                {'arbitrary': Session('arbitrary', 'Private', human=human)}, {'worker': spec})
    return Seat(ship, spec, None)


@pytest.mark.parametrize('human', [False, True])
@pytest.mark.parametrize('name', ['claude', 'codex', 'opencode'])
def test_native_question_policy_is_applied_after_root_profile_agent_and_wildcard_settings(tmp_path, monkeypatch, human, name):
    monkeypatch.setattr(endpoint, 'choose', lambda pool, model: endpoint.Choice(pool.name, 'synthetic', 8192, 1024))
    settings = {
        'claude': {'permissions': {'allow': ['AskUserQuestion', 'Read'], 'deny': ['Write']}, 'theme': 'dark'},
        'codex': {'tools': {'experimental_request_user_input': {'enabled': True}},
                  'profiles': {'custom': {'tools': {'experimental_request_user_input': {'enabled': True}}}},
                  'features': {'default_mode_request_user_input': True, 'collaboration_modes': True}},
        'opencode': {'permission': {'question': 'allow', 'q*': 'allow', '*': 'allow'}, 'tools': {'question': True},
                     'mode': {'legacy': {'permission': {'question': 'allow', '*': 'allow'}},
                              'custom': {'permission': {'*': 'allow'}}},
                     'agent': {'build': {'permission': '*'}, 'plan': {'permission': {'question': 'allow', '*': 'allow'}},
                               'custom': {'permission': {'q*': 'allow'}, 'tools': {'question': True, 'read': False}}}},
    }
    # Use a valid native scalar permission, deliberately broad, on a configured agent.
    settings['opencode']['agent']['build']['permission'] = 'allow'
    config = settings[name]
    before = copy.deepcopy(config)
    seat = resolved(tmp_path / name, name, human, config)
    seat.harness.build_home(seat.home, seat.spec, seat.pool, None)
    assert config == before, 'native policy must not mutate another session\'s declaration'
    brief = tmp_path / 'brief.txt'
    brief.write_text('Synthetic brief')
    launch = seat.harness.launch(seat.spec, seat.pool, seat.home, brief, None, None)
    if name == 'claude':
        out = json.loads((seat.home / 'settings.json').read_text())
        assert ('AskUserQuestion' in out['permissions']['deny']) is (not human)
        assert 'Write' in out['permissions']['deny'] and 'Read' in out['permissions']['allow']
    elif name == 'codex':
        out = tomllib.loads((seat.home / 'config.toml').read_text())
        for scope in [out, *out['profiles'].values()]:
            assert scope['tools']['experimental_request_user_input']['enabled'] is human
        assert out['features'] == config['features'], 'do not disable unrelated modes'
        assert ('tools.experimental_request_user_input.enabled=false' in launch.argv) is (not human)
    else:
        out = json.loads((seat.home / 'config/opencode/opencode.json').read_text())
        if human:
            assert out['permission'] == config['permission'] and out['agent'] == config['agent']
            assert out['tools'] == config['tools']
            assert 'OPENCODE_PERMISSION' not in launch.env
        else:
            # Pinned native ConfigAgent.normalize uses Object.assign; mode then
            # merges into agent. Existing keys keep their position in both steps.
            def native_permissions(scope):
                rules = {key: 'allow' if value else 'deny' for key, value in scope.get('tools', {}).items()}
                rules.update(scope.get('permission', {}))
                return rules
            root_rules = native_permissions(out)
            assert [action for pattern, action in root_rules.items() if fnmatchcase('question', pattern)][-1] == 'deny'
            for name in out['agent'].keys() | out['mode'].keys():
                rules = native_permissions(out['agent'].get(name, {}))
                rules.update(native_permissions(out['mode'].get(name, {})))
                matching = [action for pattern, action in [*root_rules.items(), *rules.items()] if fnmatchcase('question', pattern)]
                assert matching[-1] == 'deny', 'native normalization/legacy overlay must still remove question'
            assert out['agent']['custom']['tools']['read'] is False
            assert launch.env['OPENCODE_DISABLE_PROJECT_CONFIG'] == '1'
            assert json.loads(launch.env['OPENCODE_PERMISSION']) == {'question': 'deny'}


def test_human_policy_does_not_force_enable_an_explicit_denial_and_inline_choice_keeps_session(tmp_path):
    seat = resolved(tmp_path, 'claude', True, {'permissions': {'deny': ['AskUserQuestion']}})
    seat.harness.build_home(seat.home, seat.spec, seat.pool, None)
    assert 'AskUserQuestion' in json.loads((seat.home / 'settings.json').read_text())['permissions']['deny']
    spec = replace(seat.spec, pools=('pool', 'other'), pool_choices={
        'pool': {'harness': 'claude'}, 'other': {'harness': 'codex', 'settings': {}}})
    assert Seat(seat.ship, spec, None).spec.for_pool('other').human_session is True
    seat.ship.seats['worker'] = spec
    # Actual session ownership also survives selecting the other program on reconstruction.
    (seat.trusted / 'pool').write_text('other')
    seat.ship.pools['other'] = Pool('other', 'codex')
    assert Seat(seat.ship, spec, None).spec.human_session is True


def test_default_nonhuman_home_cannot_change_a_later_human_sessions_native_tools(tmp_path):
    from quarterdeck.harness.claude import SETTINGS

    before = copy.deepcopy(SETTINGS)
    nonhuman = resolved(tmp_path/'automated', 'claude', False)
    nonhuman.harness.build_home(nonhuman.home, nonhuman.spec, nonhuman.pool, None)
    assert 'AskUserQuestion' in json.loads((nonhuman.home/'settings.json').read_text())['permissions']['deny']
    human = resolved(tmp_path/'interactive', 'claude', True)
    human.harness.build_home(human.home, human.spec, human.pool, None)
    assert SETTINGS == before
    assert 'AskUserQuestion' not in json.loads((human.home/'settings.json').read_text())['permissions'].get('deny', [])


@pytest.mark.parametrize('name,phrase', [('antigravity', 'no supported native question-tool exclusion proved'),
                                        ('pi', 'enumerated core tools')])
def test_nonhuman_brief_states_the_supported_boundary_and_question_route(tmp_path, name, phrase):
    seat = resolved(tmp_path, name, False)
    text = standing.seat_section(seat.ship, seat.spec, False)
    assert phrase in text and 'qd post question' in text
    assert phrase not in standing.seat_section(seat.ship, seat.spec, True)


@pytest.mark.parametrize('rules,safe', [
    (None, True), ([{'permission': 'read', 'pattern': '*', 'action': 'allow'}], True),
    ([{'permission': 'q*', 'pattern': '*', 'action': 'allow'}], False),
    ([{'permission': 'question *', 'pattern': '*', 'action': 'allow'}], False),
    ([{'permission': '*', 'pattern': '*', 'action': 'allow'},
      {'permission': 'question', 'pattern': '*', 'action': 'deny'}], True),
    ([{'permission': 'question', 'pattern': 'specific', 'action': 'deny'}], False),
    ('malformed', False),
])
def test_native_saved_session_overrides_cannot_restore_question_on_nonhuman_resume(tmp_path, monkeypatch, rules, safe):
    from tests.unit.test_opencode_record import database, session

    monkeypatch.setattr(endpoint, 'choose', lambda pool, model: endpoint.Choice(pool.name, 'synthetic', 8192, 1024))
    seat = resolved(tmp_path, 'opencode', False)
    seat.harness.build_home(seat.home, seat.spec, seat.pool, None)
    record = seat.home / 'data-001/opencode/opencode.db'
    con = database(record)
    session(con)
    con.execute('ALTER TABLE session ADD COLUMN permission TEXT')
    raw = None if rules is None else rules if isinstance(rules, str) else json.dumps(rules)
    con.execute('UPDATE session SET permission=?', (raw,))
    con.commit()
    con.close()
    before = record.read_bytes()
    if safe:
        assert '--session' in seat.harness.launch(seat.spec, seat.pool, seat.home, tmp_path/'brief', 'root', None).argv
    else:
        with pytest.raises(ShipError, match='saved native session permissions may re-enable question'):
            seat.harness.launch(seat.spec, seat.pool, seat.home, tmp_path/'brief', 'root', None)
    assert seat.harness.launch(replace(seat.spec, human_session=True), seat.pool, seat.home, tmp_path/'brief', 'root', None)
    assert 'data-002' in seat.harness.launch(seat.spec, seat.pool, seat.home, tmp_path/'brief', None, None).env['XDG_DATA_HOME']
    assert record.read_bytes() == before, 'policy never rewrites existing native permissions'


@pytest.mark.parametrize('relative', ['.opencode/agents/custom.md', 'config/opencode/agent/custom.md',
                                    '.opencode/modes/custom.md', 'config/opencode/opencode.jsonc'])
def test_unmanaged_native_agent_scopes_refuse_nonhuman_launch_and_preserve_human_settings(tmp_path, monkeypatch, relative):
    monkeypatch.setattr(endpoint, 'choose', lambda pool, model: endpoint.Choice(pool.name, 'synthetic', 8192, 1024))
    seat = resolved(tmp_path, 'opencode', False)
    path = seat.home / relative
    path.parent.mkdir(parents=True)
    content = '---\ndescription: Synthetic agent\npermission:\n  question: allow\n---\nSynthetic prompt.\n' if path.suffix == '.md' else '{"agent":{"custom":{"permission":{"question":"allow"}}}}'
    path.write_text(content)
    with pytest.raises(ShipError, match='unmanaged native configuration'):
        seat.harness.build_home(seat.home, seat.spec, seat.pool, None)
    seat.harness.build_home(seat.home, replace(seat.spec, human_session=True), seat.pool, None)
    assert path.read_text() == content, 'never edit an unmanaged native configuration to enforce policy'


def test_native_scope_failure_is_not_certified_empty_and_empty_directories_remain_compatible(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(endpoint, 'choose', lambda pool, model: endpoint.Choice(pool.name, 'synthetic', 8192, 1024))
    seat = resolved(tmp_path, 'opencode', False)
    folder = seat.home/'config/opencode/agents'
    folder.mkdir(parents=True)
    original = Path.iterdir
    with monkeypatch.context() as patch:
        def unavailable(path):
            if path == folder:
                raise PermissionError('synthetic native scope unavailable')
            return original(path)
        patch.setattr(Path, 'iterdir', unavailable)
        with pytest.raises(PermissionError, match='native scope unavailable'):
            seat.harness.build_home(seat.home, seat.spec, seat.pool, None)
    seat.harness.build_home(seat.home, seat.spec, seat.pool, None)


def test_native_scope_symlink_is_refused_without_reading_its_target(tmp_path, monkeypatch):
    monkeypatch.setattr(endpoint, 'choose', lambda *args: pytest.fail('unmanaged native scope reached model discovery'))
    seat = resolved(tmp_path, 'opencode', False)
    seat.home.mkdir(parents=True)
    seat.trusted.mkdir(parents=True, exist_ok=True)
    target = tmp_path/'synthetic-native-config'
    target.mkdir()
    (seat.home/'.opencode').symlink_to(target, target_is_directory=True)
    with pytest.raises(ShipError, match='unmanaged native configuration'):
        seat.harness.build_home(seat.home, seat.spec, seat.pool, None)
