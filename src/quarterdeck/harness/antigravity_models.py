"""Exact Antigravity scope facts; names alone establish no quota membership.

Populate only from explicit program/sign-in statements, with reviewed provenance.
The empty tables deliberately leave real membership and all-model scope unknown.
"""

# An explicit relation (including an explicit exclusion), never a family classifier.
MEMBERSHIP: dict[tuple[str, str], bool] = {}
ALL_MODEL_GROUPS: frozenset[str] = frozenset()
# Each spelling -> canonical native identifier, only when the program explicitly equates them.
MODEL_IDENTITIES: dict[str, str] = {}


def applies(group: str | None, model: str | None) -> bool | None:
    if group is None:
        return True  # an ungrouped window applies to every model, as everywhere in the ship
    if group in ALL_MODEL_GROUPS:
        return True
    if not model:
        return None
    return MEMBERSHIP.get((group, MODEL_IDENTITIES.get(model, model)))


def event_applies(event, model: str | None) -> bool | None:
    if event.all_models:
        return True
    if event.group is not None:
        return applies(event.group, model)
    return same_model(event.model, model)


def same_model(left: str | None, right: str | None) -> bool | None:
    if not left or not right:
        return None
    if left == right:
        return True
    left, right = MODEL_IDENTITIES.get(left), MODEL_IDENTITIES.get(right)
    return left == right if left is not None and right is not None else None
