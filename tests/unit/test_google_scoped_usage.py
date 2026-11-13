"""Synthetic Google group policy examples; no real membership or provider calls."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import sqlite3

import pytest

from quarterdeck.harness import antigravity_models as models
from quarterdeck.harness.antigravity import AntigravityHarness
from quarterdeck.harness.antigravity_quota import parse_usage, wall
from quarterdeck.harness.base import Screen
from quarterdeck.harness.claude import ClaudeHarness
from quarterdeck.harness.codex import CodexHarness
from quarterdeck.harness.limits import Usage, Window
from quarterdeck.ledger import Ledger
from quarterdeck.quota import ModelScope, Quotas, cached_open, model_scope
from quarterdeck.watch import headline_readings, render_headlines

ARGON, GEMINI = "Argon synthetic group", "Gemini synthetic group"
ARGON_MODEL, FLASH_MODEL = "argon-synth", "flash-synth"
POOL = "google-synth"


@pytest.fixture
def at():
    return datetime.now(timezone.utc)


@pytest.fixture
def facts(monkeypatch):
    # These relations and canonical identities are fixture facts only.
    monkeypatch.setattr(models, "MEMBERSHIP", {
        (ARGON, ARGON_MODEL): True, (ARGON, FLASH_MODEL): False,
        (GEMINI, ARGON_MODEL): False, (GEMINI, FLASH_MODEL): True,
    })
    monkeypatch.setattr(models, "MODEL_IDENTITIES", {ARGON_MODEL: ARGON_MODEL, FLASH_MODEL: FLASH_MODEL})
    monkeypatch.setattr(models, "ALL_MODEL_GROUPS", frozenset())


@pytest.fixture
def book(tmp_path):
    with Ledger(tmp_path / "ledger.sqlite") as ledger:
        yield ledger


def document(at, groups=None):
    return {"status": "SUCCESS", "conversation_id": "", "num_turns": 0,
            "usage": {k: 0 for k in ("input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens")},
            "command": {"name": "usage", "data": {"groups": groups if groups is not None else [
                {"name": ARGON, "buckets": [bucket(at, "weekly", 0)]},
                {"name": GEMINI, "buckets": [bucket(at, "weekly", 0.8), bucket(at, "five_hour", 1)]},
            ]}}}


def bucket(at, period, remaining):
    return {"id": f"bucket-{period}-synth", "name": f"{period} synthetic allowance", "window": period,
            "remaining_fraction": remaining, "reset_time": (at + timedelta(days=2)).isoformat()}


def scope(model, pool=POOL):
    return ModelScope(pool, AntigravityHarness(), model)


def refusal(at, model=None, **metadata):
    event = wall("Individual quota reached. Resets in 2h.", "event-synth", at)
    return replace(event, model=model, **metadata)


def record_windows(book, at):
    book.record_usage(POOL, parse_usage(document(at), at))


def synthetic_ship(tmp_path):
    pool = SimpleNamespace(name=POOL, provider="google", kind="subscription", budget=None)
    return SimpleNamespace(runtime=tmp_path, pools={POOL: pool})


def test_all_returned_buckets_can_constrain_their_own_group(at):
    usage = parse_usage(document(at), at)
    assert len(usage.windows) == 3
    assert all(window.blocks for window in usage.windows)
    assert {w.group for w in usage.windows} == {ARGON, GEMINI}
    assert all(AntigravityHarness().is_headline(w.group, w.name) for w in usage.windows)


def test_argon_week_retains_legacy_storage_identity(at):
    usage = parse_usage(document(at, [{"name": "Gemini 4 Argon", "buckets": [bucket(at, "weekly", 0)]}]), at)
    assert [(w.name, w.group) for w in usage.windows] == [("7d", "Gemini 4 Argon")]


def test_other_returned_groups_do_not_invent_an_argon_window(at):
    usage = parse_usage(document(at, [{"name": GEMINI, "buckets": [bucket(at, "weekly", 1)]}]), at)
    assert usage.error is None
    assert [w.name for w in usage.windows] == [f"{GEMINI} / weekly"]


def test_failed_whole_read_is_neutral(at):
    usage = parse_usage({"status": "UNAVAILABLE"}, at)
    assert usage.windows == ()
    assert usage.error


def test_separate_periods_retain_independent_fractions(at):
    usage = parse_usage(document(at), at)
    gemini = [w for w in usage.windows if w.group == GEMINI]
    assert {w.name: w.used for w in gemini} == pytest.approx({f"{GEMINI} / weekly": 0.2, f"{GEMINI} / five_hour": 0})


def test_the_real_tables_hold_only_the_measured_rows_and_leave_the_rest_unknown():
    """The rows the live proof of 2026-11-13 measured, and nothing inferred from a name."""
    flash, argon, sonnet = "Gemini 3.8 Flash (Low)", "Gemini 4 Argon (Low)", "Claude Sonnet 5.5 (Low)"
    groups = ("Gemini Models", "Gemini 4 Argon", "Claude and GPT models")
    assert models.MEMBERSHIP == {(group, model): group == home for model, home in
                                 ((flash, "Gemini Models"), (argon, "Gemini 4 Argon"), (sonnet, "Claude and GPT models"))
                                 for group in groups}
    assert models.ALL_MODEL_GROUPS == frozenset()
    reader = AntigravityHarness()
    assert reader.usage_scope("Gemini Models", "gemini-3.8-flash-low") is True, "the launch id is the recorded name"
    assert reader.usage_scope("Gemini 4 Argon", "gemini-3.8-flash-low") is False
    assert reader.usage_scope("Gemini 4 Argon", "Gemini 4 Argon (Low)") is True
    for unmeasured in ("Gemini 3.8 Flash (High)", "Gemini 3.8 Flash", "Gemini 4 Argon", None):
        assert reader.usage_scope("Gemini Models", unmeasured) is None, unmeasured
    assert reader.usage_scope("Some other group", flash) is None


def test_synthetic_membership_and_exclusion_are_explicit(facts):
    assert models.applies(ARGON, ARGON_MODEL) is True
    assert models.applies(ARGON, FLASH_MODEL) is False
    assert models.applies(ARGON, "argon-synth-high") is None


def test_unplaced_model_has_no_group_membership(facts):
    assert models.applies(ARGON, None) is None
    assert models.applies(GEMINI, "unplaced-synth") is None


def test_missing_group_does_not_mean_all_models(facts):
    assert models.applies(None, FLASH_MODEL) is None
    assert models.applies(None, None) is None


def test_proved_all_model_group_applies_without_model(monkeypatch):
    monkeypatch.setattr(models, "ALL_MODEL_GROUPS", frozenset({"all-models-synth"}))
    assert models.applies("all-models-synth", None) is True
    assert models.applies("other-group-synth", None) is None


def test_alias_only_uses_explicit_native_identity(facts, monkeypatch):
    assert models.applies(GEMINI, "flash-display-synth") is None
    monkeypatch.setitem(models.MODEL_IDENTITIES, "flash-display-synth", FLASH_MODEL)
    assert models.applies(GEMINI, "flash-display-synth") is True


def test_refusal_models_use_exact_or_proved_canonical_identity(at, facts):
    event = refusal(at, ARGON_MODEL)
    assert models.event_applies(event, ARGON_MODEL) is True
    assert models.event_applies(event, FLASH_MODEL) is False
    assert models.event_applies(event, "argon-display-synth") is None


def test_argon_spent_gemini_open_share_the_ledger_policy(book, at, facts):
    record_windows(book, at)
    assert book.walled(POOL, scope=scope(ARGON_MODEL))
    assert not book.walled(POOL, scope=scope(FLASH_MODEL))
    assert book.usage_known(POOL, scope=scope(FLASH_MODEL))
    assert ARGON in book.walls(scope=scope(ARGON_MODEL), pool=POOL)[0]


def test_unknown_model_proves_neither_group_wall_nor_room(book, at, facts):
    record_windows(book, at)
    assert not book.walled(POOL, scope=scope(None))
    assert not book.usage_known(POOL, scope=scope(None))


def test_positive_membership_alone_cannot_prove_room(book, at, facts, monkeypatch):
    record_windows(book, at)
    monkeypatch.delitem(models.MEMBERSHIP, (ARGON, FLASH_MODEL))
    assert models.applies(GEMINI, FLASH_MODEL) is True
    assert not book.usage_known(POOL, scope=scope(FLASH_MODEL))


def test_all_model_window_can_hold_an_unplaced_source(book, at, monkeypatch):
    monkeypatch.setattr(models, "ALL_MODEL_GROUPS", frozenset({"all-models-synth"}))
    book.record_usage(POOL, Usage((Window("week-synth", 1, at + timedelta(days=1), group="all-models-synth"),), at))
    assert book.walled(POOL, scope=scope(None))


def test_refusal_persists_its_own_model_not_an_entire_pool(book, at, facts):
    book.quota.observe("record-synth", "seat-synth", POOL, refusal(at, ARGON_MODEL), at)
    assert book.walled(POOL, scope=scope(ARGON_MODEL))
    assert not book.walled(POOL, scope=scope(FLASH_MODEL))
    assert "model argon-synth" in book.walls(pool=POOL)[0]


def test_unknown_event_scope_proves_no_wall_and_no_automatic_room(book, at, facts, tmp_path):
    record_windows(book, at)
    book.quota.observe("record-synth", "seat-synth", POOL, refusal(at), at)
    assert not book.walled(POOL, scope=scope(FLASH_MODEL))
    assert not book.quota.room_known(POOL, scope=scope(FLASH_MODEL))
    assert not cached_open(synthetic_ship(tmp_path), book, POOL)


def test_unplaced_refusal_stays_with_its_own_observed_source(book, at):
    book.quota.observe("record-synth", "seat-synth", POOL, refusal(at), at)
    own = ModelScope(POOL, AntigravityHarness(), None, "seat-synth", ("record-synth",))
    other = ModelScope(POOL, AntigravityHarness(), None, "other-synth", ("other-record-synth",))
    assert book.walled(POOL, scope=own)
    assert not book.walled(POOL, scope=other)


def test_legacy_wall_table_needs_no_side_table_for_reading(at):
    db = sqlite3.connect(":memory:")
    try:
        db.execute("CREATE TABLE wall_events (source TEXT, event TEXT, seat TEXT, pool TEXT, at TEXT, resets_at TEXT, "
                   "observed_at TEXT, reason TEXT, error_id TEXT, signature TEXT, superseded INTEGER)")
        # The legacy writer's positional insert remains valid.
        db.execute("INSERT INTO wall_events VALUES (?,?,?,?,?,?,?,?,?,?,0)",
                   ("record-synth", "event-synth", "seat-synth", POOL, at.isoformat(),
                    (at + timedelta(hours=1)).isoformat(), at.isoformat(), "individual quota reached", None, "signature-synth"))
        quota = Quotas(db, create=False)
        assert len(quota.active(at)[0]) == 6
        assert quota.walls(at, scope=scope(FLASH_MODEL)) == []
        assert quota.walls(at)  # An older unscoped caller still reads its old layout.
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='wall_scopes'").fetchone()
    finally:
        db.close()


def test_identical_event_observation_does_not_rewrite_scope(book, at):
    event = refusal(at, ARGON_MODEL)
    book.quota.observe("record-synth", "seat-synth", POOL, event, at)
    changes = book.db.total_changes
    book.quota.observe("record-synth", "seat-synth", POOL, event, at + timedelta(minutes=1))
    assert book.db.total_changes == changes


def seat_for_model(tmp_path, record_model, declared_model):
    harness = AntigravityHarness()
    harness.state = lambda record: SimpleNamespace(model=record_model, effort=None)
    return SimpleNamespace(name="seat-synth", ship=synthetic_ship(tmp_path), harness=harness,
                           spec=SimpleNamespace(for_pool=lambda pool: SimpleNamespace(harness="antigravity", model=declared_model)),
                           record=lambda: Path("record-synth.db"), launched=lambda: SimpleNamespace(at="launch-synth"))


def test_source_model_and_destination_model_are_resolved_separately(tmp_path, facts):
    seat = seat_for_model(tmp_path, ARGON_MODEL, FLASH_MODEL)
    screen = Screen("", None, shows={"model": FLASH_MODEL})
    assert model_scope(seat, POOL, running=True, screen=screen)(ARGON) is True
    assert model_scope(seat, POOL)(ARGON) is False


def test_no_like_seat_uses_actual_screen_model_when_record_has_none(tmp_path, facts):
    seat = seat_for_model(tmp_path, None, None)
    assert model_scope(seat, POOL, running=True, screen=Screen("", None, shows={"model": FLASH_MODEL}))(GEMINI) is True


def test_claude_and_codex_window_semantics_are_retained(book, at):
    reset = at + timedelta(days=1)
    book.record_usage("claude-synth", Usage((Window("sonnet / 7d", 1, reset, group="sonnet"), Window("5h", 0, reset)), at))
    assert not book.walled("claude-synth", scope=ModelScope("claude-synth", ClaudeHarness(), "claude-opus-synth"))
    assert book.walled("claude-synth", scope=ModelScope("claude-synth", ClaudeHarness(), "claude-sonnet-synth"))
    assert not CodexHarness().model_scoped_usage
    book.record_usage("codex-synth", Usage((Window("7d", 1, reset),), at))
    assert book.walled("codex-synth")


def test_group_headlines_show_spent_and_open_independently(book, at, tmp_path):
    record_windows(book, at)
    lines = render_headlines(headline_readings(synthetic_ship(tmp_path)))
    assert any(ARGON in line and "100% used" in line for line in lines)
    assert any(GEMINI in line and "20% used" in line for line in lines)
    assert any(GEMINI in line and "0% used" in line for line in lines)


def proto_field(number, value):
    def varint(n):
        result = bytearray()
        while n >= 128:
            result.append((n & 127) | 128)
            n >>= 7
        return bytes(result) + bytes([n])
    if isinstance(value, int):
        return varint(number << 3) + varint(value)
    value = value.encode() if isinstance(value, str) else value
    return varint((number << 3) | 2) + varint(len(value)) + value


def native_input(model):
    config = proto_field(1, proto_field(28, model))
    return proto_field(19, proto_field(2, "synthetic input") + proto_field(12, config))


def native_error():
    return proto_field(24, proto_field(3, proto_field(1, "Individual quota reached. Resets in 2h.")))


def decoded_events(monkeypatch, tmp_path, rows, executors=()):
    from quarterdeck.harness import antigravity_record as native
    fingerprint = ((0, 0, 0, 1), (0, 0, 0, 0))
    monkeypatch.setattr(native, "_snapshot_query", lambda path: (fingerprint, "conversation-synth", None, rows, [], executors))
    return native.read(tmp_path / "record-synth.db")[0]


def test_unplaced_record_refusal_does_not_borrow_a_later_model(monkeypatch, tmp_path):
    rows = [(0, 17, 3, 0, b"", None, native_error()),
            (1, 14, 3, 0, proto_field(12, "execution-synth"), None, native_input(FLASH_MODEL))]
    state = decoded_events(monkeypatch, tmp_path, rows)
    assert state.model == FLASH_MODEL
    assert state.walls[0].model is None


def test_record_error_trailer_keeps_its_own_execution_model(monkeypatch, tmp_path):
    rows = [(0, 14, 3, 0, proto_field(12, "argon-execution-synth"), None, native_input(ARGON_MODEL)),
            (1, 17, 3, 0, b"", None, native_error()),
            (2, 14, 3, 0, proto_field(12, "flash-execution-synth"), None, native_input(FLASH_MODEL))]
    executor = proto_field(1, 1) + proto_field(3, 0) + proto_field(9, "argon-execution-synth")
    state = decoded_events(monkeypatch, tmp_path, rows, [(0, executor)])
    assert state.model == FLASH_MODEL
    assert state.walls[0].model == ARGON_MODEL


def test_destination_uses_its_own_harness_and_model(tmp_path, facts):
    seat = seat_for_model(tmp_path, ARGON_MODEL, FLASH_MODEL)
    seat.ship.pools["claude-synth"] = SimpleNamespace(kind="subscription", provider="anthropic")
    seat.spec.for_pool = lambda pool: SimpleNamespace(harness="claude", model="claude-sonnet-synth")
    destination = model_scope(seat, "claude-synth")
    assert destination.reader.name == "claude"
    assert destination(None) is True
    assert destination("sonnet") is True
    assert destination("opus") is False


def test_bound_google_policy_does_not_filter_another_pool(book, at, facts):
    record_windows(book, at)
    book.record_usage("codex-synth", Usage((Window("7d", 1, at + timedelta(days=1)),), at))
    assert not book.walled(POOL, scope=scope(FLASH_MODEL))
    assert "codex-synth 7d" in book.walls(scope=scope(FLASH_MODEL))


def test_missing_google_headline_keeps_its_group_label(tmp_path):
    lines = render_headlines(headline_readings(synthetic_ship(tmp_path)))
    assert any("Gemini 4 Argon / 7d usage unknown" in line for line in lines)


@pytest.mark.parametrize("member,held", [("argon", True), ("flash", False)])
def test_the_door_holds_a_seat_in_the_spent_group_with_its_reason_and_no_other(tmp_path, monkeypatch, member, held):
    """The desk's case end to end: Argon's week spent, the Gemini week open, the seat's running model from its record."""
    from quarterdeck.door import Door
    from .test_quota_walls import synthetic_seat
    seat, _, _, _ = synthetic_seat(tmp_path, monkeypatch)
    running = "synthetic-model"  # what the seat's own record says it runs
    monkeypatch.setattr(models, "MEMBERSHIP", {
        ("Gemini 4 Argon", running): member == "argon", ("Gemini Models", running): member == "flash"})
    at = datetime.now(timezone.utc)
    doc = document(at, [{"name": "Gemini 4 Argon", "buckets": [bucket(at, "weekly", 0)]},
                        {"name": "Gemini Models", "buckets": [bucket(at, "weekly", 0.99)]}])
    with Ledger(seat.ship.runtime / "ledger.sqlite") as ledger:
        ledger.record_usage(seat.pool.name, parse_usage(doc, at))
    reason = Door(seat).wall(None)
    if held:
        assert reason and "Gemini 4 Argon" in reason, "the seat is held, and the reason names the group"
    else:
        assert reason is None, "a spent group holds no seat whose model is in another group"
