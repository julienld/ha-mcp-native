"""Registry recovery uses the saved Core identity and a compared before-image."""

from typing import Any

from ha_mcp.tool_api import Backend, CallContext, ToolFailure, UnsupportedOperation

from .snapshot_payloads import InvalidBackupSnapshotError, _strip_readonly


def registry_target(
    domain: str, entity_id: str, config: Any
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    if not isinstance(config, dict):
        raise InvalidBackupSnapshotError("Registry snapshot must contain an object")
    if domain == "category":
        scope, category_id = config.get("scope"), config.get("category_id")
        if (
            not isinstance(scope, str)
            or not isinstance(category_id, str)
            or not category_id
            or entity_id != f"{scope}:{category_id}"
        ):
            raise InvalidBackupSnapshotError(
                "Category snapshot does not match its scope and identity"
            )
        return (
            "category",
            {"scope": scope, "category_id": category_id},
            {"scope": scope},
        )
    if domain == "area_or_floor":
        kind = config.get("kind")
        key = f"{kind}_id"
        identifier = config.get(key)
        if (
            kind not in {"area", "floor"}
            or not isinstance(identifier, str)
            or not identifier
            or entity_id != f"{kind}:{identifier}"
        ):
            raise InvalidBackupSnapshotError(
                "Location snapshot does not match its kind and identity"
            )
        return kind, {key: identifier}, {"kind": kind}
    raise UnsupportedOperation(f"snapshots.{domain}.read")


async def current_registry(
    backend: Backend, actor: CallContext, domain: str, entity_id: str, config: Any
) -> dict[str, Any] | None:
    kind, target, metadata = registry_target(domain, entity_id, config)
    row = await backend.execute(f"registry.{kind}.read", target, actor)
    return {**metadata, **row} if row is not None else None


async def restore_registry(
    backend: Backend,
    actor: CallContext,
    domain: str,
    entity_id: str,
    config: Any,
    before: dict[str, Any] | None,
) -> Any:
    kind, target, metadata = registry_target(domain, entity_id, config)
    if before is None:
        raise ToolFailure(
            "RESOURCE_NOT_FOUND",
            "The saved registry entry no longer exists",
            status="not_started",
        )
    # An area's floor_id is writable affiliation, not the area's own identity.
    result = await backend.execute(
        f"registry.{kind}.update",
        {
            "data": {**_strip_readonly(config, *metadata), **target},
            "expected_item": {
                key: value for key, value in before.items() if key not in metadata
            },
        },
        actor,
    )
    if not result.get("success"):
        raise ToolFailure(
            "RESTORE_FAILED", "Core rejected registry restoration", status="not_started"
        )
    return result["result"]
