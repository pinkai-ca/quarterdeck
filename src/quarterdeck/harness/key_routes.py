"""How each program takes an API key at launch: a table of names, never a value (keyfile, key_launch).

A program that is not here takes no key. No record reader needs this, and it is in no reader's stamp set.
"""

import json
import re

from .. import durable, seat_files
from ..keyfile import ENDPOINT_VARIABLE, HELPER_VARIABLE, KeyProblem, REFUSED, Route

ZAI_MESSAGES = 'https://api.z.ai/api/anthropic'
ZAI_CHAT = 'https://api.z.ai/api/paas/v4'
ZAI_MODELS = f'{ZAI_CHAT}/models'


def zai_route(pool):
    if pool is None or pool.provider != 'zai' or not pool.paid_by_key or not pool.endpoint:
        return None
    base = pool.endpoint.rstrip('/')
    return base if base in (ZAI_MESSAGES, ZAI_CHAT) else None


def zai_model(name) -> bool:
    return isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9._:/][A-Za-z0-9._:/-]{0,127}', name) is not None


def zai_accepts(program, pool, seat=None) -> str | None:
    base = zai_route(pool)
    messages = program == 'claude' and base == ZAI_MESSAGES
    chat = program in ('opencode', 'pi') and base == ZAI_CHAT
    if (messages or chat) and seat is not None and not zai_model(seat.model):
        return f'pool {pool.name}: {program} on Z.ai needs an explicit model ID for this account'
    if messages:
        if seat is not None and seat.remote_control:
            return f'pool {pool.name}: Claude Remote Control on Z.ai is unproved'
        return None
    if chat:
        return None
    return f'pool {pool.name}: {program} has no supported Z.ai key route at the declared address'


def prepare_zai(program, pool, home) -> None:
    """Give Claude its Messages address and check the merged Chat provider before the key is injected."""
    base = zai_route(pool)
    if base is None:
        return
    refused = KeyProblem(REFUSED, f'pool {pool.name}: remove authentication and routing overrides from the program settings')
    if program == 'claude':
        path = home / 'settings.json'
        settings = json.loads(seat_files.read_text(path))
        env = settings.get('env', {})
        if (not isinstance(env, dict) or {'forceLoginMethod', 'forceLoginOrgUUID'} & settings.keys()
                or settings.get('skipWebFetchPreflight') is not True or settings.get('autoUploadSessions', False) is not False
                or any(name.startswith(('ANTHROPIC_', 'CLAUDE_CODE_USE_')) or name in
                       ('CLAUDE_CODE_OAUTH_TOKEN', HELPER_VARIABLE) for name in env)):
            raise refused
        env.update(ANTHROPIC_BASE_URL=base, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',
                   CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL='1')
        settings['env'] = env
        durable.replace_file(path, json.dumps(settings, indent=2) + '\n')
        return
    if program == 'opencode':
        settings = json.loads(seat_files.read_text(home / 'config/opencode/opencode.json'))
        from .opencode import zai_route_matches

        if not zai_route_matches(settings, pool):
            raise refused
    if program == 'pi':
        settings = json.loads(seat_files.read_text(home / 'agent/models.json'))
        provider = settings['providers'][pool.name]
        if (provider.get('api') != 'openai-completions' or provider.get('baseUrl') != pool.endpoint
                or provider.get('apiKey') != ENDPOINT_VARIABLE or 'headers' in provider
                or any({'api', 'baseUrl', 'headers'} & model.keys() for model in provider['models'])):
            raise refused

ROUTES = {
    # Measured on 2.1.317: its usual variable stops at a question whose preselected answer is No, and answering it
    # would keep the key's last 20 characters in its state. Its helper setting asks nothing and keeps nothing.
    'claude': Route(HELPER_VARIABLE, helper=True, state_file='.claude.json', state_account='oauthAccount'),
    # Measured on 0.166.0: the variable is ignored and it stops at its sign-in; its own key login is what it reads.
    'codex': Route(None, login=('login', '--with-api-key'), login_home='CODEX_HOME', login_file='auth.json'),
    # Measured on 1.0.92: the variable is taken, with no sign-in screen.
    'grok': Route('XAI_API_KEY'),
    'opencode': Route(ENDPOINT_VARIABLE),
    'pi': Route(ENDPOINT_VARIABLE),
    'fake': Route(HELPER_VARIABLE),
}
