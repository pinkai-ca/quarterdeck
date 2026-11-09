"""The seat cards page: one card per declared seat, from a status reading and the ship file, nothing fetched.

The page keeps the reading's register: what the reading could not see stays unknown, a count with
unmeasured turns keeps its +, and a value taken only from the ship file reads as declared."""

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


def seat(name: str, **fields) -> dict:
    base = {"name": name, "activity": {"state": "idle", "detail": ""}, "door": {"open": True}, "order": None,
            "today": {"known": 0, "unmeasured": 0}, "pool": "fake", "model": "fake-1", "harness": "fake", "version": None}
    return base | fields


def reading(*seats: dict) -> dict:
    return {"service": "ok", "seats": list(seats),
            "open_orders": [{"id": "ord-1", "kind": "order", "subject": "Fix <the> door & its test"}],
            "facts": {"needs_human": []}}


def drydock():
    return shipmod.load(ROOT / "ships" / "drydock")


def card_of(page: str, persona: str) -> str:
    start = page.index(f"<h2>{persona}</h2>")
    return page[page.rindex("<article", 0, start):page.index("</article>", start)]


def test_one_card_per_declared_seat_in_its_session_with_every_text_escaped():
    ship = drydock()
    page = seat_cards().page(ship, reading(seat("captain", activity={"state": "working", "detail": "Bash"}, order="ord-1",
                                                today={"known": 1_234_567, "unmeasured": 0})),
                             dt.datetime(2026, 11, 9, 7, 0, tzinfo=dt.timezone.utc))
    assert page.count('<article class="seat"') == len(ship.seats)
    for spec in ship.seats.values():
        assert f"<h2>{spec.display}</h2>" in page
    for session in ship.sessions.values():
        assert f"<h3>{session.title}</h3>" in page
    captain = card_of(page, ship.seats["captain"].display)
    assert "Fix &lt;the&gt; door &amp; its test" in captain and "<the>" not in page
    assert 'data-state="working"' in captain and "working, Bash" in captain and "<b>1.2M</b>" in captain


def test_counts_keep_their_register_unmeasured_turns_and_absence_are_never_a_measured_zero():
    ship = drydock()
    persona = ship.seats["captain"].display
    def today_of(**fields):
        return card_of(seat_cards().page(ship, reading(seat("captain", **fields))), persona)
    assert "<b>123+</b>" in today_of(today={"known": 123, "unmeasured": 2})
    assert "<b>0+</b>" in today_of(today={"known": 0, "unmeasured": 1})
    assert "<b>0</b>" in today_of(today={"known": 0, "unmeasured": 0})
    assert "<b>unknown</b>" in today_of(today=None)


def test_what_the_reading_could_not_see_stays_unknown_and_declared_values_say_so():
    ship = drydock()
    spec = ship.seats["captain"]
    page = seat_cards().page(ship, reading(
        seat("captain", activity={"state": "unknown", "detail": "record unreadable: truncated"}, model=None)))
    captain = card_of(page, spec.display)
    assert 'data-state="unknown"' in captain and "unknown, record unreadable: truncated" in captain
    assert f"{spec.model} (declared)" in captain
    assert "effort (declared)" in captain and f"<b>{spec.effort}</b>" in captain
    absent = next(s for s in ship.seats.values() if s.name != "captain")
    missing = card_of(page, absent.display)  # a declared seat the reading does not hold
    assert 'data-state="unknown"' in missing and "not in the status reading" in missing
    assert "<b>unknown</b>" in missing and "door unknown" in missing


def test_a_seat_that_needs_a_human_is_marked_for_attention():
    ship = drydock()
    data = reading(seat("captain"))
    data["facts"]["needs_human"] = ["captain"]
    assert 'data-state="attention"' in card_of(seat_cards().page(ship, data), ship.seats["captain"].display)


def test_the_page_fetches_nothing():
    page = seat_cards().page(drydock(), reading(seat("captain")))
    for needle in ("src=", "href=", "url(", "@import", "<script"):
        assert needle not in page
