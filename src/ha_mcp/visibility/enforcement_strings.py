"""Nested strings and boundary matching for entity visibility enforcement."""

import re
from typing import Any


def _iter_strings(value: Any) -> Any:
    """Yield every string in a nested args/result structure (dicts/lists/strings).

    Dict KEYS are yielded too: HA config payloads key maps by entity_id (a scene's
    ``entities``, a group's members), so a key is as much a reference as a value —
    and the outbound scan (``json.dumps``) sees keys, so the inbound scan must
    traverse the same surface or a hidden id positioned as a key would execute.
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from _iter_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_strings(item)


def _build_hidden_regex(hidden: set[str]) -> re.Pattern[str] | None:
    """Compile one boundary-aware alternation over the hidden set, or None if empty.

    The ``(?<![a-z0-9_]) ... (?![a-z0-9_])`` guards (case-insensitive) make
    ``sensor.foo`` match ``states.sensor.foo`` and a bare ``sensor.foo`` in a
    template, but NOT ``sensor.foo2`` or ``my_sensor.foo``.
    """
    if not hidden:
        return None
    alternation = "|".join(re.escape(eid) for eid in sorted(hidden))
    return re.compile(rf"(?<![a-z0-9_])(?:{alternation})(?![a-z0-9_])", re.IGNORECASE)
