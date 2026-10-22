"""Fixed filesystem probes for the real-Core File/YAML contract bench.

Root hooks: GET file_probe -> file_probe(hass); POST file_external_edit ->
await external_yaml_edit(hass). Neither accepts an arbitrary filesystem path.
"""

from pathlib import Path
from typing import Any


def file_probe(hass: Any) -> dict[str, Any]:
    owner = hass.data.get("ha_mcp_tools", {}).get("file_access_owner")
    entry = getattr(owner, "entry", None)
    return {
        "ready": bool(
            owner is not None
            and owner.active
            and entry is not None
            and entry.state.value == "loaded"
        ),
        "owner": type(owner).__name__ if owner is not None else None,
        "entry_id": entry.entry_id if entry is not None else None,
        "pending": len(owner.pending) if owner is not None else 0,
    }


async def external_yaml_edit(hass: Any) -> dict[str, Any]:
    target = Path(hass.config.path("packages", "native_contract.yaml"))
    content = "# external editor\nutility_meter:\n  external:\n    source: sensor.native_external\n"
    await hass.async_add_executor_job(target.write_text, content)
    return {"written": True}
