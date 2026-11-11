"""Synthetic native period/diagnostic evidence and current-writer upgrade continuity."""

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from quarterdeck import quota
from quarterdeck.harness.claude import ClaudeHarness
from quarterdeck.harness.claude_limits import shape, snapshot
from quarterdeck.ledger import Ledger
from quarterdeck.seat import Seat
from quarterdeck.ship import Pool, SeatSpec, Session, Ship
from .test_price_rollback import old  # noqa: F401

BASE = "34abc0b5a43ba192e5f589b8c5e5cc86c37200cb"
ABSENT = object()


@pytest.fixture
def at():
    return datetime.now(timezone.utc).replace(microsecond=0)


def document(at, delta=0, *, kind=ABSENT, group=ABSENT, percent=30, severity="normal", two=False):
    reset = at+timedelta(days=7)
    model_reset = reset+timedelta(seconds=delta)
    row = {"scope": {"model": {"display_name": "Fable"}}, "percent": percent, "severity": severity,
           "is_active": False, "resets_at": model_reset.isoformat()}
    if kind is not ABSENT:
        row["kind"] = kind
    if group is not ABSENT:
        row["group"] = group
    doc = {"seven_day": {"utilization": 60, "resets_at": reset.isoformat()}, "limits": [row]}
    if two:
        doc["five_hour"] = {"utilization": 20, "resets_at": (model_reset+timedelta(seconds=.5)).isoformat()}
    return doc


def model_window(measured):
    return next(w for w in measured.windows if w.group == "fable")


@pytest.mark.parametrize("delta", [.00015, 1.0002, -1.0002, 0, 2, -2])
def test_reset_inference_has_an_inclusive_absolute_two_second_bound(at, delta):
    row = model_window(snapshot(document(at, delta), at))
    assert row.name == "fable / 7d" and row.uncertainty is None


@pytest.mark.parametrize("delta", [2.000001, -2.000001, 10])
def test_just_outside_or_missing_reset_match_is_unknown(at, delta):
    row = model_window(snapshot(document(at, delta), at))
    assert row.name == "fable / unknown period" and row.uncertainty


def test_two_general_matches_stay_unknown(at):
    row = model_window(snapshot(document(at, .2, two=True), at))
    assert row.name == "fable / unknown period" and row.uncertainty


@pytest.mark.parametrize("delta,two", [(20, False), (.2, True)])
def test_native_weekly_scoped_kind_precedes_reset_inference(at, delta, two):
    row = model_window(snapshot(document(at, delta, kind="weekly_scoped", group="weekly", two=two), at))
    assert row.name == "fable / 7d" and row.uncertainty is None


def test_render_group_alone_does_not_establish_a_period(at):
    row = model_window(snapshot(document(at, 10, group="weekly"), at))
    assert row.name == "fable / unknown period" and row.uncertainty


@pytest.mark.parametrize("word", ["weekly_scoped", "Weekly-2", "0", "_", "A"*32])
def test_only_short_ascii_kind_and_group_words_can_be_diagnostic_values(word):
    found = shape({"kind": word, "group": word, "other": "PRIVATE_VALUE", "nested": {"kind": "PRIVATE_NESTED"}})
    assert found["kind"] == {"type": "str", "word": word}
    assert found["group"] == {"type": "str", "word": word}
    text = json.dumps(found)
    assert "PRIVATE_VALUE" not in text and "PRIVATE_NESTED" not in text


@pytest.mark.parametrize("value", [None, 7, True, [], {}, "", "a b", "a.b", "a/b", "x\n", "κ", "A"*33])
def test_other_scalar_values_remain_key_and_type_only(value):
    found = shape({"kind": value, "group": value})
    if isinstance(value, (dict, list)):
        assert "word" not in found["kind"]
    else:
        assert found["kind"] == type(value).__name__
    if isinstance(value, str) and value:
        assert value not in json.dumps(found)


def test_unknown_kind_is_observed_without_becoming_a_period_enum(at):
    measured = snapshot(document(at, 10, kind="new_kind", group="weekly"), at)
    assert model_window(measured).name == "fable / unknown period"
    text = " ".join(measured.diagnostics)
    assert "new_kind" in text and "weekly" in text and "unmapped limits:" not in text


def test_mapped_uncertainty_does_not_inflate_unmapped_entry_count(at):
    doc = document(at, 10)
    doc["limits"].insert(0, {"scope": None, "kind": "weekly_all", "group": "weekly"})
    measured = snapshot(doc, at)
    text = " ".join(measured.diagnostics)
    assert "unmapped limits: 1;" in text and "unmapped limits: 2;" not in text
    assert "limits diagnostics: 1;" in text
    assert model_window(measured).uncertainty


@pytest.mark.parametrize("old", [BASE], indirect=True)
@pytest.mark.parametrize("percent,severity,spent", [(30, "normal", False), (100, "critical", True), (-0.0, "critical", True)])
def test_upgrade_current_writer_unknown_period_is_replaced_by_proved_current_row(tmp_path, old, at, percent, severity, spent):
    path = tmp_path/"runtime/ledger.sqlite"
    old('''
import json,sys
from datetime import datetime
from quarterdeck.harness.claude_limits import snapshot
from quarterdeck.ledger import Ledger
with Ledger(Path(sys.argv[1])) as book:
    book.record_usage("synthetic",snapshot(json.loads(sys.argv[2]),datetime.fromisoformat(sys.argv[3])))
    row=next(r for r in book.usage.rows() if r.group=="fable")
    assert row.window=="fable / unknown period" and not row.current(datetime.fromisoformat(sys.argv[3]))
''', path, json.dumps(document(at, 1.0002, kind="weekly_scoped", group="weekly", percent=percent, severity=severity)), at.isoformat())
    with Ledger(path) as book:
        measured = snapshot(document(at, 1.00035, kind="weekly_scoped", group="weekly", percent=percent, severity=severity), at+timedelta(seconds=301))
        book.record_usage("synthetic", measured)
        rows = book.usage.rows("synthetic")
        assert {r.window for r in rows} == {"7d", "fable / 7d"}
        assert all(r.current(at+timedelta(seconds=301)) for r in rows)
        assert book.walled("synthetic") == spent
        assert not book.quota.usage_notes()
        ship = Ship(tmp_path, "synthetic", "operator", tmp_path, {"synthetic": Pool("synthetic", "claude")},
                    {"crew": Session("crew", "Crew")}, {})
        seat = Seat(ship, SeatSpec("worker", "crew", "worker", "claude", ("synthetic",), model="claude-fable-5-2"), SimpleNamespace())
        assert quota.cached_open(ship, book, "synthetic", seat) == (not spent)
        reader = ClaudeHarness()
        assert not book.walled("synthetic", scope=lambda g: reader.usage_scope(g, "claude-opus-5-5"))


@pytest.mark.parametrize("reason", ["different_reset", "different_group", "different_bucket", "different_blocks", "ambiguous", "still_present", "older", "stale", "failed", "malformed"])
def test_unknown_identity_is_preserved_without_a_justified_replacement(tmp_path, at, reason):
    from quarterdeck.harness.limits import Usage, Window
    from dataclasses import replace
    path = tmp_path/"ledger.sqlite"
    reset = at+timedelta(days=7)
    unknown = Window("fable / unknown period", 1, reset, group="fable", exhausted=True,
                     uncertainty="model allowance period unknown")
    with Ledger(path) as book:
        book.record_usage("synthetic", Usage((unknown,), at))
        measured = snapshot(document(at, kind="weekly_scoped", group="weekly"), at+timedelta(seconds=301))
        if reason == "different_reset":
            measured = snapshot(document(at, 3, kind="weekly_scoped"), at+timedelta(seconds=301))
        elif reason == "different_group":
            measured = replace(measured, windows=tuple(replace(w, group="opus") if w.group else w for w in measured.windows))
        elif reason == "different_bucket":
            measured = replace(measured, windows=tuple(replace(w, bucket="synthetic-new-cap") if w.group else w for w in measured.windows))
        elif reason == "different_blocks":
            measured = replace(measured, windows=tuple(replace(w, blocks=False) if w.group else w for w in measured.windows))
        elif reason == "ambiguous":
            other = replace(model_window(measured), name="fable / 5h")
            measured = replace(measured, windows=measured.windows+(other,))
        elif reason == "still_present":
            measured = replace(measured, windows=measured.windows+(unknown,))
        elif reason == "older":
            measured = replace(measured, read_at=at-timedelta(seconds=1))
        elif reason == "failed":
            measured = replace(measured, error="synthetic failed observation")
        elif reason == "malformed":
            measured = replace(measured, windows=tuple(replace(w, used=None) if w.group else w for w in measured.windows))
        attempt = at+timedelta(hours=2) if reason == "stale" else measured.read_at
        book.record_usage("synthetic", measured, attempted_at=attempt)
        assert any(r.window == unknown.name for r in book.usage.rows("synthetic"))
        assert book.walled("synthetic"), "missing complete spent evidence is retained until its own reset"


def test_diagnostic_count_tracks_window_creation_including_explicit_unknown_windows(at):
    doc = document(at, 10)
    doc["limits"][0]["percent"] = None
    doc["limits"].insert(0, {"scope": None, "kind": "weekly_all", "group": "weekly"})
    measured = snapshot(doc, at)
    assert model_window(measured).error
    text = " ".join(measured.diagnostics)
    assert "unmapped limits: 1;" in text and "limits diagnostics: 1;" in text


@pytest.mark.parametrize("kind", [None, [], {}, 7, True, "future_kind"])
def test_unproved_native_kind_type_or_enum_never_overrides_reset_evidence(at, kind):
    measured = snapshot(document(at, 10, kind=kind, group="weekly"), at)
    row = model_window(measured)
    assert row.name == "fable / unknown period" and row.uncertainty
    assert any("limits diagnostics:" in note for note in measured.diagnostics)


@pytest.mark.parametrize("distance,retired", [(2, True), (-2, True), (2.000001, False), (-2.000001, False)])
def test_reconciliation_reset_identity_bound_is_also_inclusive(tmp_path, at, distance, retired):
    from quarterdeck.harness.limits import Usage, Window
    reset = at+timedelta(days=7)
    old_window = Window("fable / unknown period", .3, reset, group="fable", uncertainty="model allowance period unknown")
    with Ledger(tmp_path/"ledger.sqlite") as book:
        book.record_usage("synthetic", Usage((old_window,), at))
        book.record_usage("synthetic", snapshot(document(at, distance, kind="weekly_scoped"), at+timedelta(seconds=301)))
        assert all(r.window != old_window.name for r in book.usage.rows()) == retired
        assert book.usage_known("synthetic") == retired
