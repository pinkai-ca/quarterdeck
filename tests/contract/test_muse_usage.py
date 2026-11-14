"""Synthetic native-command envelopes; no usage command or service request."""

import json
from datetime import datetime, timezone

import pytest

from quarterdeck.harness.muse_usage import decode

NOW = datetime(2040, 1, 1, tzinfo=timezone.utc)


def reading(allowance):
    return decode(json.dumps(dict(schemaVersion=1, allowance=allowance)), NOW)


def test_no_allowance_is_unknown_without_an_invented_window():
    result = decode('{"schemaVersion":1,"allowance":null,"hint":null,"reason":"no_allowance"}', NOW)
    assert result.windows == () and result.error == "Muse usage no_allowance"


def test_present_percentage_keeps_its_precision_and_explicit_period():
    result = reading(dict(window=dict(usedPercent=12, resetsAtMs=2208996000000),
                          weekly=dict(usedPercent=0, resetsAtMs=2209593600000)))
    assert result.error is None
    assert [(window.name, window.used) for window in result.windows] == [("window", 0.12), ("weekly", 0)]
    assert result.windows[0].resets_at == datetime.fromtimestamp(2208996000, timezone.utc)


@pytest.mark.parametrize("percent", [True, None, -1, 101, "1", float("nan"), float("inf")])
def test_invalid_percentage_is_not_zero_or_saturated(percent):
    result = reading(dict(weekly=dict(used_percent=percent, resets_at=2209593600)))
    assert result.error and result.windows[0].used is None


def test_absent_period_is_not_assumed_from_history():
    result = reading(dict(window=dict(used_percent=2, resets_at=2208996000)))
    assert result.error and result.windows[0].name == "window" and result.windows[0].used is None


@pytest.mark.parametrize("value", ["invalid fictional JSON", "[]", '{"schemaVersion":true}',
                                  '{"schemaVersion":2}', '{"schemaVersion":1,"allowance":{}}'])
def test_unknown_envelopes_return_safe_diagnostics(value):
    result = decode(value, NOW)
    assert result.windows == () and result.error and "fictional" not in result.error
