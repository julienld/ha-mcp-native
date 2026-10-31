"""Prevent automation writes from changing another stored automation's ID."""

from typing import Any

from ha_mcp.errors import HomeAssistantAPIError


def automation_write_body(
    config: dict[str, Any], identifier: str | None, unique_id: str
) -> dict[str, Any]:
    # Reject mismatch between resolved storage key and inner ``config["id"]``.
    # HA stores by the inner id field even though the URL carries one too —
    # a divergence silently overwrites the automation whose unique_id matches
    # the inner id, while reporting success for the URL target (#1404).
    config_id = config.get("id")
    if config_id is not None and str(config_id) != str(unique_id):
        if identifier is None:
            raise HomeAssistantAPIError(
                "Cannot create automation with explicit config['id']="
                f"{config_id!r}: Home Assistant stores by the inner id, "
                "which would silently overwrite an existing automation. "
                "Omit 'id' from config to auto-generate, or pass identifier "
                "to update an existing automation.",
                status_code=400,
            )
        raise HomeAssistantAPIError(
            f"Mismatched automation id: identifier={identifier!r} resolves "
            f"to unique_id={unique_id!r}, but config['id']={config_id!r}. "
            "Refusing to write to prevent overwriting the wrong automation. "
            f"Remove 'id' from config or set it to the resolved unique_id "
            f"({unique_id!r}).",
            status_code=400,
        )

    # Add unique_id to config for updates
    if unique_id and "id" not in config:
        config = {**config, "id": unique_id}

    return config
