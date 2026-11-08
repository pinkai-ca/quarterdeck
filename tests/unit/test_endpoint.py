"""Endpoint pools: what an endpoint serves, the model a seat takes from it, and the key beside it."""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path

import pytest

from quarterdeck import endpoint
from quarterdeck.ship import Pool, ShipError

LISTING = {"object": "list", "data": [{"id": "qwen3.8", "object": "model", "max_model_len": 98304},
                                      {"id": "qwen3.8-27b", "object": "model", "max_model_len": 98304},
                                      {"id": "other", "object": "model"}, {"not": "a model"}]}


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def answer(payload):
    def urlopen(request, timeout=0):
        answer.asked = request.full_url
        if isinstance(payload, Exception):
            raise payload
        return Response(json.dumps(payload).encode())
    return urlopen


def test_models_reads_the_listing_and_the_context_length_where_the_server_states_one(monkeypatch):
    monkeypatch.setattr(endpoint.urllib.request, "urlopen", answer(LISTING))
    served = endpoint.models("http://127.0.0.1:8000/v1/")
    assert answer.asked == "http://127.0.0.1:8000/v1/models"
    assert served == [endpoint.Served("qwen3.8", 98304), endpoint.Served("qwen3.8-27b", 98304), endpoint.Served("other", None)]


def test_an_endpoint_that_does_not_answer_or_answers_badly_is_a_refusal_naming_it(monkeypatch):
    monkeypatch.setattr(endpoint.urllib.request, "urlopen", answer(urllib.error.URLError("connection refused")))
    with pytest.raises(ShipError, match="http://127.0.0.1:1/v1 does not answer: connection refused"):
        endpoint.models("http://127.0.0.1:1/v1")
    monkeypatch.setattr(endpoint.urllib.request, "urlopen", answer(urllib.error.HTTPError("u", 404, "nope", {}, None)))
    with pytest.raises(ShipError, match="answered 404"):
        endpoint.models("http://127.0.0.1:1/v1")
    monkeypatch.setattr(endpoint.urllib.request, "urlopen", answer({"hello": "world"}))
    with pytest.raises(ShipError, match="something other than a model listing"):
        endpoint.models("http://127.0.0.1:1/v1")


def test_choose_takes_the_named_model_or_the_first_and_says_where_the_context_comes_from(monkeypatch):
    monkeypatch.setattr(endpoint, "models", lambda url, timeout=0: [endpoint.Served("a", 4096), endpoint.Served("b", None)])
    pool = Pool("local", "local", endpoint="http://127.0.0.1:8000/v1")
    assert endpoint.choose(pool, "a") == endpoint.Choice("local", "a", 4096, 1024, None)
    first = endpoint.choose(pool, None)
    assert first.model == "a" and first.output == 1024, "output capped to a quarter of a small context"
    unknown = endpoint.choose(pool, "b")
    assert unknown.context == endpoint.DEFAULT_CONTEXT and "states no context length" in unknown.note
    declared = endpoint.choose(Pool("local", "local", endpoint="http://127.0.0.1:8000/v1", context=200000), "b")
    assert declared.context == 200000 and declared.output == 8192 and declared.note is None
    with pytest.raises(ShipError, match="does not serve 'c'; it serves a, b"):
        endpoint.choose(pool, "c")
    monkeypatch.setattr(endpoint, "models", lambda url, timeout=0: [])
    with pytest.raises(ShipError, match="serves no model"):
        endpoint.choose(pool, None)
    with pytest.raises(ShipError, match="names no endpoint"):
        endpoint.choose(Pool("codex", "codex", config_dir=Path("/x")), None)


def test_a_refusal_from_the_server_names_the_pool_and_what_would_lift_it(monkeypatch):
    def down(url, timeout=0):
        raise ShipError(f"{url} does not answer: connection refused")
    monkeypatch.setattr(endpoint, "models", down)
    with pytest.raises(ShipError, match="pool local: http://127.0.0.1:1/v1 does not answer: connection refused; start the server"):
        endpoint.choose(Pool("local", "local", endpoint="http://127.0.0.1:1/v1"), None)


def test_the_choice_is_written_in_the_home_and_read_back_at_launch(tmp_path: Path):
    choice = endpoint.Choice("local", "a", 4096, 1024, "a note")
    choice.write(tmp_path)
    assert endpoint.Choice.read(tmp_path) == choice
    with pytest.raises(ShipError, match="holds no model choice"):
        endpoint.Choice.read(tmp_path / "nowhere")


def test_the_key_beside_an_endpoint_travels_by_environment_and_describe_lists_what_is_served(monkeypatch):
    assert endpoint.key(Pool("local", "local", endpoint="http://127.0.0.1:8000/v1")) == {}
    monkeypatch.setenv("SOME_KEY", "value")
    assert endpoint.key(Pool("remote", "x", endpoint="https://example.invalid/v1", api_key="env:SOME_KEY")) == {"QD_ENDPOINT_KEY": "value"}
    monkeypatch.setattr(endpoint, "models", lambda url, timeout=0: [endpoint.Served("a", 98304), endpoint.Served("b", None)])
    assert endpoint.describe(Pool("local", "local", endpoint="http://127.0.0.1:8000/v1")) == "http://127.0.0.1:8000/v1 answers; serves a (98,304 tokens), b"
