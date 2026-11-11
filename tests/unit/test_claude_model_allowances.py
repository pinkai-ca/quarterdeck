"""Synthetic model-scoped allowances; no native response or provider request."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from quarterdeck import cli, harness, output, post, quota, watch
from quarterdeck.harness.limits import Usage, Window
from quarterdeck.ledger import Ledger
from .test_claude_usage_reads import Endpoint, ok, subscription
from .test_cross_program_failover import running, usage  # noqa: F401
from .test_price_rollback import old  # noqa: F401


@pytest.fixture(autouse=True)
def stable_body_clock(monkeypatch):
    at = datetime.now(timezone.utc).replace(microsecond=0)
    monkeypatch.setattr(post, "now", lambda: at)


def body(**extra):
    reset = (post.now() + timedelta(days=7)).isoformat()
    return {"seven_day": {"utilization": 80, "resets_at": reset}, **{
        key: {"utilization": value, "resets_at": reset} for key, value in extra.items()}}


def test_provider_model_allowances_are_kept_from_one_fetched_body(tmp_path, monkeypatch):
    from quarterdeck.harness import claude
    endpoint = Endpoint(monkeypatch, ok(body(seven_day_sonnet=100, seven_day_opus=20)))
    measured = claude.usage(subscription(tmp_path))
    assert endpoint.calls == 1
    assert {(w.name, w.group, w.used) for w in measured.windows} == {
        ("7d", None, .8), ("fable / 7d", "fable", 1), ("opus / 7d", "opus", .2)}


@pytest.fixture
def scoped(running, monkeypatch):
    r = running
    original = r.seat.spec
    choices = {name: dict(values) for name, values in original.pool_choices.items()}
    choices["second"]["model"] = "claude-fable-5-2"
    choices["third"]["model"] = "claude-fable-5-2"
    r.seat.spec = replace(original, pool_choices=choices)
    r.seat.ship.seats["worker"] = r.seat.spec
    r.svc.failover()
    assert r.seat.pool.name == "second"
    monkeypatch.setattr(watch, "service_state", lambda _: "ok")
    monkeypatch.setattr(watch, "disks", lambda _: [])
    monkeypatch.setattr(watch.credential, "login_needed", lambda _: [])
    return r


def model(r, running, declared=None, destination=None):
    choices = {name: dict(values) for name, values in r.seat.spec.pool_choices.items()}
    if declared:
        choices["second"]["model"] = declared
    if destination:
        choices["third"]["model"] = destination
    r.seat.spec = replace(r.seat.spec, pool_choices=choices, model=choices["second"]["model"])
    r.seat.ship.seats["worker"] = r.seat.spec
    r.created[-1].begin("synthetic input", running, "high")
    r.created[-1].end("synthetic reply")


def windows(r, pool, *, fable=.2, opus=.2, general=.8, error=None, at=None):
    at = at or post.now()
    rows = [Window("5h", general, at + timedelta(hours=5)), Window("7d", general, at + timedelta(days=7))]
    rows += [Window(f"{family} / 7d", used, at + timedelta(days=7), group=family, error=error)
             for family, used in (("fable", fable), ("opus", opus)) if used is not None or error]
    r.svc.ledger.record_usage(pool, Usage(tuple(rows), at))


def facts(r):
    return watch.facts(r.seat.ship, r.seat.tmux)


@pytest.mark.parametrize("running", [False, True], indirect=True)
@pytest.mark.parametrize("running_model,declared,moves", [
    ("claude-fable-5-2", "claude-opus-5-5", True),
    ("claude-opus-5-5", "claude-fable-5-2", False),
])
def test_source_wall_uses_running_family_not_declared_family(scoped, running_model, declared, moves):  # law: record-first
    r = scoped
    model(r, running_model, declared=declared)
    windows(r, "second", fable=1)
    windows(r, "third")
    assert facts(r).held == []
    r.svc.failover()
    assert r.seat.pool.name == ("third" if moves else "second")


@pytest.mark.parametrize("running", [False, True], indirect=True)
@pytest.mark.parametrize("destination,eligible", [("claude-fable-5-2", False), ("claude-opus-5-5", True)])
def test_destination_uses_its_resolved_inline_model(scoped, destination, eligible):  # law: nothing-waits-silently
    r = scoped
    model(r, "claude-fable-5-2", destination=destination)
    windows(r, "second", fable=1)
    windows(r, "third", fable=1)
    assert r.svc.known_open(r.seat, "third") == eligible
    assert facts(r).held == ([] if eligible else ["worker"])
    line = facts(r).line()
    assert "fable" not in line and "reset" not in line and "usage" not in line
    r.svc.failover()
    assert r.seat.pool.name == ("third" if eligible else "second")


def test_unknown_matching_allowance_blocks_automatic_but_not_explicit_selection(scoped):
    r = scoped
    model(r, "claude-fable-5-2")
    windows(r, "second", fable=1)
    windows(r, "third", fable=None, error="synthetic unreadable scoped fraction")
    assert not r.svc.known_open(r.seat, "third")
    assert facts(r).held == ["worker"]
    r.svc.failover()
    assert r.seat.pool.name == "second"
    result = r.svc.select(r.seat, "third")
    assert "usage unknown; moved on the explicit act" in result
    assert r.seat.pool.name == "third"


def test_missing_model_rows_preserve_legacy_general_room(scoped):
    r = scoped
    model(r, "claude-fable-5-2")
    windows(r, "second", fable=1)
    usage(r.svc, "third", .8)
    assert r.svc.known_open(r.seat, "third")
    r.svc.failover()
    assert r.seat.pool.name == "third"


def test_unknown_other_family_does_not_block_a_known_destination(scoped):
    r = scoped
    model(r, "claude-opus-5-5", destination="claude-opus-5-5")
    at = post.now()
    r.svc.ledger.record_usage("third", Usage((Window("5h", .8, at+timedelta(hours=5)), Window("7d", .8, at+timedelta(days=7)),
        Window("fable / 7d", None, None, group="fable", error="synthetic missing"),
        Window("opus / 7d", .2, at+timedelta(days=7), group="opus")), at))
    assert r.svc.known_open(r.seat, "third")


@pytest.mark.parametrize("raw", [None, True, "100", -1, 101, float("nan"), {}, {"utilization": 100}])
def test_reported_unreadable_scope_is_unknown_without_manufacturing_a_wall(tmp_path, raw):
    from quarterdeck.harness.claude_limits import snapshot
    at = post.now()
    row = {"scope": {"model": {"display_name": "Fable"}}, "percent": raw,
           "severity": "normal", "is_active": False, "resets_at": body()["seven_day"]["resets_at"]}
    measured = snapshot({**body(), "limits": [row]}, at)
    with Ledger(tmp_path / "ledger.sqlite") as book:
        book.record_usage("synthetic", measured)
        reader = harness.get("claude")
        fable = lambda group: reader.usage_scope(group, "claude-fable-5-2")
        opus = lambda group: reader.usage_scope(group, "claude-opus-5-5")
        assert not book.walled("synthetic", scope=fable)
        assert not book.usage_known("synthetic", scope=fable)
        assert book.usage_known("synthetic", scope=opus)


@pytest.mark.parametrize("duplicate", [100, 20])
def test_repeated_top_level_representations_agree_or_remain_unknown(tmp_path, duplicate):
    from quarterdeck.harness.claude_limits import snapshot
    at = post.now()
    measured = snapshot(body(seven_day_sonnet=100, seven_day_fable=duplicate), at)
    scoped_rows = [w for w in measured.windows if w.group == "fable"]
    assert len(scoped_rows) == 1
    with Ledger(tmp_path / "ledger.sqlite") as book:
        book.record_usage("synthetic", measured)
        assert book.walled("synthetic") == (duplicate == 100)
        assert book.usage_known("synthetic") == (duplicate == 100)


def test_last_known_scoped_spend_survives_missing_or_failed_measurement_until_reset(tmp_path):
    from quarterdeck.harness.claude_limits import snapshot
    at = post.now()
    with Ledger(tmp_path / "ledger.sqlite") as book:
        book.record_usage("synthetic", snapshot(body(seven_day_sonnet=100), at))
        book.record_usage("synthetic", snapshot(body(), at + timedelta(seconds=1)))
        assert book.walled("synthetic") and not book.usage_known("synthetic")
        book.record_usage("synthetic", Usage((), at+timedelta(seconds=2), "synthetic refused"))
        assert book.walled("synthetic")
        assert not book.walls(now=at + timedelta(days=8))


def test_stale_matching_scope_is_unknown_but_stale_other_family_is_not(scoped):
    r = scoped
    model(r, "claude-fable-5-2")
    windows(r, "third")
    old = (post.now() - timedelta(hours=2)).isoformat()
    r.svc.ledger.db.execute("UPDATE pools SET read_at=? WHERE pool='third' AND group_name='opus'", (old,))
    r.svc.ledger.db.commit()
    assert r.svc.known_open(r.seat, "third")
    r.svc.ledger.db.execute("UPDATE pools SET read_at=? WHERE pool='third' AND group_name='fable'", (old,))
    r.svc.ledger.db.commit()
    # Retain paced reads: don't call the synthetic forbidden provider before its interval.
    assert not r.svc.known_open(r.seat, "third")


def test_general_spend_still_blocks_both_families(scoped):
    r = scoped
    windows(r, "second", general=1)
    for value in ("claude-fable-5-2", "claude-opus-5-5"):
        model(r, value)
        assert quota.current_walled(r.seat, r.svc.ledger)


def test_eligible_destination_suppresses_hold_while_lifecycle_defers_movement(scoped):
    r = scoped
    windows(r, "second", fable=1)
    windows(r, "third")
    r.created[-1].begin("synthetic active input", "claude-fable-5-2", "high")
    r.svc.failover()
    assert r.seat.pool.name == "second" and facts(r).held == []


def test_detailed_status_ledger_and_json_keep_every_scoped_window(scoped, monkeypatch, capsys):
    r = scoped
    windows(r, "second", fable=1)
    windows(r, "third", fable=1)
    ship = r.seat.ship
    monkeypatch.delenv("QD_SHIP", raising=False)
    monkeypatch.delenv("QD_SEAT", raising=False)
    monkeypatch.setattr(watch, "seats", lambda *args: [r.seat])
    for command in (("status",), ("ledger",), ("status", "--line")):
        assert cli.main(["--ship", str(ship.folder), "--json", *command]) == 0
        data = json.loads(capsys.readouterr().out)
        rows = data["windows"] if command == ("ledger",) else data["usage"]
        fable = next(row for row in rows if row["pool"] == "second" and row["reading"] and row["reading"]["group"] == "fable")
        assert fable["reading"]["window"] == "fable / 7d" and fable["reading"]["used"] == 1
        assert fable["reading"]["resets_at"]
        assert cli.main(["--ship", str(ship.folder), *command]) == 0
        text = capsys.readouterr().out
        if command == ("status", "--line"):
            assert "worker held at limit" in text and "fable" not in text and "reset" not in text
        else:
            assert "second fable / 7d 100%" in text and "reset" in text


def native_row(percent=35, severity="normal", reset=None, **extra):
    return {"scope": {"model": {"display_name": "Fable"}}, "percent": percent,
            "severity": severity, "is_active": False,
            "resets_at": reset or body()["seven_day"]["resets_at"], **extra}


@pytest.mark.parametrize("severity,spent", [("normal", False), ("warning", False), ("critical", True),
                                            ("exceeded", True), ("error", True)])
def test_native_list_severity_is_explicit_without_inflating_the_percentage(tmp_path, severity, spent):
    from quarterdeck.harness.claude_limits import snapshot
    at = post.now()
    measured = snapshot({**body(), "limits": [native_row(35, severity)]}, at)
    row = next(w for w in measured.windows if w.group == "fable")
    assert row.name == "fable / 7d" and row.used == .35 and row.exhausted == spent
    assert not measured.diagnostics
    with Ledger(tmp_path / "ledger.sqlite") as book:
        book.record_usage("synthetic", measured)
        assert book.walled("synthetic") == spent
        assert next(r for r in book.usage.rows() if r.group == "fable").used == .35


@pytest.mark.parametrize("matching", [0, 2])
def test_native_period_needs_one_general_reset_match_and_keeps_known_fraction(tmp_path, matching):
    from quarterdeck.harness.claude_limits import snapshot
    at = post.now()
    reset = (at + timedelta(hours=4)).isoformat()
    doc = {**body(), "limits": [native_row(35, "critical", reset)]}
    if matching == 2:
        doc.update(five_hour={"utilization": 20, "resets_at": reset},
                   seven_day={"utilization": 30, "resets_at": reset})
    measured = snapshot(doc, at)
    scoped_row = next(w for w in measured.windows if w.group == "fable")
    assert scoped_row.name == "fable / unknown period" and scoped_row.uncertainty
    with Ledger(tmp_path / "ledger.sqlite") as book:
        book.record_usage("synthetic", measured)
        assert not book.usage_known("synthetic")
        row = next(r for r in book.usage.rows() if r.group == "fable")
        assert row.used == .35 and row.exhausted and row.error
        assert "period unknown" in row.describe()
        assert book.walled("synthetic"), "native critical severity proves the wall despite unknown duration"


def test_model_period_can_match_the_five_hour_reset_without_defaulting_to_weekly(tmp_path):
    from quarterdeck.harness.claude_limits import snapshot
    at = post.now()
    reset = (at + timedelta(hours=5)).isoformat()
    measured = snapshot({**body(), "five_hour": {"utilization": 10, "resets_at": reset},
                         "limits": [native_row(reset=reset)]}, at)
    assert next(w for w in measured.windows if w.group == "fable").name == "fable / 5h"


@pytest.mark.parametrize("row", [None, 1, "synthetic secret value", [], {}, {"scope": None},
    {"scope": "synthetic secret value"}, {"scope": {"model": []}},
    {"scope": {"model": {"renamed_name": "synthetic secret value"}}},
    native_row(percent=True), native_row(severity=[]), native_row(is_active="false"),
    native_row(resets_at=[]), native_row(percent=200), native_row(scope={"model": {"display_name": "unrecognized secret model"}})])
def test_unmapped_limits_expose_only_key_names_and_types_in_ledger_and_json(tmp_path, row):
    from quarterdeck import ledger_reading
    from quarterdeck.harness.claude_limits import snapshot
    at = post.now()
    doc = {**body(), "limits": [row]}
    measured = snapshot(doc, at)
    assert len(measured.diagnostics) == 1 and "unmapped limits: 1" in measured.diagnostics[0]
    with Ledger(tmp_path / "ledger.sqlite") as book:
        book.record_usage("synthetic", measured)
        data = ledger_reading.collect(book)
        text = "\n".join(ledger_reading.render(data))
        serialized = json.dumps(output.value(data))
        for rendered in (text, serialized):
            assert "unmapped limits: 1" in rendered
            assert "synthetic secret value" not in rendered and "unrecognized secret model" not in rendered
        assert not book.walled("synthetic")


@pytest.mark.parametrize("limits", [None, {}, "synthetic secret value", False])
def test_renamed_or_malformed_list_container_remains_visible_and_unknown(tmp_path, limits):
    from quarterdeck.harness.claude_limits import snapshot
    at = post.now()
    with Ledger(tmp_path / "ledger.sqlite") as book:
        measured = snapshot({**body(), "limits": limits}, at)
        book.record_usage("synthetic", measured)
        assert measured.diagnostics and not book.usage_known("synthetic")
        assert not book.walled("synthetic")


def test_top_level_and_list_representation_merge_and_keep_the_native_severity_wall(tmp_path):
    from quarterdeck.harness.claude_limits import snapshot
    at = post.now()
    measured = snapshot({**body(seven_day_sonnet=35), "limits": [native_row(35, "critical")]}, at)
    rows = [w for w in measured.windows if w.group == "fable"]
    assert len(rows) == 1 and rows[0].used == .35 and rows[0].exhausted


def test_null_optional_model_fields_and_no_model_rows_keep_legacy_behavior(tmp_path):
    from quarterdeck.harness.claude_limits import snapshot
    at = post.now()
    with Ledger(tmp_path / "ledger.sqlite") as book:
        measured = snapshot({**body(), "seven_day_opus": None, "limits": []}, at)
        assert len(measured.windows) == 1 and not measured.diagnostics
        book.record_usage("synthetic", measured)
        assert book.usage_known("synthetic") and not book.walled("synthetic")


def test_door_scope_uses_the_running_model_and_preserves_another_family(scoped):
    from quarterdeck.door import Door
    r = scoped
    windows(r, "second", fable=1)
    model(r, "claude-opus-5-5", declared="claude-fable-5-2")
    assert Door(r.seat).wall(None) is None
    model(r, "claude-fable-5-2", declared="claude-opus-5-5")
    assert "second fable / 7d" in Door(r.seat).wall(None)


def test_explicit_wall_survives_failure_and_clears_only_on_newer_good_read(tmp_path):
    from quarterdeck.harness.claude_limits import snapshot
    at = post.now()
    with Ledger(tmp_path / "ledger.sqlite") as book:
        book.record_usage("synthetic", snapshot({**body(), "limits": [native_row(35, "critical")]}, at))
        book.record_usage("synthetic", Usage((), at+timedelta(seconds=1), "synthetic refused"))
        assert book.walled("synthetic")
        assert next(r for r in book.usage.rows() if r.group == "fable").used == .35
        book.record_usage("synthetic", snapshot({**body(), "limits": [native_row(20)]}, at+timedelta(seconds=2)))
        assert not book.walled("synthetic")


def test_old_book_reading_does_not_migrate_and_writer_adds_explicit_wall_default(tmp_path):
    path = tmp_path / "ledger.sqlite"
    at = post.now()
    with Ledger(path) as book:
        book.record_usage("synthetic", Usage((Window("7d", .8, at+timedelta(days=7)),), at))
        book.db.execute("ALTER TABLE pools DROP COLUMN exhausted")
        book.db.commit()
    before = path.read_bytes()
    with Ledger.reading(path) as book:
        assert not book.usage.rows()[0].exhausted and book.usage_known("synthetic")
    assert path.read_bytes() == before
    with Ledger(path) as book:
        assert not book.usage.rows()[0].exhausted
        assert "exhausted" in {r[1] for r in book.db.execute("PRAGMA table_info(pools)")}
        assert book.usage.rows()[0].used == .8


def test_last_choice_can_return_to_an_earlier_reset_model_allowance(scoped):
    r = scoped
    windows(r, "second", fable=1)
    windows(r, "third")
    r.svc.failover()
    assert r.seat.pool.name == "third"
    windows(r, "third", fable=1)
    assert facts(r).held == ["worker"]
    windows(r, "second", fable=.1)
    assert facts(r).held == []
    r.svc.failover()
    assert r.seat.pool.name == "second"
    launched = r.seat.launched()
    for _ in range(3):
        r.svc.failover()
    assert r.seat.launched() == launched


def test_record_model_wins_over_conflicting_screen_model(scoped, monkeypatch):
    from quarterdeck.harness.types import Screen
    r = scoped
    model(r, "claude-fable-5-2", declared="claude-opus-5-5")
    monkeypatch.setattr(type(r.seat.harness), "screen", lambda *a: Screen("", "", shows={"model": "claude-opus-5-5"}))
    windows(r, "second", fable=1)
    assert quota.current_walled(r.seat, r.svc.ledger)


def test_cross_program_destination_uses_its_model_and_fresh_carriage(running):
    r = running
    choices = {name: dict(values) for name, values in r.seat.spec.pool_choices.items()}
    choices["second"]["model"] = "claude-fable-5-2"
    choices["third"]["model"] = "claude-opus-5-5"
    r.seat.spec = replace(r.seat.spec, pool_choices=choices)
    r.seat.ship.seats["worker"] = r.seat.spec
    windows(r, "second", fable=1)
    windows(r, "third", fable=1)
    old = r.source.path.read_bytes()
    message = r.svc.store.new("order", "issuer", "worker", "synthetic context to carry")
    r.svc.failover()
    assert r.seat.pool.name == "third" and r.seat.harness.name == "claude"
    assert r.seat.spec.model == "claude-opus-5-5"
    assert "--resume" not in r.launches[-1] and post.typed([message]) in r.launches[-1][-1]
    assert r.source.path.read_bytes() == old


@pytest.mark.parametrize("unknown", [None, "synthetic-unknown", "claude-fable-opus"])
def test_unknown_or_ambiguous_model_cannot_prove_scoped_wall_or_destination_room(scoped, unknown, monkeypatch):
    r = scoped
    windows(r, "second", fable=1)
    windows(r, "third")
    monkeypatch.setattr(watch, "running_settings", lambda seat: {"model": unknown} if unknown else {})
    assert not quota.current_walled(r.seat, r.svc.ledger)
    choices = {name: dict(values) for name, values in r.seat.spec.pool_choices.items()}
    choices["third"]["model"] = unknown
    r.seat.spec = replace(r.seat.spec, pool_choices=choices)
    assert not r.svc.known_open(r.seat, "third")


@pytest.mark.parametrize("changed", ["used", "resets_at", "read_at", "group_name", "blocks"])
def test_old_writer_cannot_leave_a_severity_wall_attached_to_a_replaced_measurement(tmp_path, changed):
    from quarterdeck.harness.claude_limits import snapshot
    at = post.now()
    path = tmp_path / "ledger.sqlite"
    with Ledger(path) as book:
        book.record_usage("synthetic", snapshot({**body(), "limits": [native_row(35, "critical")]}, at))
        assert book.walled("synthetic")
        replacement = {"used": .2, "resets_at": (at+timedelta(days=8)).isoformat(),
                       "read_at": (at+timedelta(seconds=1)).isoformat(), "group_name": "opus", "blocks": 0}[changed]
        # The old schema writer updates ordinary columns and never touches the new evidence column.
        book.db.execute(f"UPDATE pools SET {changed}=? WHERE window='fable / 7d'", (replacement,))
        book.db.commit()
    with Ledger.reading(path) as book:
        assert not next(r for r in book.usage.rows() if r.window == "fable / 7d").exhausted
        assert not book.walled("synthetic")
        assert book.usage_known("synthetic") == (changed == "blocks")


@pytest.mark.parametrize("old", ["2ac327daf56413395e7a4f6e7598c5906530570a"], indirect=True)
def test_actual_settled_base_writer_invalidates_the_new_wall_evidence(tmp_path, old):
    from quarterdeck.harness.claude_limits import snapshot
    at = post.now()
    path = tmp_path / "ledger.sqlite"
    with Ledger(path) as book:
        book.record_usage("synthetic", snapshot({**body(), "limits": [native_row(35, "critical")]}, at))
        assert book.walled("synthetic")
    old('''
from datetime import datetime
import sys
from quarterdeck.ledger import Ledger
from quarterdeck.harness.limits import Usage, Window
with Ledger(Path(sys.argv[1])) as book:
    reset=datetime.fromisoformat(sys.argv[3]); at=datetime.fromisoformat(sys.argv[2])
    book.record_usage("synthetic", Usage((Window("fable / 7d", .2, reset, group="fable"),), at))
''', path, (at+timedelta(seconds=1)).isoformat(), body()["seven_day"]["resets_at"])
    with Ledger.reading(path) as book:
        row = next(r for r in book.usage.rows() if r.group == "fable")
        assert row.used == .2 and not row.exhausted and not book.walled("synthetic")
        assert not book.usage_known("synthetic") and "no longer matches" in row.error
