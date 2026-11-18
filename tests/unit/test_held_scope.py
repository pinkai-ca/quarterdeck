"""Display the complete scope from an ordinary formatted quota reason."""

import pytest

from quarterdeck import quota, watch
from quarterdeck.harness.limits import Wall


@pytest.mark.parametrize(('harness', 'model', 'reason', 'expected'), [
    ('antigravity', 'Gemini 4 Argon (Low)', 'individual quota reached', 'model Gemini 4 Argon (Low)'),
    ('antigravity', 'gemini-4-argon-low', 'individual quota reached', 'model gemini-4-argon-low'),
    ('antigravity', 'Example (Tier (Variant))', 'quota reached (recorded)', 'model Example (Tier (Variant))'),
    ('codex', None, 'individual quota reached', 'individual quota reached'),
])
def test_held_scope_from_quota_reason(harness, model, reason, expected):
    pool = 'example-pool'
    wall = Wall('example', None, None, reason=reason, harness=harness, model=model)
    described = quota.Quotas.describe(dict(pool=pool, wall=wall, resets_at=None, source='example-record'))
    assert watch.held_scope(described, pool) == expected
