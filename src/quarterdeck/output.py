"""One collected reading, rendered for a person or serialized for a caller."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable
import json
import sys

from . import closed_pipe


@dataclass(frozen=True)
class Reading:
    data: Any
    render: Callable[[Any], str | list[str]]


def value(item: Any) -> Any:
    """JSON's types, with explicit encodings for the types readings carry."""
    if is_dataclass(item) and not isinstance(item, type):
        return value(asdict(item))
    if isinstance(item, (datetime, date)):
        return item.isoformat()
    if isinstance(item, Path):
        return str(item)
    if isinstance(item, dict):
        return {key: value(child) for key, child in item.items()}
    if isinstance(item, (list, tuple)):
        return [value(child) for child in item]
    return item


def with_default_ship(result: Any, name: str) -> Any:
    """A result acted on the default ship says so: its first line carries "(default ship: <name>)", and its JSON
    the field default_ship."""
    note = f"(default ship: {name})"

    def first(rendered):
        if isinstance(rendered, str):
            head, newline, rest = rendered.partition("\n")
            return f"{head} {note}" + newline + rest if head else note + newline + rest
        lines = list(rendered)
        return [f"{lines[0]} {note}" if lines[0] else note, *lines[1:]] if lines else [note]

    if isinstance(result, Reading):
        data = value(result.data)
        data = {**data, "default_ship": name} if isinstance(data, dict) else {"reading": data, "default_ship": name}
        return Reading(data, lambda _: first(result.render(result.data)))
    shape = {"proof": result} if isinstance(result, str) else {"proofs": value(result)}
    return Reading({**shape, "default_ship": name}, lambda _: first(result))


def emit(result: Any, *, json_output: bool = False, completed_act: bool = False) -> bool:
    """Render, write and flush; receipt-bearing callers keep the strict default.

    Only a caller whose act already has its outcome may tolerate a broken output
    pipe. Return true only for complete output (including no document), false for
    a tolerated broken pipe. Rendering and other output failures still raise.
    """
    if result is None:
        return True
    if isinstance(result, Reading):
        rendered = (json.dumps(value(result.data), indent=2, ensure_ascii=False, allow_nan=False)
                    if json_output else result.render(result.data))
    elif json_output:
        rendered = json.dumps(value({"proof": result} if isinstance(result, str) else result),
                              indent=2, ensure_ascii=False, allow_nan=False)
    else:
        rendered = result
    text = rendered if isinstance(rendered, str) else "\n".join(rendered)
    document = text + "\n"
    try:
        if sys.stdout.write(document) != len(document):
            raise OSError("incomplete reading output; receipt withheld")
        sys.stdout.flush()
    except BrokenPipeError:
        if not completed_act:
            raise
        closed_pipe.silence('stdout')
        return False
    return True
