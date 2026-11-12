"""Captain-requested synthetic native creation-time boundaries; no native calls."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from uuid import UUID

import pytest

from quarterdeck.harness import get
from quarterdeck.harness.grok_record import first_timestamp

SECOND = datetime(2040, 1, 1, tzinfo=timezone.utc)
LAUNCH = SECOND + timedelta(milliseconds=250)


def session_uuid(milliseconds, tail=1):
    return str(UUID(int=(int(SECOND.timestamp() * 1000 + milliseconds) << 80) | (7 << 76) | (2 << 62) | tail))


def native_record(home, identity, row_identity=None):
    path = home / "sessions/synthetic-work" / identity / "updates.jsonl"
    path.parent.mkdir(parents=True)
    row = {"timestamp": int(SECOND.timestamp()), "method": "session/update", "params": {
        "sessionId": row_identity or identity, "update": {"sessionUpdate": "user_message_chunk",
        "content": {"type": "text", "text": "A fictional input in the launch second."}}}}
    path.write_text(json.dumps(row) + "\n")
    return path


def test_uuid_creation_after_fractional_launch_joins_same_second_input(tmp_path):
    path = native_record(tmp_path, session_uuid(650))
    adapter = get("grok")
    assert first_timestamp(path) == LAUNCH + timedelta(milliseconds=400)
    assert adapter.inputs(path)[0].at == SECOND  # row precision is separate
    assert adapter.join(tmp_path, LAUNCH).record == path


def test_uuid_creation_before_launch_does_not_join(tmp_path):
    native_record(tmp_path, session_uuid(100))
    assert get("grok").join(tmp_path, LAUNCH).record is None


def test_two_equal_creation_candidates_stay_ambiguous(tmp_path):
    native_record(tmp_path, session_uuid(650, 1))
    native_record(tmp_path, session_uuid(650, 2))
    joined = get("grok").join(tmp_path, LAUNCH)
    assert joined.record is None and "ambiguous" in joined.reason


@pytest.mark.parametrize("identity", ["not-a-uuid", str(UUID(int=1, version=4))])
def test_non_v7_folder_is_unknown_without_row_timestamp_fallback(tmp_path, identity):
    path = native_record(tmp_path, identity)
    assert first_timestamp(path) is None and get("grok").join(tmp_path, SECOND).record is None


@pytest.mark.parametrize("extra", [False, True])
def test_every_row_identity_must_match_the_session_folder(tmp_path, extra):
    identity, contrary = session_uuid(650), session_uuid(650, 2)
    path = native_record(tmp_path, identity, identity if extra else contrary)
    if extra:
        row = json.loads(path.read_text())
        row["params"]["sessionId"] = contrary
        with path.open("a") as stream:
            stream.write(json.dumps(row) + "\n")
    assert first_timestamp(path) is None and get("grok").join(tmp_path, LAUNCH).record is None


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_existing_adapters_keep_their_exact_join_boundary(tmp_path, monkeypatch, name):
    adapter = get(name)
    record = Path(__file__).parents[1] / f"fixtures/receipt-time/{name}-fractional.jsonl"
    at = adapter.first_timestamp(record)
    assert at is not None
    monkeypatch.setattr(adapter, "records", lambda home: [record])
    assert adapter.join(tmp_path, at).record == record
    assert adapter.join(tmp_path, at + timedelta(microseconds=1)).record is None
