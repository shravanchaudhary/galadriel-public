"""Coerce and diagnose array-typed tool arguments before a tool acts on them.

A tool schema declares `kg_triplets` / `positive_examples` / `event_types` as
arrays, but a model on an OpenAI-compatible provider can emit an array-typed
argument as a JSON *string* — in practice when the payload grows large enough
that it stops tracking bracket depth. Every array parameter used to funnel
through an `isinstance(x, list)` gate that returned `[]` on anything else, so a
wrong *type* reached the caller as an *absent* field.

That mattered more than it looks. On 2026-08-26 a `learn` call arrived with
`kg_triplets` as a 3,900-character string; the gate emptied it and the tool
answered "content or kg_triplets is required" — telling the model it had
omitted what it had just sent. The model believed it, concluded triplets were
not valid on their own, and wrote the next twelve memories as prose drawers
instead of graph facts. One misleading string changed the memory
representation for a whole session.

So: coerce what is unambiguous, and when coercion fails, say what actually
arrived and what to send instead. Never report a wrong type as a missing one.
"""

from __future__ import annotations

import json

# `json.loads` reports trailing content after the first complete value with
# this message. It is the signature of several JSON values concatenated —
# which is what a model produces when it tries to pack multiple objects into
# one argument that only accepts a single array.
_EXTRA_DATA = "Extra data"

_MAX_ECHO = 80


def _echo(raw: str) -> str:
    """A short, safe quotation of what arrived, for the error message."""
    flat = " ".join(raw.split())
    return flat if len(flat) <= _MAX_ECHO else f"{flat[:_MAX_ECHO]}…"


def as_list(value, field: str, *, multi_hint: str = "") -> tuple[list | None, str | None]:
    """Return (list, None) or (None, error). Call only when value is not None.

    `multi_hint` is appended when the payload looks like several JSON values
    concatenated — the caller knows what "split this up" means for its own
    tool, and a generic message cannot say it usefully.
    """
    if isinstance(value, list):
        return value, None

    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None, (
                f"{field} was an empty string. Send an array, "
                f"or omit the field entirely."
            )
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            if e.msg.startswith(_EXTRA_DATA):
                detail = (
                    f"{field} looks like several separate values concatenated "
                    f"into one argument — it stops being valid JSON at "
                    f"character {e.pos}."
                )
                return None, f"{detail} {multi_hint}".strip()
            return None, (
                f"{field} was sent as a {len(raw)}-character string that is not "
                f"valid JSON ({e.msg} at character {e.pos}). Send it as an "
                f"array value, not as a quoted string. Got: {_echo(raw)}"
            )
        if not isinstance(parsed, list):
            return None, (
                f"{field} parsed to a {type(parsed).__name__}, but an array is "
                f"required. Got: {_echo(raw)}"
            )
        return parsed, None

    return None, (
        f"{field} must be an array; got {type(value).__name__}. "
        f"Send an array value, or omit the field entirely."
    )


def as_str_list(value, field: str) -> tuple[list[str] | None, str | None]:
    """`as_list`, then require every element to be a string.

    Cue arrays are whole-array replacements, so a silently skipped element is a
    silently deleted cue. Name the bad element instead of dropping it.
    """
    items, error = as_list(value, field)
    if error:
        return None, error
    bad = [
        f"#{i + 1} ({type(v).__name__})"
        for i, v in enumerate(items)
        if not isinstance(v, str)
    ]
    if bad:
        return None, (
            f"{field} must contain only strings; these are not: "
            f"{', '.join(bad[:5])}"
            + (f" and {len(bad) - 5} more" if len(bad) > 5 else "")
        )
    return items, None
