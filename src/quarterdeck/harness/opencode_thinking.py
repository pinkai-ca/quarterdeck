"""OpenCode 1.19.5's sole Qwen variant: proved off AND return to original requests."""

import json
import os
import re
from ..ship import ShipError

from .thinking import declaration, digest, json_file, sources, unchanged

MODEL = 'qwen3.8-27b'
OFF = 'qd-recovery-off'
OUTPUT_CAP = 'OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX'
FILES = ('config/opencode/opencode.json',)
OVERRIDE = {'chat_template_kwargs': {'enable_thinking': False}}
CAP_KEYS = {'max_tokens', 'max_completion_tokens', 'max_output_tokens', 'maxOutputTokens', 'maxTokens', 'outputTokenMax'}
THINKING_KEYS = {'thinking', 'reasoning_effort', 'reasoningEffort', 'thinking_budget', 'reasoningBudget'}


def competing(value, keys):
    if isinstance(value, dict):
        return any(k in keys or competing(v, keys) for k, v in value.items())
    return isinstance(value, list) and any(competing(v, keys) for v in value)


def request_options(config, pool, model):
    provider = config['provider'][pool]
    return [provider.get('options', {}), provider['models'][model].get('options', {}),
            config.get('agent', {}), config.get('mode', {})]


def prepare(config, pool, model, human):
    selected = config['provider'][pool.name]['models'][model]
    if pool.output is not None:
        if competing(request_options(config, pool.name, model), CAP_KEYS):
            raise ShipError('opencode: output declaration conflicts with native request-budget options; keep one output allowance in the pool')
        selected.setdefault('limit', {})['output'] = pool.output
    if (not human and model == MODEL and not selected.get('variants')
            and selected.get('options', {}).get('chat_template_kwargs', {}).get('enable_thinking') is True):
        selected['variants'] = {OFF: OVERRIDE}


def settings(home, pool, model):
    config = json_file(home / FILES[0])
    if (config is None or model != MODEL or config.get('plugin') or config.get('plugins')
            or config.get('keybinds', {}).get('variant_cycle', 'ctrl+t') != 'ctrl+t'):
        return None
    for scope in ('agent', 'mode'):
        if not isinstance(config.get(scope, {}), dict):
            return None
        if any(not isinstance(a, dict) or a.get('variant') for a in config.get(scope, {}).values()):
            return None
    # Native TUI configuration outside the generated JSON has not been proved.
    if any((home / folder / name).exists() for folder in ('.opencode', 'config/opencode')
           for name in ('tui.json', 'tui.jsonc')):
        return None
    provider = config.get('provider', {}).get(pool)
    if not isinstance(provider, dict) or provider.get('npm') != '@ai-sdk/openai-compatible':
        return None
    selected = provider.get('models', {}).get(model)
    if (not isinstance(selected, dict) or selected.get('variants') != {OFF: OVERRIDE}
            or selected.get('variants', {}).get(OFF, {}).get('chat_template_kwargs', {}).get('enable_thinking') is not False
            or selected.get('options', {}).get('chat_template_kwargs', {}).get('enable_thinking') is not True):
        return None
    if competing(request_options(config, pool, model), CAP_KEYS | THINKING_KEYS):
        return None  # preserve an unfamiliar request override; never claim a known cap/control for it
    limit = selected.get('limit', {}).get('output')
    if type(limit) is not int or limit <= 0:
        return None
    return dict(model=model, provider=pool, output=limit, original='default')


def launch_evidence(home, seat, pool, choice, env):
    if pool.output is not None:
        env[OUTPUT_CAP] = str(pool.output)  # explicit pool allowance, after native configuration layering
    found = None if seat.human_session else settings(home, pool.name, choice.model)
    if found is None:
        return None
    inherited = os.environ.get(OUTPUT_CAP)
    native_llm = os.environ.get('OPENCODE_EXPERIMENTAL_NATIVE_LLM')
    if (native_llm not in (None, '0', 'false')
            or (pool.output is None and (found['output'] > 32000 or inherited not in (None, str(found['output']))))):
        return None  # preserve an unproven native cap/path, rather than pretend to know its request
    env[OUTPUT_CAP] = str(found['output'])
    env['OPENCODE_EXPERIMENTAL_NATIVE_LLM'] = '0'  # do not inherit a tmux server's different native request path
    hashes = sources(home, FILES)
    if hashes is None:
        return None
    found['declaration'] = declaration(seat, pool)
    return dict(format=1, configuration=hashes, settings=found,
                environment={OUTPUT_CAP: env[OUTPUT_CAP], 'OPENCODE_EXPERIMENTAL_NATIVE_LLM': '0'})


class Client:
    continuation = ('The last assistant generation exhausted its output allowance with thinking and no answer.\n'
        'Continue the accepted work from the retained conversation with thinking disabled.\n'
        'Use completed tool results; do not repeat the original request or completed actions.\n'
        'If the work is already complete, give its final answer. This is the only automatic recovery.')

    def __init__(self, home, evidence):
        self.home, self.evidence = home, evidence
        self.settings = evidence['settings']
        self.signature = digest(evidence)
        self.off_keys = self.restore_keys = ('C-t',)

    def eligible(self, capped):
        limit = self.settings['output']
        # These are two exact native accounting shapes, not a character-derived
        # estimate or a generic length retry. Loopback accounting proof preserves
        # completion8192/reasoning8191 as output1 despite no returned text part.
        return (capped.model == self.settings['model'] and capped.provider == self.settings['provider']
                and (capped.reasoning, capped.written) in ((limit, 0), (limit - 1, 1)))

    def accepted_input(self, record, text, since, conversation):
        from ..input_receipt import same_text
        from .opencode_record import _Rows, _active_session, _ms
        rows = _Rows(record)
        if _active_session(rows) != conversation:
            return None
        matches = []
        for mid, sid, _, _, data in rows.messages:
            at = _ms(data.get('time', {}).get('created'))
            own = [p for _, pid, session, _, _, p in rows.parts if pid == mid and session == sid]
            if (sid == conversation and data.get('role') == 'user' and at is not None and at >= since
                    and own and all(p.get('type') == 'text' and isinstance(p.get('text'), str) for p in own)
                    and same_text('\n'.join(p['text'] for p in own), text)):
                matches.append(mid)
        return matches[0] if len(matches) == 1 else None

    def mode(self, state, screen):
        if state.model != self.settings['model']:
            return None
        path = self.home / 'state/opencode/model.json'
        try:
            if path.is_symlink():
                return None
            saved = json.loads(path.read_text())
        except FileNotFoundError:
            saved = {}
        except (OSError, ValueError):
            return None
        if not isinstance(saved, dict) or not isinstance(saved.get('variant', {}), dict):
            return None
        selected = saved.get('variant', {}).get(f"{self.settings['provider']}/{self.settings['model']}", 'default')
        if selected not in ('default', OFF):
            return None
        shown = re.search(r'^\s*┃  \S+ \S+ · ' + re.escape(self.settings['model'] + ' ' + self.settings['provider'])
                          + r'(?: · (?P<variant>\S+))?\s*$', screen.text, re.M)
        if shown is None:
            return None
        if selected == OFF and shown['variant'] == OFF:
            return 'off'
        if selected == 'default' and shown['variant'] is None:
            return 'original'
        return None


def client(home, saved, version, seat, pool):
    if version != '1.19.5' or saved is None or not unchanged(home, saved, FILES):
        return None
    found = saved['settings']
    current = settings(home, found.get('provider'), found.get('model'))
    if current is not None:
        current['declaration'] = declaration(seat, pool)
    if (set(found) != {'model', 'provider', 'output', 'original', 'declaration'} or found.get('original') != 'default'
            or found != current
            or saved['environment'].get(OUTPUT_CAP) != str(found['output'])
            or saved['environment'].get('OPENCODE_EXPERIMENTAL_NATIVE_LLM') not in (None, '0', 'false')):
        return None
    return Client(home, saved)
