"""Remote compatibility functions for the shared radio enrichment logic."""

from typing import Any

from ha_mcp.backends.remote import RemoteBackend
from ha_mcp.runtime import radio
from ha_mcp.tool_api import CallContext


async def enrich_zha_metrics(
    client: Any, device_info: dict[str, Any], warnings: list[str]
) -> None:
    await radio.enrich_zha_metrics(
        RemoteBackend(client), CallContext(None), device_info, warnings
    )


async def enrich_zwave_status(
    client: Any, device_id: str, device_info: dict[str, Any], warnings: list[str]
) -> None:
    await radio.enrich_zwave_status(
        RemoteBackend(client), CallContext(None), device_id, device_info, warnings
    )


async def enrich_matter_diagnostics(
    client: Any, device_id: str, device_info: dict[str, Any], warnings: list[str]
) -> None:
    await radio.enrich_matter_diagnostics(
        RemoteBackend(client), CallContext(None), device_id, device_info, warnings
    )
