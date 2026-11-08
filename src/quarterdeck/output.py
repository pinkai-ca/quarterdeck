"""One collected reading, rendered for a person or serialized for a caller."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable
import json
import sys


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


def emit(result: Any, *, json_output: bool = False) -> None:
    """Serialize/render completely, print and flush. Callers may receipt only after return."""
    if result is None:
        return
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
    if sys.stdout.write(document) != len(document):
        raise OSError("incomplete reading output; receipt withheld")
    sys.stdout.flush()
