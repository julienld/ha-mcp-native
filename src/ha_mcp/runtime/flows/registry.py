"""Registry and skill seams for the shared helper-flow orchestration."""

from typing import Any, cast

from ..helper_schema import _HELPER_SKILL_FILES
from ..registry_references import validate_registry_ids as validate_backend_registry_ids
from ..skill_content import apply_skill_content
from .session import FlowSession


async def validate_registry_ids(
    session: FlowSession,
    area_id: str | None,
    labels: list[str] | None,
    categories: dict[str, str | None],
    *,
    fail_closed: bool = True,
) -> None:
    await validate_backend_registry_ids(
        session.backend,
        session.context,
        area_id,
        labels,
        categories,
        fail_closed=fail_closed,
    )


async def _get_entities_for_config_entry(
    session: FlowSession, entry_id: str, warnings: list[str]
) -> list[dict[str, Any]]:
    result = await session.backend.execute(
        "helpers.entities.for_entry", {"config_entry_id": entry_id}, session.context
    )
    warnings.extend(result.get("warnings", []))
    return cast(list[dict[str, Any]], result["entities"])


async def _apply_registry_updates_to_entity(
    session: FlowSession,
    entity_id: str,
    area_id: str | None,
    labels: list[str] | None,
    category: str | None,
    icon: str | None,
    warnings: list[str],
) -> dict[str, Any]:
    registry = {
        key: value
        for key, value in (
            ("area_id", area_id),
            ("labels", labels),
            ("category", category),
            ("icon", icon),
        )
        if value is not None
    }
    result = await session.backend.execute(
        "helpers.entities.update",
        {"entity_ids": [entity_id], "registry": registry},
        session.context,
    )
    warnings.extend(result.get("warnings", []))
    return cast(dict[str, Any], result["applied"][0])


async def _attach_helper_skill(
    session: FlowSession, response: dict[str, Any], mandatory: bool
) -> None:
    resolved = await session.backend.execute(
        "skills.files",
        {"references": list(_HELPER_SKILL_FILES) if mandatory else []},
        session.context,
    )
    apply_skill_content(
        response,
        resolved["content"],
        enabled=resolved["enabled"],
        requested=mandatory,
        vendor_missing=resolved["vendor_missing"],
    )
