"""Transport-neutral descriptions, schemas and rich results for LLM adapters.

The engine supplies authoritative definitions and results. This module only
projects their presentation; it never dispatches a tool or grants authority.
"""

from __future__ import annotations

import copy
import json
import logging
import math
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import ToolDefinition, ToolResult

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ToolDescription:
    """A presentation view, derived from the engine's existing definition."""

    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any]
    output_schema: dict[str, Any] | None
    tags: frozenset[str]
    meta: dict[str, Any]

    @property
    def title(self) -> str | None:
        return self.annotations.get("title")


def describe_tool(definition: ToolDefinition) -> ToolDescription:
    support = definition.meta.get("ha_mcp", {}).get("native_support", {})
    note = support.get("note", "")
    return ToolDescription(
        definition.name,
        definition.description + (f"\n\nNative support: {note}" if note else ""),
        copy.deepcopy(definition.input_schema),
        dict(definition.annotations),
        copy.deepcopy(definition.output_schema),
        definition.tags,
        copy.deepcopy(definition.meta),
    )


def result_envelope(result: ToolResult) -> dict[str, Any]:
    """Keep the established rich Assist result without an MCP model round-trip."""
    if result.images:
        raise ValueError("The Assist adapter must handle unsupported images first")
    text = (
        result.data
        if isinstance(result.data, str)
        else json.dumps(result.data, default=str)
    )
    return {
        "content": [{"type": "text", "text": text}],
        **({"structuredContent": result.data} if isinstance(result.data, dict) else {}),
        "isError": result.is_error,
        "meta": {
            "ha_mcp": {"status": result.status, "operation_id": result.operation_id}
        },
    }


# Keywords whose value is a mapping keyed by author-chosen names — property
# names, definition names, regexes — rather than by JSON Schema keywords. A key
# spelled like a bound is one of those names and must not be rewritten.
_SCHEMA_MAPS: frozenset[str] = frozenset(
    {
        "properties",
        "patternProperties",
        "$defs",
        "definitions",
        "dependentSchemas",
        "dependentRequired",
        # Draft-7's predecessor of the two above; still emitted by some servers.
        "dependencies",
    }
)
# Keywords whose value is instance data rather than a subschema. A key spelled
# like a bound inside one of these is a value in someone's default, not a bound.
# ``example`` is OpenAPI's singular spelling of ``examples``.
_INSTANCE_VALUES: frozenset[str] = frozenset(
    {"default", "const", "enum", "examples", "example"}
)
# Keywords whose value is neither a subschema nor a map of subschemas.
# OpenAPI's ``discriminator`` holds a ``propertyName`` and a ``mapping`` of
# author-chosen tags to ``$ref`` strings; a tag spelled like a bound is one of
# those names, and its ``$ref`` value would read as a non-numeric bound and be
# dropped, corrupting a valid discriminated union. OpenAPI ``x-`` specification
# extensions carry arbitrary vendor objects the same way and are matched by
# prefix in ``_is_opaque_key``.
_OPAQUE_KEYWORDS: frozenset[str] = frozenset({"discriminator"})


def _is_opaque_key(key: str) -> bool:
    """True for a keyword whose value must be copied through untouched."""
    return key in _OPAQUE_KEYWORDS or key.startswith("x-")


# Each exclusive keyword, the inclusive twin it folds into, the picker that
# keeps the tighter of the two when both are present, and the step to the
# nearest integer the bound still admits.
_EXCLUSIVE_BOUNDS: tuple[
    tuple[str, str, Callable[[Any, Any], Any], Callable[[Any], int]], ...
] = (
    ("exclusiveMinimum", "minimum", max, lambda bound: math.floor(bound) + 1),
    ("exclusiveMaximum", "maximum", min, lambda bound: math.ceil(bound) - 1),
)


@dataclass
class _Rewrite:
    """What one schema normalisation changed, so a caller can log it once.

    The walk is recursive and knows no tool name; collecting here lets the
    entry point say what happened to which tool in a single line, rather than
    once per node or not at all.
    """

    folded: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)


def _to_inclusive_bounds(schema: Any, rewrite: _Rewrite | None = None) -> Any:
    """Return the schema with every exclusive numeric bound made inclusive.

    Home Assistant re-emits the converted schema through Probatio's OpenAPI
    codec, which defaults to OpenAPI 3.0 and writes an exclusive bound the
    Draft-4 way: ``minimum`` beside ``exclusiveMinimum: true``. The Anthropic
    API validates ``input_schema`` as JSON Schema draft 2020-12, where
    ``exclusiveMinimum`` must be a number, and rejects the entire request — so
    one such bound anywhere in the mirrored toolset fails every conversation
    turn, not just calls to the tool carrying it.

    On an integer-only node the fold is exact -- ``exclusiveMinimum: 1``
    becomes ``minimum: 2``, the smallest integer the bound admits. Elsewhere
    the advertised edge widens by one representable point; the server still
    enforces the real bound when the call arrives, so nothing becomes callable
    that was not callable before. A boolean is the Draft-4 flag rather than a
    bound and is dropped outright.

    Normalising here rather than only at the source covers the server versions
    this component does not control: the ha-mcp package installs and updates
    independently of the component, and an explicit pip-spec pins it outright.
    """
    if isinstance(schema, list):
        return [_to_inclusive_bounds(item, rewrite) for item in schema]
    if not isinstance(schema, dict):
        return schema

    result: dict[str, Any] = {}
    for key, value in schema.items():
        if key in _INSTANCE_VALUES or _is_opaque_key(key):
            # Copied, not aliased: the result is handed to Core and kept in
            # the search catalog, and neither may reach back into the MCP
            # result object this schema came from.
            result[key] = copy.deepcopy(value)
        elif key in _SCHEMA_MAPS and isinstance(value, dict):
            result[key] = {
                name: _to_inclusive_bounds(sub, rewrite) for name, sub in value.items()
            }
        else:
            result[key] = _to_inclusive_bounds(value, rewrite)

    _fold_bounds(result, rewrite)
    return result


def _is_number(value: Any) -> bool:
    """True for a JSON number. bool is an int subclass and is not one."""
    return not isinstance(value, bool) and isinstance(value, int | float)


def _admits_integers_only(node: dict[str, Any]) -> bool:
    """True when the node's declared type accepts integers and nothing wider.

    A union that also admits ``number`` is not tightened: the excluded edge is
    still reachable there as a fraction.
    """
    declared = node.get("type")
    if isinstance(declared, str):
        return declared == "integer"
    if isinstance(declared, list):
        return "integer" in declared and "number" not in declared
    return False


def _fold_bounds(node: dict[str, Any], rewrite: _Rewrite | None = None) -> None:
    """Fold each exclusive keyword into its inclusive twin, in place."""
    for exclusive, inclusive, tighter, to_integer in _EXCLUSIVE_BOUNDS:
        if exclusive not in node:
            continue
        bound = node.pop(exclusive)
        if not _is_number(bound):
            # Nothing non-numeric is a readable bound in a subschema slot, and
            # the slot is all this can be: the name maps, the instance-value
            # keywords and the opaque ones above keep foreign data out of
            # reach. Probatio refuses a string, a list and the Draft-4 boolean
            # outright -- costing the whole tool -- and silently retypes a null
            # number param to a string one. Dropping the key repairs all four.
            if rewrite is not None:
                rewrite.dropped.append(exclusive)
            continue
        # On an integer-only node the exact inclusive equivalent exists, so
        # take it: folding ``exclusiveMinimum: 1`` to ``minimum: 1`` would
        # advertise a value the server rejects.
        if _admits_integers_only(node):
            bound = to_integer(bound)
        # ``tighter`` when the schema already carried an inclusive bound, so
        # that bound is never loosened to the exclusive one.
        current = node.get(inclusive)
        node[inclusive] = tighter(current, bound) if _is_number(current) else bound
        if rewrite is not None:
            rewrite.folded.append(exclusive)


def _tally(keywords: list[str]) -> str:
    """Render the rewritten keywords with a count, so two nodes read as two.

    One line per tool keeps the log readable, but a bare set of keyword names
    would make a schema with several malformed bounds indistinguishable from
    one carrying a single bad node -- and the count is the only hint left that
    more than one place needs fixing.
    """
    return ", ".join(
        keyword if seen == 1 else f"{keyword} x{seen}"
        for keyword, seen in sorted(Counter(keywords).items())
    )


def normalise_schema(
    schema: Any, tool_name: str, *, logger: logging.Logger | None = None
) -> Any:
    """Normalise one tool's schema and report, once, what that changed.

    Both publication paths reach the model: the mirrored parameters Core
    converts, and the tool-search catalog, which is returned verbatim and
    passes no converter at all. Both call this, so a tool is normalised once
    per turn and logged once per turn whichever path published it.
    """
    logger = logger or _LOGGER
    rewrite = _Rewrite()
    result = _to_inclusive_bounds(schema, rewrite)
    if rewrite.dropped:
        logger.warning(
            "Dropped %s from %s's schema: the value is not a number, so it is "
            "either the Draft-4 boolean flag or malformed, and Home "
            "Assistant's codec answers such a node by refusing the tool or by "
            "retyping the parameter. The server should advertise the bound as "
            "a number, the only form draft 2020-12 permits.",
            _tally(rewrite.dropped),
            tool_name,
        )
    if rewrite.folded:
        logger.debug(
            "Rewrote %s in %s's schema to the inclusive twin so Home Assistant "
            "does not re-emit it in the Draft-4 form. On a non-integer bound "
            "the advertised edge is now one representable point wider than the "
            "server enforces, so a model passing that edge gets a rejection "
            "that is otherwise unattributable from any log.",
            _tally(rewrite.folded),
            tool_name,
        )
    return result
