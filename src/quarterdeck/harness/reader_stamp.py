"""File-derived reader identities, independent of an installation's location.

These are semantic dependencies of record reading, not every module an adapter
imports for launching or controlling its program. Keep each set explicit when
moving a parser or adding a helper to its reading path. A quarterdeck module
a reader imports and that is not in its set is named beside the set as
launch-only or control-only, with the reason. Nothing is exempt.
"""

import hashlib
from pathlib import Path
import sys


COMMON = (
    'harness/reader_stamp.py', 'record_owner.py', 'harness/__init__.py', 'harness/base.py',
    'harness/types.py', 'harness/limits.py', 'harness/json_record.py',
    'harness/reading_scope.py', 'harness/compactions.py',
)
CODEX = ('harness/codex.py', 'harness/codex_usage.py', 'harness/failures.py')
CLAUDE = ('harness/claude.py', 'harness/claude_background.py',
          'harness/claude_limits.py', 'harness/claude_watch.py',
          'harness/failures.py', 'seat_command.py')
PI = ('harness/pi.py', 'harness/endpoint.py', 'harness/costs.py',
      'harness/failures.py', 'pricing.py')

MODULES = {
    'codex': tuple(sorted({*COMMON, *CODEX})),
    'claude': tuple(sorted({*COMMON, *CLAUDE})),
    'pi': tuple(sorted({*COMMON, *PI})),
    'opencode': tuple(sorted({*COMMON, 'harness/opencode.py',
                             'harness/opencode_record.py', 'harness/sqlite_snapshot.py',
                             'harness/endpoint.py', 'harness/costs.py', 'harness/failures.py',
                             'pricing.py'})),
    'antigravity': tuple(sorted({*COMMON, 'harness/antigravity.py',
                                'harness/antigravity_record.py', 'harness/antigravity_wire.py',
                                'harness/antigravity_quota.py', 'harness/antigravity_models.py',
                                'harness/sqlite_snapshot.py', 'pricing.py'})),
    'grok': tuple(sorted({*COMMON, 'harness/grok.py', 'harness/grok_record.py',
                          'harness/grok_usage.py'})),
    # The synthetic adapter delegates record formats to these three native readers.
    'fake': tuple(sorted({*COMMON, *CODEX, *CLAUDE, *PI, 'harness/fake.py'})),
}

# The verdict engine and its direct semantic imports. Native input interpretation
# is covered separately by MODULES; control/launch-only imports are not readers.
ATTRIBUTION = ('attribution_cache.py', 'order_attribution.py', 'input_receipt.py',
               'post.py', 'harness/__init__.py')

# Shared modules every reader imports, and that do not interpret a record.
_LAUNCH_SHARED = (
    ('ship.py', 'ship, pool and seat declaration; a reader takes the built spec and does not parse the ship file'),
    ('harness/home.py', 'credential home and program templates written at launch'),
    ('seat.py', 'seat launch and the program path; record ownership keeps the saved binding'),
)
_CONTROL_SHARED = (
    ('__init__.py', 'package entry for the status line and the CLI; readers load it only as their package'),
    ('providers.py', 'which program may run on a pool; records are not interpreted here'),
    ('harness/capabilities.py', 'model and effort selection for launch and controls'),
)
_CHECK = (
    ('harness/check_evidence.py', "qd check's tool evidence; turns are read by the record parser"),
)
_ENDPOINT = (
    ('endpoint.py', 'endpoint choice and key used to start the program'),
)
_DURABLE = (
    ('durable.py', 'replaces files when a price or a home is written; record bytes are read by the parser'),
)

# Named beside each reader set. A new quarterdeck import joins the set or one of these.
LAUNCH_ONLY = {
    'codex': _LAUNCH_SHARED,
    'claude': _LAUNCH_SHARED + (
        ('harness/claude_statusline.py', 'status-line command written into the home at launch'),
    ),
    'pi': _LAUNCH_SHARED + _ENDPOINT + _DURABLE + (
        ('harness/pi_thinking.py', 'thinking level applied at launch and restored by a client'),
    ),
    'opencode': _LAUNCH_SHARED + _ENDPOINT + _DURABLE + (
        ('harness/opencode_thinking.py', 'thinking written into the program config at launch'),
    ),
    'antigravity': _LAUNCH_SHARED + _DURABLE,
    'grok': _LAUNCH_SHARED + _DURABLE + (
        ('harness/grok_workspace.py', 'launch arguments and the workspace check before start'),
    ),
    'fake': _LAUNCH_SHARED + _ENDPOINT + _DURABLE + (
        ('harness/claude_statusline.py', 'status-line command written into the home at launch'),
        ('harness/pi_thinking.py', 'thinking level applied at launch and restored by a client'),
    ),
}
CONTROL_ONLY = {
    'codex': _CONTROL_SHARED + _CHECK,
    'claude': _CONTROL_SHARED + _CHECK,
    'pi': _CONTROL_SHARED + _CHECK,
    'opencode': _CONTROL_SHARED + _CHECK,
    'antigravity': _CONTROL_SHARED,
    'grok': _CONTROL_SHARED + (
        ('harness/grok_models.py', 'screen selection warnings and cached model availability; not record interpretation'),
        ('harness/grok_screen.py', 'screen, box and paste reading for the door'),
        ('harness/grok_notice.py', 'workspace inventory shown for the person; records are read by grok_record'),
    ),
    'fake': _CONTROL_SHARED + _CHECK + (
        ('request_queue.py', 'synthetic control origin; the fake record reader delegates to the native parsers'),
    ),
}


# Exact computed calls: module, enclosing scope, expression and reason. A
# classification covers this call, not every dynamic import in its module.
_ADAPTER_FACTORY = (
    'harness/__init__.py', '_adapter_class',
    'importlib.import_module(f".{module_name}", __package__)',
    'the fixed MODULES registry selects an adapter; each harness reader set hashes its selected adapter and delegated readers',
)
COMPUTED_IMPORTS = {name: (_ADAPTER_FACTORY,) for name in MODULES}


def files_stamp(modules, root=None):
    """Unknown/unreadable code grants no reuse; never hash paths or mtimes."""
    root = Path(root) if root is not None else Path(__file__).parent.parent
    digest = hashlib.sha256()
    try:
        for name in sorted(modules):
            digest.update(name.encode() + b'\0')
            digest.update((root / name).read_bytes() + b'\0')
    except OSError:
        return None
    return [list(sys.version_info[:3]), digest.hexdigest()]


def readers(root=None):
    """One bounded collection per accounting pass or status reading."""
    return {name: files_stamp(modules, root) for name, modules in MODULES.items()}


def attributions(root=None):
    engine = files_stamp(ATTRIBUTION, root)
    return {name: [key, engine] if key is not None and engine is not None else None
            for name, key in readers(root).items()}
