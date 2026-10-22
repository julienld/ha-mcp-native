"""Load Search's dashboard documents through the actual Lovelace owners."""

import asyncio
from typing import Any

from ha_mcp.runtime.search_component_results import _DashboardLeg
from ha_mcp.runtime.search_response import _ResolvedSearch
from ha_mcp.tool_api import ToolFailure

from .dashboard_storage import DashboardStorage
from .registry_access import authorize_actor
from .search_sources import search_setting


async def load_dashboard_documents(
    manager: Any, actor: Any, req: _ResolvedSearch, check: Any
) -> _DashboardLeg:
    await authorize_actor(actor, manager.backend.authorize)
    storage = DashboardStorage(manager.hass, actor, authorize=manager.backend.authorize)
    try:
        rows = storage.metadata()
    except Exception:  # noqa: BLE001 - the public search preserves other surfaces
        return _DashboardLeg([], failed=1, error="Core dashboard inventory unavailable")
    paths = {str(row["url_path"]): row for row in rows}
    paths.setdefault("lovelace", {"url_path": "lovelace", "title": "Default"})
    documents = []
    failures = 0
    for path, row in paths.items():
        try:
            async with asyncio.timeout(
                float(search_setting(manager, "individual_config_timeout"))
            ):
                image = await storage.read(path, force=True)
            if image.owner is None:
                failures += 1
            elif image.config is not None:
                documents.append(
                    {
                        "url_path": image.url_path,
                        "title": row.get("title") or image.url_path,
                        "mode": image.owner.mode,
                        "config": image.config,
                    }
                )
        except ToolFailure as error:
            if error.code == "AUTH_FORBIDDEN":
                raise
            failures += 1
        except Exception:  # noqa: BLE001 - individual dashboard read failures are disclosed
            failures += 1
        await check()
    return _DashboardLeg(documents, failed=failures)
