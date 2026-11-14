"""File-derived reader identities, independent of an installation's location.

These are semantic dependencies of record reading, not every module an adapter
imports for launching or controlling its program. Keep each set explicit when
moving a parser or adding a helper to its reading path.
"""

import hashlib
from pathlib import Path
import sys


COMMON = (
    'harness/reader_stamp.py', 'record_owner.py', 'harness/base.py',
    'harness/types.py', 'harness/limits.py', 'harness/json_record.py',
    'harness/reading_scope.py',
)
CODEX = ('harness/codex.py', 'harness/codex_usage.py',
         'harness/compactions.py', 'harness/failures.py')
CLAUDE = ('harness/claude.py', 'harness/claude_background.py',
          'harness/claude_limits.py', 'harness/claude_watch.py',
          'harness/compactions.py', 'harness/failures.py', 'seat_command.py')
PI = ('harness/pi.py', 'harness/endpoint.py', 'harness/costs.py',
      'harness/failures.py', 'pricing.py')

MODULES = {
    'codex': tuple(sorted({*COMMON, *CODEX})),
    'claude': tuple(sorted({*COMMON, *CLAUDE})),
    'pi': tuple(sorted({*COMMON, *PI})),
    'opencode': tuple(sorted({*COMMON, 'harness/opencode.py',
                             'harness/opencode_record.py', 'harness/sqlite_snapshot.py',
                             'harness/endpoint.py', 'harness/costs.py', 'pricing.py'})),
    'antigravity': tuple(sorted({*COMMON, 'harness/antigravity.py',
                                'harness/antigravity_record.py', 'harness/antigravity_wire.py',
                                'harness/antigravity_quota.py', 'harness/sqlite_snapshot.py',
                                'pricing.py'})),
    'grok': tuple(sorted({*COMMON, 'harness/grok.py', 'harness/grok_record.py'})),
    # The synthetic adapter delegates record formats to these three native readers.
    'fake': tuple(sorted({*COMMON, *CODEX, *CLAUDE, *PI, 'harness/fake.py'})),
}

# The verdict engine and its direct semantic imports. Native input interpretation
# is covered separately by MODULES; control/launch-only imports are not readers.
ATTRIBUTION = ('attribution_cache.py', 'order_attribution.py', 'input_receipt.py',
               'post.py', 'harness/__init__.py')


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
