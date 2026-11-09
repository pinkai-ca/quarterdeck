"""Pi 0.74.2 Qwen thinking cycles with explicit native request compatibility."""

from .thinking import declaration, digest, json_file, sources, unchanged

MODEL = 'qwen3.8-27b'
FILES = ('agent/models.json',)
LEVELS = ('off', 'minimal', 'low', 'medium', 'high')


def options(home):
    config = json_file(home / 'agent/settings.json')
    if config is None or config.get('thinkingBudgets') or (home / 'agent/keybindings.json').exists():
        return None
    # These two keys are written by this exact native client, including during
    # a thinking control. Every other setting remains bound byte-semantically.
    return {k: v for k, v in config.items() if k not in ('defaultThinkingLevel', 'lastChangelogVersion')}


def settings(home, provider, model, level):
    config = json_file(home / FILES[0])
    if config is None or model != MODEL or level not in LEVELS[1:]:
        return None
    selected_provider = config.get('providers', {}).get(provider)
    if not isinstance(selected_provider, dict) or selected_provider.get('api') != 'openai-completions':
        return None
    models = selected_provider.get('models', [])
    if not isinstance(models, list):
        return None
    matches = [m for m in models if isinstance(m, dict) and m.get('id') == model]
    if len(matches) != 1:
        return None
    selected = matches[0]
    if (selected.get('reasoning') is not True or selected.get('thinkingLevelMap')
            or selected.get('compat', {}).get('thinkingFormat') != 'qwen-chat-template'):
        return None
    output = selected.get('maxTokens')
    opts = options(home)
    if type(output) is not int or output <= 0 or opts is None:
        return None
    return dict(model=model, provider=provider, output=output, original=level, options=digest(opts))


def launch_evidence(home, seat, pool, choice):
    if seat.human_session:
        return None
    found = settings(home, pool.name, choice.model, seat.effort)
    current = json_file(home / 'agent/settings.json')
    hashes = sources(home, FILES)
    if (found is None or hashes is None or current is None
            or current.get('defaultThinkingLevel') not in (None, seat.effort)):
        return None
    found['declaration'] = declaration(seat, pool)
    return dict(format=1, configuration=hashes, settings=found, environment={})


class Client:
    continuation = 'Continue once; reuse results, never repeat completed actions.'

    def __init__(self, home, evidence):
        self.home, self.evidence = home, evidence
        self.settings = evidence['settings']
        self.signature = digest(evidence)
        index = LEVELS.index(self.settings['original'])
        self.off_keys = ('BTab',) * (len(LEVELS) - index)
        self.restore_keys = ('BTab',) * index

    def eligible(self, capped):
        # This is NOT a separate measured reasoning count. The reader proves
        # thinking-only content; Pi's unseparated output must exactly reach the cap.
        return (capped.model == self.settings['model'] and capped.provider == self.settings['provider']
                and capped.reasoning is None and capped.written == self.settings['output'])

    def accepted_input(self, record, text, since, conversation):
        from ..input_receipt import same_text
        from .pi import _session, _text
        from .json_record import entries, when
        header = _session(record)
        if header is None or header.get('id') != conversation:
            return None
        matches = []
        for entry in entries(record):
            message = entry.get('message')
            at = when(entry.get('timestamp'))
            if (entry.get('type') == 'message' and isinstance(message, dict) and message.get('role') == 'user'
                    and at is not None and at >= since and same_text(_text(message.get('content')), text)
                    and isinstance(entry.get('id'), str)):
                matches.append(entry['id'])
        return matches[0] if len(matches) == 1 else None

    def mode(self, state, screen):
        current = json_file(self.home / 'agent/settings.json')
        if (current is None or state.model != self.settings['model'] or screen.shows.get('model') != state.model
                or screen.shows.get('effort') != state.effort
                or current.get('defaultThinkingLevel') not in (None, state.effort)):
            return None
        if state.effort == self.settings['original']:
            return 'original'
        if state.effort == 'off':
            return 'off'
        return None


def client(home, saved, version, seat, pool):
    if version != '0.74.2' or saved is None or not unchanged(home, saved, FILES):
        return None
    found = saved['settings']
    current = settings(home, found.get('provider'), found.get('model'), found.get('original'))
    if current is not None:
        current['declaration'] = declaration(seat, pool)
    if (set(found) != {'model', 'provider', 'output', 'original', 'options', 'declaration'} or saved['environment']
            or found != current):
        return None
    return Client(home, saved)
