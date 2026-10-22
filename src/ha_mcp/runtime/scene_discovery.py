"""Shared bounded scene discovery, exact body search and incomplete-read reporting."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from ha_mcp.tool_api import Backend, CallContext, ToolFailure

logger = logging.getLogger(__name__)
CONFIG_SCAN_TIMEOUT = 10.0
CONFIG_SCAN_CONCURRENCY = 4


def build_scene_inventory(
    states: list[dict[str, Any]], registry: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Keep live labels and storage ownership distinct from vendor scene IDs."""
    rows = []
    for state in states:
        entity_id = state["entity_id"]
        entry = registry.get(entity_id, {})
        platform = entry.get("platform")
        integration_managed = bool(platform and platform != "homeassistant")
        rows.append(
            {
                "entity_id": entity_id,
                "name": state.get("friendly_name")
                or state.get("attributes", {}).get("friendly_name")
                or entry.get("name")
                or entity_id,
                "platform": platform,
                "scene_id": None,
                "config_available": False if integration_managed else None,
                "config_status": "integration_managed"
                if integration_managed
                else "not_checked",
                "_storage_id": entry.get("unique_id")
                if platform == "homeassistant"
                else None,
            }
        )
    return sorted(rows, key=lambda row: row["entity_id"])


def _matches_identity(row: dict[str, Any], query: str) -> bool:
    """Match scene names, entity IDs, and homeassistant storage-key candidates."""
    return any(
        query in str(row.get(key) or "").casefold()
        for key in ("entity_id", "name", "_storage_id")
    )


async def _read_config(
    backend: Backend, context: CallContext, row: dict[str, Any], query: str
) -> None:
    """Bounded persisted-body lookup, never runtime state objects or expanded YAML."""
    if row["config_status"] == "integration_managed":
        return
    storage_id = row["_storage_id"]
    resolution = {
        "storage_key": storage_id or row["entity_id"].removeprefix("scene."),
        "registry_hit": bool(storage_id),
        "platform": row["platform"],
    }
    try:
        result = await backend.execute(
            "config.scene.read",
            {"identifier": row["entity_id"], "resolution": resolution},
            context,
        )
    except PermissionError:
        raise
    except ToolFailure as error:
        if error.code.startswith("AUTH_") or error.code in {
            "READ_ONLY",
            "TOOL_DISABLED",
        }:
            raise
        row["config_status"] = "failed"
        return
    except (TimeoutError, OSError):
        row["config_status"] = "failed"
        return
    if not isinstance(result, dict):
        row["config_status"] = "failed"
        return
    if not result["found"]:
        row.update(config_available=False, config_status="not_in_storage")
        return
    config = result.get("config")
    if not isinstance(config, dict):
        row["config_status"] = "failed"
        return
    try:
        match_in_config = bool(
            query and query in json.dumps(config, ensure_ascii=False).casefold()
        )
    except (TypeError, ValueError) as exc:
        row["config_status"] = "failed"
        logger.debug(
            "Scene config discovery could not serialize %s: %r",
            row["entity_id"],
            exc,
        )
        return
    row.update(
        scene_id=result.get("storage_key")
        or config.get("id")
        or resolution["storage_key"],
        config_available=True,
        config_status="available",
        match_in_config=match_in_config,
    )


async def _scan_configs(
    backend: Backend,
    context: CallContext,
    rows: list[dict[str, Any]],
    query: str,
    *,
    timeout: float = CONFIG_SCAN_TIMEOUT,
    concurrency: int = CONFIG_SCAN_CONCURRENCY,
) -> dict[str, int]:
    """Bound concurrent config reads and cancel outstanding work at the deadline."""
    candidates = iter(
        row for row in rows if row["config_status"] != "integration_managed"
    )

    async def worker() -> None:
        for row in candidates:
            await _read_config(backend, context, row, query)

    tasks = [asyncio.create_task(worker()) for _ in range(concurrency)]
    try:
        async with asyncio.timeout(timeout):
            await asyncio.gather(*tasks)
    except TimeoutError:
        logger.debug(
            "Scene config scan reached its deadline; returning partial results"
        )
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return {
        "checked": sum(row["config_status"] == "available" for row in rows),
        "failed": sum(row["config_status"] == "failed" for row in rows),
        "not_scanned": sum(row["config_status"] == "not_checked" for row in rows),
        "not_in_storage": sum(row["config_status"] == "not_in_storage" for row in rows),
        "integration_managed": sum(
            row["config_status"] == "integration_managed" for row in rows
        ),
    }


async def discover_scenes(
    backend: Backend,
    context: CallContext,
    *,
    query: str | None,
    search_in_config: bool,
    limit: int,
    offset: int,
    scan_timeout: float = CONFIG_SCAN_TIMEOUT,
    scan_concurrency: int = CONFIG_SCAN_CONCURRENCY,
) -> dict[str, Any]:
    """List compact scene metadata, optionally matching full storage attribute values."""
    rows = await backend.execute("scenes.inventory", {}, context)
    query_text = (query or "").strip().casefold()
    content_search = bool(query_text and search_in_config)
    for row in rows:
        row["match_in_name"] = _matches_identity(row, query_text)
        row["match_in_config"] = False
    if not content_search:
        rows = [row for row in rows if row["match_in_name"]]
    scan = await _scan_configs(
        backend,
        context,
        rows if content_search else rows[offset : offset + limit],
        query_text if content_search else "",
        timeout=scan_timeout,
        concurrency=scan_concurrency,
    )
    if content_search:
        rows = [row for row in rows if row["match_in_name"] or row["match_in_config"]]
    page = rows[offset : offset + limit]
    has_more = offset + len(page) < len(rows)
    result: dict[str, Any] = {
        "success": True,
        "action": "list",
        "scenes": [
            {key: value for key, value in row.items() if not key.startswith("_")}
            for row in page
        ],
        "count": len(page),
        "total": len(rows),
        "total_is_exact": not (
            content_search
            and (scan["failed"] or scan["not_scanned"] or scan["not_in_storage"])
        ),
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
        "next_offset": offset + len(page) if has_more else None,
        "config_scan": scan,
    }
    if content_search:
        result["config_search_scope"] = (
            "Home Assistant editable storage scenes; integration-managed and raw YAML bodies are unavailable"
        )
    if (
        scan["failed"]
        or scan["not_scanned"]
        or (content_search and scan["not_in_storage"])
    ):
        result["partial"] = True
        result["partial_reason"] = (
            "Some scene configs could not be read within the bounded scan; config metadata and content matches are not exhaustive."
        )
    return result
