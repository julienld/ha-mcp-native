"""Bounded scene discovery with verified storage keys and optional body search."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..client.rest_client import (
    HomeAssistantAPIError,
)
from ..client.websocket_client import get_websocket_client
from ..runtime.scene_discovery import build_scene_inventory
from .component_api import (
    DEVICE_REGISTRY_CHILD_SEMANTICS,
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .component_registry_lookup import resolve_entities_via_component

logger = logging.getLogger(__name__)
CONFIG_SCAN_TIMEOUT = 10.0
CONFIG_SCAN_CONCURRENCY = 4
REGISTRY_CHUNK_SIZE = 500


async def _component_inventory(client: Any) -> list[dict[str, Any]] | None:
    """Enumerate scenes through the existing capability-gated search command."""
    try:
        caps = await get_component_caps(client)
        if not (
            component_supports(caps, "search")
            and component_supports(caps, DEVICE_REGISTRY_CHILD_SEMANTICS)
        ):
            return None
        assert caps is not None
        advertised = caps.limits.get("max_results", 500)
        limit = (
            min(advertised, 500)
            if isinstance(advertised, int)
            and not isinstance(advertised, bool)
            and advertised > 0
            else 500
        )
        async with asyncio.timeout(CONFIG_SCAN_TIMEOUT):
            ws = await get_websocket_client(
                url=client.base_url,
                token=client.token,
                verify_ssl=getattr(client, "verify_ssl", None),
            )
            return await _component_inventory_pages(ws, limit)
    except Exception as exc:  # noqa: BLE001
        # Optional component reads always retain the independent REST fallback.
        if is_unknown_command(exc):
            invalidate_caps(client)
        logger.debug("Scene inventory component read fell back to REST: %r", exc)
        return None


async def _component_inventory_pages(
    ws: Any, limit: int
) -> list[dict[str, Any]] | None:
    """Read every inventory page; discard incomplete or malformed snapshots."""
    rows: dict[str, dict[str, Any]] = {}
    offset = 0
    while True:
        raw = await ws.send_command(
            "ha_mcp_tools/search",
            search_types=["entity"],
            domain_filter="scene",
            include_hidden=True,
            limit=limit,
            offset=offset,
        )
        result = raw.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("entities"), list):
            return None
        if not isinstance(result.get("entity_has_more"), bool):
            return None
        if result.get("partial") or result.get("diagnostics"):
            return None
        page = result["entities"]
        if not all(
            isinstance(row, dict) and str(row.get("entity_id", "")).startswith("scene.")
            for row in page
        ):
            return None
        previous_count = len(rows)
        rows.update((row["entity_id"], row) for row in page)
        if not result.get("entity_has_more"):
            return list(rows.values())
        if len(rows) == previous_count:
            return None
        offset += len(page)


async def _registry_rows(
    client: Any, entity_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """Read only scene registry entries when the component is available."""
    rows: list[dict[str, Any]] = []
    for start in range(0, len(entity_ids), REGISTRY_CHUNK_SIZE):
        result = await resolve_entities_via_component(
            client, entity_ids[start : start + REGISTRY_CHUNK_SIZE]
        )
        if result is None:
            raw = await client.send_websocket_message(
                {"type": "config/entity_registry/list"}
            )
            if raw.get("success") is False or not isinstance(raw.get("result"), list):
                raise HomeAssistantAPIError("Could not read the scene entity registry")
            rows = raw["result"]
            break
        rows.extend(result["entities"])
    requested = set(entity_ids)
    return {row["entity_id"]: row for row in rows if row.get("entity_id") in requested}


async def _inventory(client: Any) -> list[dict[str, Any]]:
    """Join names and owning platforms without treating vendor IDs as storage IDs."""
    states = await _component_inventory(client)
    if states is None:
        states = [
            row
            for row in await client.get_states()
            if row["entity_id"].startswith("scene.")
        ]
    registry = await _registry_rows(client, [row["entity_id"] for row in states])
    return build_scene_inventory(states, registry)


async def discover_scenes(
    client: Any, *, query: str | None, search_in_config: bool, limit: int, offset: int
) -> dict[str, Any]:
    from ha_mcp.backends.remote import RemoteBackend
    from ha_mcp.runtime.scene_discovery import discover_scenes as discover
    from ha_mcp.tool_api import CallContext

    return await discover(
        RemoteBackend(client),
        CallContext(None),
        query=query,
        search_in_config=search_in_config,
        limit=limit,
        offset=offset,
        scan_timeout=CONFIG_SCAN_TIMEOUT,
        scan_concurrency=CONFIG_SCAN_CONCURRENCY,
    )
