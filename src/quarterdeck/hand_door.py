"""One order at a time at a hand's door.

A hand holds one order at a time. Its order in hand is its oldest open order or question, less any message its
provider refused (``refusal.excluded``): the reading a hand's fresh start carries (``Seat._in_hand`` takes it from
here, so the carriage and the door never disagree). What belongs to the order in hand is the order itself and every
message below it in its thread, by ``re`` at any depth.

The door types into a hand only what belongs to its order in hand, and that only while the hand's current launch
has been given no other order or question. So a correction or a further order sent ``--re`` the order in hand, and an
answer to the hand's own question in that thread, still reach it as they come. A further order or question outside
that thread waits, unread and with no attempt, until the hand closes its order in hand: reports on an order, or
answers a question, first; then the hand starts fresh
(``Service.fresh_after_order``), and that launch carries the next order in hand whole. A message that is neither an
order nor a question, and does not belong to the order in hand, asks nothing of the hand and is delivered as before.
Every held message is named on the status line (law: nothing-waits-silently). Every other role is unchanged.
"""
from __future__ import annotations

from . import post


def order_in_hand(store: post.Store, name: str, refused=None) -> post.Message | None:
    """The hand's oldest open order or question, less those its provider refused; None when nothing is open."""
    if refused is None:
        from . import refusal
        refused = refusal.excluded(store.dir.parent.parent, name)
    open_ = sorted((message for message in store.for_seat(name)
                    if message.obligation and message.id not in refused and store.is_open(message)),
                   key=lambda message: (message.at, message.id))
    return open_[0] if open_ else None


def belongs(message: post.Message, order: post.Message, by_id: dict[str, post.Message]) -> bool:
    """The order itself, or a message below it in its thread."""
    from .post_threads import _descends
    return message.id == order.id or _descends(message, {order.id}, by_id)


def given(attempts: post.Attempts, name: str, launched_at: str) -> set[str]:
    """The messages the hand's current launch was given: carried at its start, or typed to it since, confirmed or
    not. Only an attempt withheld before typing gave it nothing."""
    return {attempt.message for attempt in attempts.all()
            if attempt.seat == name and attempt.launched_at == launched_at and attempt.result != 'withheld'}


def held(seat, store: post.Store, attempts: post.Attempts, message: post.Message) -> str | None:
    """Why the door holds ``message`` for this hand now, or None: it goes as before. Every other role: None."""
    if seat.spec.role != 'hand':
        return None
    order = order_in_hand(store, seat.name)
    if order is None:
        return None
    by_id = {each.id: each for each in store.all()}
    if not belongs(message, order, by_id):
        return f'waits: its order in hand {order.short} is open' if message.obligation else None
    launched = seat.launched()
    if launched is None:
        return None
    other = sorted((each for identifier in (given(attempts, seat.name, launched.at) if attempts is not None else ())
                    if (each := by_id.get(identifier)) is not None and each.obligation
                    and not belongs(each, order, by_id)), key=lambda each: (each.at, each.id))
    if other:
        return f'waits: it comes at the fresh start after {other[-1].short}'
    return None


def closed_by_hand(store: post.Store, name: str, since, excluded=frozenset()):
    """When the hand last closed, after ``since``, a question it held in hand: by its own answer, the first, which is
    what closes a question by the post's own rule. A question someone else answered first was not closed by the hand.
    (An order the hand holds is closed by its report, which the fresh-start reading already counts.)"""
    return max((closing.at for message in store.for_seat(name)
                if message.kind == 'question' and message.id not in excluded
                and (closing := store.closing(message)) is not None and closing is not message
                and closing.sender == name and closing.at > since), default=None)


def blocks_fresh(seat, store: post.Store, attempts: post.Attempts) -> bool:
    """Whether open work holds a seat's fresh start after its order: any open message does, as before, except a
    hand's order in hand that waits at the door for that very fresh start."""
    open_ = store.open_for(seat.name)
    if not open_ or seat.spec.role != 'hand' or attempts is None:
        return bool(open_)
    order = order_in_hand(store, seat.name)
    return order is None or held(seat, store, attempts, order) is None
