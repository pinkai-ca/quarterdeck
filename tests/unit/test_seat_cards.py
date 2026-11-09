"""The seat cards page: one card per declared seat, from a status reading and the ship file, nothing fetched."""

from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path

from quarterdeck import ship as shipmod

ROOT = Path(__file__).resolve().parents[2]


def seat_cards():
    spec = importlib.util.spec_from_file_location("seat_cards", ROOT / "dashboard" / "seat_cards.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def reading() -> dict:
    return {
        "service": "ok",
        "seats": [
            {"name": "captain", "activity": {"state": "working", "detail": ""}, "door": {"open": True}, "order": "ord-1",
             "today": {"known": 1_234_567, "unmeasured": 0}, "pool": "fake", "model": "fake-1", "harness": "fake", "version": None},
            {"name": "desk", "activity": {"state": "idle", "detail": ""}, "door": {"open": False, "reason": "a human's session"},
             "order": None, "today": {"known": 0, "unmeasured": 0}, "pool": "fake", "model": "fake-1", "harness": "fake", "version": None},
        ],
        "open_orders": [{"id": "ord-1", "kind": "order", "subject": "Fix <the> door & its test"}],
        "facts": {"needs_human": ["desk"]},
    }


def test_one_card_per_declared_seat_in_its_session_with_every_text_escaped():
    ship = shipmod.load(ROOT / "ships" / "drydock")
    page = seat_cards().page(ship, reading(), dt.datetime(2026, 11, 9, 7, 0, tzinfo=dt.timezone.utc))
    assert page.count('<article class="seat"') == len(ship.seats)
    for seat in ship.seats.values():
        assert f"<h2>{seat.display}</h2>" in page
    for session in ship.sessions.values():
        assert f"<h3>{session.title}</h3>" in page
    assert "Fix &lt;the&gt; door &amp; its test" in page and "<the>" not in page
    assert "1.2M" in page and 'data-state="working"' in page and 'data-state="attention"' in page
    assert 'data-state="down"' in page  # a declared seat absent from the reading still has its card


def test_the_page_fetches_nothing():
    ship = shipmod.load(ROOT / "ships" / "drydock")
    page = seat_cards().page(ship, reading())
    for needle in ("src=", "href=", "url(", "@import", "<script"):
        assert needle not in page
