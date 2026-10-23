"""Core LLM projection shared by native definitions and the legacy MCP adapter.

Only the injected invocation callback executes work. Catalogue construction
preserves schema normalization, metadata and pinned-tool conversion fallbacks.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from functools import cache
from typing import Any, cast

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm

from ha_mcp.tool_api.llm_projection import normalise_schema

from .const import DATA_LLM_API_UNSUB, DOMAIN
from .llm_api_search import CALL_TOOL_NAME as _CALL_TOOL_NAME
from .llm_api_search import SEARCH_TOOL_NAME as _SEARCH_TOOL_NAME
from .llm_api_search import HaMcpSearchTool
from .llm_tool_exposure import META_PARAMS_KEY, _tool_meta_namespace

_LOGGER = logging.getLogger(__name__)


CALL_TOOL_DESCRIPTION = (
    "Execute a Home Assistant MCP tool by name with a dictionary of "
    f"arguments. Discover tools and their schemas with {_SEARCH_TOOL_NAME} first."
)
CALL_TOOL_PARAMETERS = vol.Schema(
    {vol.Required("name"): str, vol.Optional("arguments", default=dict): dict}
)


def convert_parameters(
    tool: Any, schema: Any, *, converter: Callable[[Any], Any] | None = None
) -> Any:
    try:
        return (converter or convert_to_voluptuous)(schema)
    except Exception:
        _LOGGER.warning(
            "Skipping tool %s: could not convert its input schema",
            tool.name,
            exc_info=True,
        )
        return None


def build_full_tools(
    exposed: list[Any],
    mirror: Callable[[Any, Any], llm.Tool],
    convert: Callable[[Any, Any], Any] = convert_parameters,
    *,
    normalize: Callable[[Any, str], Any] = normalise_schema,
) -> list[llm.Tool]:
    tools = []
    for tool in exposed:
        schema = normalize(_tool_input_schema(tool), tool.name)
        if (parameters := convert(tool, schema)) is not None:
            tools.append(mirror(tool, parameters))
    return tools


def build_search_tools(
    exposed: list[Any],
    pinned: set[str],
    mirror: Callable[[Any, Any], llm.Tool],
    convert: Callable[[Any, Any], Any],
    caller: Callable[[set[str]], llm.Tool],
    *,
    normalize: Callable[[Any, str], Any] = normalise_schema,
    check_enabled: Callable[[], None] | None = None,
) -> list[llm.Tool]:
    tools: list[llm.Tool] = []
    catalog = []
    mirrored = set()
    for tool in exposed:
        schema = normalize(_tool_input_schema(tool), tool.name)
        entry = {
            "name": tool.name,
            "description": tool.description or "",
            "input_schema": schema,
        }
        params = (_tool_meta_namespace(tool) or {}).get(META_PARAMS_KEY)
        if isinstance(params, str):
            entry["params"] = params
        catalog.append(entry)
        if tool.name in pinned and (parameters := convert(tool, schema)) is not None:
            tools.append(mirror(tool, parameters))
            mirrored.add(tool.name)
    # Only successfully converted pins are direct tools; every other tool
    # retains its complete schema in discovery and can use the call callback.
    tools.append(HaMcpSearchTool(catalog, mirrored, check_enabled=check_enabled))
    tools.append(caller({tool.name for tool in exposed}))
    return tools


@cache
def _schema_converter() -> Callable[[Any], Any]:
    """Resolve the Core-provided schema converter once, off the event loop."""
    try:
        legacy = importlib.import_module("voluptuous_openapi")
    except ModuleNotFoundError as err:
        if err.name != "voluptuous_openapi":
            raise
        probatio = importlib.import_module("probatio")
        _LOGGER.warning(
            "voluptuous-openapi is not importable; converting tool schemas "
            "with probatio instead. The manifest declares it, so this is "
            "Home Assistant running with skip_pip, or a deps tree that lost "
            "the package after setup. A requirement that genuinely failed to "
            "install never reaches this line -- Home Assistant reports that "
            "itself and abandons the integration before importing it. "
            "Probatio's OpenAPI codec cannot express the node it builds for "
            "an integer, so a numeric parameter can reach the conversation "
            "agent as a string, an empty schema or a plain number."
        )
        return cast(Callable[[Any], Any], probatio.from_openapi)
    return cast(Callable[[Any], Any], legacy.convert_to_voluptuous)


def convert_to_voluptuous(schema: Any) -> vol.Schema:
    """Convert an OpenAPI schema on stable and Probatio-based HA Core."""
    return cast(vol.Schema, _schema_converter()(schema))


# Used when the server's initialize result carries no instructions (it always
# should — ha-mcp ships server-level instructions — but never render an empty
# prompt if a build does not).
_FALLBACK_API_PROMPT = (
    "The following tools are provided by the HA-MCP server running inside "
    "Home Assistant. They give full control over this Home Assistant "
    "instance: entities, automations, scripts, dashboards, helpers, and "
    "configuration."
)

_TOOL_SEARCH_PROMPT = (
    "\n\n## Tool Discovery\n"
    "This assistant uses search-based tool discovery: most tools are NOT "
    "listed directly.\n"
    f"1. Call {_SEARCH_TOOL_NAME}(query=...) to find tools for the task; "
    "results are compact: each tool's name, one-line description, and params.\n"
    f"2. Call {_SEARCH_TOOL_NAME}(tools=[...]) for the full description and "
    "input schema of the tool you will call — required before calling: the "
    "compact hit omits usage guidance, nested fields, defaults, and parameter "
    "descriptions.\n"
    f"3. Execute it with {_CALL_TOOL_NAME}(name=..., arguments={{...}}) — "
    "discovered tools are NOT directly callable here.\n"
    "4. The few tools listed directly can be called as usual.\n"
    "Search once per task, not per call — tool names stay valid all "
    "conversation."
)


def _tool_input_schema(tool: Any) -> Any:
    """``input_schema`` on SDK 2.x, ``inputSchema`` on 1.x."""
    schema = getattr(tool, "input_schema", None)
    if schema is None:
        schema = getattr(tool, "inputSchema", None)
    return schema


def async_unregister_llm_api(hass: HomeAssistant) -> None:
    """Unregister the LLM API(s) if registered (idempotent, teardown-safe)."""
    unsubs = hass.data.get(DOMAIN, {}).pop(DATA_LLM_API_UNSUB, None)
    if not unsubs:
        return
    for unsub in unsubs:
        unsub()


__all__ = [
    "CALL_TOOL_DESCRIPTION",
    "CALL_TOOL_PARAMETERS",
    "_FALLBACK_API_PROMPT",
    "_TOOL_SEARCH_PROMPT",
    "async_unregister_llm_api",
    "build_full_tools",
    "build_search_tools",
    "convert_parameters",
    "convert_to_voluptuous",
]
