"""Grok 1.0.77 selection diagnostics and its cached model list, read only.

Display labels, accepted native ids and billing keys are separate namespaces.
The cache supplies no alias from a display label or a billing key to an id.
"""

import json


MODEL_REFUSAL = 'Couldn\'t switch model: Invalid params: "unknown model id"'
EFFORT_REFUSAL = '--effort/--reasoning-effort: current model does not support reasoning effort'


def selection_warning(text):
    """Only the complete warning rows measured on 1.0.77; no inferred error grammar."""
    rows = {line.strip() for line in text.splitlines()}
    return '; '.join(word for word in (MODEL_REFUSAL, EFFORT_REFUSAL) if word in rows) or None


def listed(home, *, version=None):
    """A complete measured cache, or unknown. Never return account/connection fields."""
    if version not in (None, '1.0.77'):
        return None
    try:
        data = json.loads((home / 'models_cache.json').read_text())
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(data, dict) or data.get('grok_version') != '1.0.77':
        return None
    models = data.get('models')
    if not isinstance(models, dict) or not models:
        return None
    result = {}
    for key, entry in models.items():
        info = entry.get('info') if isinstance(entry, dict) else None
        if not isinstance(info, dict) or info.get('id') != key or info.get('model') != key:
            return None
        name = info.get('name')
        if any(not isinstance(value, str) or not value or value != value.strip()
               or len(value) > 200 or not value.isprintable() for value in (key, name)):
            return None
        result[key] = name
    return result
