"""Exact Antigravity scope facts; names alone establish no quota membership.

Populate only from explicit program/sign-in statements, with reviewed provenance.
Every relation not listed stays unknown: another model, another effort's spelling or another group.

Provenance of every row below: a live proof on agy 1.5.5 with the ship's Google sign-in, on
2026-11-13 between 19:33 and 19:35 UTC, run under the First Officer. Three turns, each one opening
turn of `qd check antigravity --model <id>` on a throwaway ship, each read by the program's own
/usage command before and after:
- gemini-3.8-flash-low, 19:33:30-19:33:38: "Gemini Models" 5h 0 -> 0.000411 and weekly
  0.020095 -> 0.020213; the Argon week stayed 0.998043; "Claude and GPT models" stayed 0.
- gemini-4-argon-low, 19:34:00-19:34:09: the Argon week 0.998043 -> 0.999394 (it ran, not
  refused: the week was not yet spent); "Gemini Models" and "Claude and GPT models" did not move.
- claude-sonnet-5-5-low, 19:34:31-19:34:36: "Claude and GPT models" 5h 0 -> 0.018541 and weekly
  0 -> 0.00927; "Gemini Models" and the Argon week did not move.
Each launch named the model by its id and the program recorded the turn under its display name,
which is the identity equality below.
"""

FLASH, ARGON, SONNET = "Gemini 3.8 Flash (Low)", "Gemini 4 Argon (Low)", "Claude Sonnet 5.5 (Low)"
GEMINI, ARGON_WEEK, CLAUDE_GPT = "Gemini Models", "Gemini 4 Argon", "Claude and GPT models"

# An explicit relation (including an explicit exclusion), never a family classifier.
MEMBERSHIP: dict[tuple[str, str], bool] = {
    (GEMINI, FLASH): True, (ARGON_WEEK, FLASH): False, (CLAUDE_GPT, FLASH): False,
    (ARGON_WEEK, ARGON): True, (GEMINI, ARGON): False, (CLAUDE_GPT, ARGON): False,
    (CLAUDE_GPT, SONNET): True, (GEMINI, SONNET): False, (ARGON_WEEK, SONNET): False,
}
ALL_MODEL_GROUPS: frozenset[str] = frozenset()
# Each spelling -> canonical native identifier, only when the program explicitly equates them:
# the launch's --model id and the display name the program recorded for that launch's turn.
MODEL_IDENTITIES: dict[str, str] = {
    "gemini-3.8-flash-low": FLASH, FLASH: FLASH,
    "gemini-4-argon-low": ARGON, ARGON: ARGON,
    "claude-sonnet-5-5-low": SONNET, SONNET: SONNET,
}


def applies(group: str | None, model: str | None) -> bool | None:
    if group is not None and group in ALL_MODEL_GROUPS:
        return True
    if group is None or not model:
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
