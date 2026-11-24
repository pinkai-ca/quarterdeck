"""A seat leaves a subscription only when it is spent: used in full, or refused at a limit. All synthetic."""

from datetime import datetime, timedelta, timezone
import json

import pytest

from quarterdeck import post, quota
from quarterdeck.harness import claude
from quarterdeck.harness.claude_limits import refused_at_limit, snapshot
from quarterdeck.harness.limits import Usage, Wall, Window
from quarterdeck.ledger import Ledger
from .test_claude_model_allowances import body, facts, model, native_row, scoped, stable_body_clock, windows  # noqa: F401
from .test_cross_program_failover import running  # noqa: F401

AT = datetime(2040, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
RESET = AT + timedelta(days=3)


def refusal(kind="seven_day", *, at=AT, reset=RESET, **changes):
    """The shape Claude Code 2.1.317 records for a request refused at a usage limit, with invented values."""
    row = {"type": "assistant", "uuid": "synthetic-refusal", "timestamp": at.isoformat(), "isSidechain": False,
           "isApiErrorMessage": True, "error": "rate_limit", "apiError": "usage_limit_reached", "apiErrorStatus": 429,
           "apiErrorParams": {"rate_limit_info": {"status": "rejected", "resetsAt": int(reset.timestamp()),
                                                  "rateLimitType": kind}},
           "message": {"id": "synthetic-message", "model": "<synthetic>", "role": "assistant",
                       "stop_reason": "stop_sequence", "content": [{"type": "text", "text": "Synthetic limit text."}],
                       "usage": {"input_tokens": 0, "output_tokens": 0}}}
    row.update(changes)
    return row


def info(**changes):
    return {"apiErrorParams": {"rate_limit_info": {"status": "rejected", "resetsAt": int(RESET.timestamp()),
                                                   "rateLimitType": "seven_day", **changes}}}


def terminal_refusal_rows(*, at=AT, reset=RESET, session='synthetic'):
    """The loopback shape with invented input, links, time and response text."""
    root = dict(version='2.1.331', sessionId=session, isSidechain=False)
    prompt = dict(root, type='user', uuid='synthetic-input', parentUuid=None,
                  timestamp=(at - timedelta(seconds=1)).isoformat(),
                  message=dict(role='user', content='Synthetic task.'))
    rows = [prompt]
    for name in ('environment', 'model', 'deferred_tools_delta', 'agent_listing_delta', 'skill_listing',
                 'session_context', 'date', 'remote_session_change', 'prompt_snapshot'):
        rows.append(dict(root, type='attachment', attachment=dict(type=name), uuid=f'synthetic-{name}',
                         parentUuid=rows[-1]['uuid'], timestamp=prompt['timestamp']))
    error = refusal('five_hour', at=at, reset=reset, **root, parentUuid=rows[-1]['uuid'],
                    failedResponseHadNoToolCall=True)
    error['apiErrorParams']['rate_limit_info']['isUsingOverage'] = False
    error['message'].update(type='message', stop_sequence='', usage=dict(
        input_tokens=0, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0))
    return [*rows, error]


# -- the meter -----------------------------------------------------------------------------------

@pytest.mark.parametrize("severity", ["critical", "exceeded", "error", "warning", "normal"])
def test_a_window_at_ninety_one_hundredths_with_any_severity_is_no_wall(tmp_path, severity):
    at = post.now()
    measured = snapshot({**body(), "limits": [native_row(91, severity)]}, at)
    with Ledger(tmp_path / "ledger.sqlite") as book:
        book.record_usage("synthetic", measured)
        row = next(r for r in book.usage.rows() if r.group == "fable")
        assert row.used == .91 and row.warning == (severity != "warning" and severity != "normal")
        assert book.walls() == [] and not book.walled("synthetic") and book.usage_known("synthetic")


@pytest.mark.parametrize("severity", ["critical", "normal"])
def test_a_window_used_in_full_is_a_wall_whatever_its_severity(tmp_path, severity):
    at = post.now()
    with Ledger(tmp_path / "ledger.sqlite") as book:
        book.record_usage("synthetic", snapshot({**body(), "limits": [native_row(100, severity)]}, at))
        assert book.walled("synthetic")
        assert book.walls() == [f"synthetic fable / 7d · {quota.reset_text(body()['seven_day']['resets_at'])}"]


@pytest.mark.parametrize("used,walled", [(.91, False), (.999, False), (1.0, True)])
def test_other_providers_meters_wall_only_when_full(tmp_path, used, walled):
    at = post.now()
    with Ledger(tmp_path / "ledger.sqlite") as book:
        for pool in ("codex", "grok", "google"):
            book.record_usage(pool, Usage((Window("7d", used, at + timedelta(days=2)),), at))
            assert book.walled(pool) == walled


# -- the recorded refusal ------------------------------------------------------------------------

@pytest.mark.parametrize("kind,window,group", [("five_hour", "5h", None), ("seven_day", "7d", None),
                                               ("seven_day_sonnet", "sonnet / 7d", "sonnet"),
                                               ("seven_day_opus", "opus / 7d", "opus")])
def test_a_recorded_usage_limit_refusal_is_a_wall_for_its_window_until_its_reset(kind, window, group):
    wall = refused_at_limit(refusal(kind), "limit:synthetic", AT)
    assert wall == Wall("limit:synthetic", AT, RESET, f"refused at its {window} limit",
                        signature=f"claude:usage_limit_reached:{kind}:{int(RESET.timestamp())}", group=group)


@pytest.mark.parametrize("changes", [
    {"apiError": None}, {"apiError": "model_requires_usage_credits"}, {"apiError": "long_context_credits_required"},
    {"error": "overloaded"}, {"error": None}, {"isApiErrorMessage": False}, {"type": "user"},
    info(status="allowed"), info(status="allowed_warning"), info(rateLimitType="seven_day_overage_included"),
    info(rateLimitType="overage"), info(rateLimitType="seven_day_cowork"), info(rateLimitType=None),
    info(resetsAt=None), info(resetsAt=True), info(resetsAt="1"), info(resetsAt=int((AT - timedelta(seconds=1)).timestamp())),
    info(resetsAt=10**20), {"apiErrorParams": None}, {"apiErrorParams": {"rate_limit_info": []}},
])
def test_an_unknown_form_is_not_a_wall(changes):
    assert refused_at_limit(refusal(**changes), "limit:synthetic", AT) is None


def test_a_refusal_without_a_time_is_not_a_wall():
    assert refused_at_limit(refusal(), "limit:synthetic", None) is None


def write_refused_turn(path, kind="seven_day", **changes):
    rows = [{"type": "user", "uuid": "synthetic-input", "timestamp": AT.isoformat(), "isSidechain": False,
             "message": {"role": "user", "content": "Synthetic task."}},
            refusal(kind, at=AT + timedelta(seconds=1), **changes),
            {"type": "system", "subtype": "turn_duration", "timestamp": (AT + timedelta(seconds=2)).isoformat()}]
    with path.open("a") as stream:
        stream.writelines(json.dumps(row) + "\n" for row in rows)


def test_the_claude_reader_reads_the_refusal_into_the_state_and_the_turn_as_unsuccessful(tmp_path):
    path = tmp_path / "record.jsonl"
    write_refused_turn(path)
    state, turns = claude.read(path)
    wall, = state.walls
    assert wall.key == "limit:synthetic-refusal" and wall.reason == "refused at its 7d limit" and wall.resets_at == RESET
    assert turns[-1].successful is False and state.turn_open is False


def test_the_claude_reader_keeps_an_unknown_form_out_of_the_walls(tmp_path):
    path = tmp_path / "record.jsonl"
    write_refused_turn(path, apiError="model_requires_usage_credits")
    assert claude.state(path).walls == ()


def test_a_refused_model_limit_walls_its_own_family_and_a_general_one_every_family(tmp_path):
    from quarterdeck.harness.claude import ClaudeHarness
    reader = ClaudeHarness()
    fable = lambda group: reader.usage_scope(group, "claude-fable-5-2")
    sonnet = lambda group: reader.usage_scope(group, "claude-sonnet-5-5")
    now = AT + timedelta(minutes=1)
    with Ledger(tmp_path / "ledger.sqlite") as book:
        book.quota.observe("record-a", "worker", "synthetic", refused_at_limit(refusal("seven_day_sonnet"), "a", AT), now)
        assert book.walled("synthetic", scope=sonnet) and not book.walled("synthetic", scope=fable)
        assert book.quota.blocked("worker", "synthetic", scope=sonnet)
        assert book.quota.blocked("worker", "synthetic", scope=fable) is None
        assert book.walls(now=now) == [f"synthetic refused at its sonnet / 7d limit · {quota.reset_text(quota.stamp(RESET))}"]
        book.quota.observe("record-a", "worker", "synthetic", refused_at_limit(refusal("five_hour"), "b", AT), now)
        assert book.walled("synthetic", scope=fable)
        assert book.walls(now=RESET + timedelta(seconds=1)) == [], "a refusal ends at its own reset"


def test_an_older_book_gains_the_group_and_a_reading_of_it_writes_nothing(tmp_path):
    path = tmp_path / "ledger.sqlite"
    with Ledger(path) as book:
        book.db.execute("ALTER TABLE wall_events DROP COLUMN group_name")
        book.db.execute("INSERT INTO wall_events VALUES ('screen:old','e','worker','synthetic',NULL,?,?,"
                        "'individual quota reached',NULL,'sig',0)", (quota.stamp(RESET), quota.stamp(AT)))
        book.db.commit()
    before = path.read_bytes()
    with Ledger.reading(path) as book:
        assert book.quota.walls(AT) == [f"synthetic individual quota reached · {quota.reset_text(quota.stamp(RESET), True)}"]
    assert path.read_bytes() == before
    with Ledger(path) as book:
        assert "group_name" in book.quota.columns() and len(book.quota.walls(AT)) == 1


# -- moves ---------------------------------------------------------------------------------------

def native(r, pool, percent, severity):
    r.svc.ledger.record_usage(pool, snapshot({**body(), "limits": [native_row(percent, severity)]}, post.now()))


def test_a_critical_warning_at_ninety_one_hundredths_moves_no_seat(scoped):
    r = scoped
    model(r, "claude-fable-5-2")
    native(r, "second", 91, "critical")
    windows(r, "third")
    assert not quota.current_walled(r.seat, r.svc.ledger) and facts(r).held == []
    r.svc.failover()
    assert r.seat.pool.name == "second"


def test_a_window_used_in_full_moves_the_seat(scoped):
    r = scoped
    model(r, "claude-fable-5-2")
    native(r, "second", 100, "critical")
    windows(r, "third")
    r.svc.failover()
    assert r.seat.pool.name == "third"


def refused(r, kind, **changes):
    """The running seat's provider refuses its next turn at a limit, as its record shows it."""
    record = r.created[-1]
    record.begin("synthetic refused input", "claude-fable-5-2", "high")
    at = datetime.now(timezone.utc)  # the record's own clock, after its input, as the synthetic writer dates rows
    record.write({**refusal(kind, at=at, reset=at + timedelta(hours=4), **changes), "uuid": f"synthetic-{kind}",
                  "sessionId": record.session})
    record.write({"type": "system", "subtype": "turn_duration", "timestamp": at.isoformat(), "sessionId": record.session})


def test_a_recorded_refusal_at_the_limit_moves_the_seat(scoped):
    r = scoped
    model(r, "claude-fable-5-2")
    windows(r, "third")
    refused(r, "five_hour")
    r.svc.account()
    assert quota.current_walled(r.seat, r.svc.ledger)
    r.svc.failover()
    assert r.seat.pool.name == "third"


@pytest.mark.parametrize("kind,changes", [("seven_day", {"apiError": "model_requires_usage_credits"}),
                                          ("seven_day_overage_included", {}), ("seven_day_sonnet", {})])
def test_an_unknown_form_or_another_familys_refusal_moves_no_seat(scoped, kind, changes):
    r = scoped
    model(r, "claude-fable-5-2")
    windows(r, "third")
    refused(r, kind, **changes)
    r.svc.account()
    assert not quota.current_walled(r.seat, r.svc.ledger)
    r.svc.failover()
    assert r.seat.pool.name == "second"
