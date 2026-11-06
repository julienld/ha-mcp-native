"""Pure input planning for update operations; Core still validates actual writes."""

from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response

from .errors import raise_failure

_INSTALL_ALL_CATEGORIES = ("addons", "hacs", "devices", "other")
_PROTECTED_UPDATE_CATEGORIES = ("core", "os", "supervisor")


def update_targets(
    action: str, entity_ids: list[str] | str | None, categories: list[str] | str | None
) -> tuple[list[str], list[str]]:
    ids = [entity_ids] if isinstance(entity_ids, str) else list(entity_ids or [])
    cats = [categories] if isinstance(categories, str) else list(categories or [])

    if action != "install" and (not ids or cats):
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"'{action}' requires entity_ids; categories is install-only.",
            )
        )
    if action == "install" and bool(ids) == bool(cats):
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "'install' requires exactly one of entity_ids or categories.",
            )
        )

    invalid = [e for e in ids if not e.startswith("update.")]
    if invalid:
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "entity_ids must be 'update.' entities.",
                context={"invalid_entity_ids": invalid},
            )
        )

    if not cats:
        return list(dict.fromkeys(ids)), []

    protected = sorted(set(cats) & set(_PROTECTED_UPDATE_CATEGORIES))
    unknown = sorted(
        set(cats) - set(_INSTALL_ALL_CATEGORIES) - set(_PROTECTED_UPDATE_CATEGORIES)
    )
    if protected or unknown:
        problems = []
        if protected:
            problems.append(
                f"categories {protected} are excluded from batch install by "
                "design (mirrors the HA Updates page, which never bundles "
                "core/OS/supervisor into 'Update all') — target those "
                "individually via entity_ids"
            )
        if unknown:
            problems.append(f"unknown categories {unknown}")
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "; ".join(problems),
                context={"allowed_categories": list(_INSTALL_ALL_CATEGORIES)},
            )
        )

    return [], list(dict.fromkeys(cats))


def single_update_id(entity_ids: list[str] | str | None) -> str:
    ids = [entity_ids] if isinstance(entity_ids, str) else list(entity_ids or [])
    if len(ids) != 1:
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "'get' requires exactly one entity in entity_ids.",
                context={"entity_ids": ids},
            )
        )
    if not ids[0].startswith("update."):
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Invalid entity_id format. Must start with 'update.'",
                context={"entity_id": ids[0]},
            )
        )
    return ids[0]


def repair_items(
    action: str, repairs: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    items = repairs or []
    if not items or not all(
        isinstance(row, dict)
        and isinstance(row.get("domain"), str)
        and isinstance(row.get("issue_id"), str)
        for row in items
    ):
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"'{action}' requires repairs: a list of "
                "{'domain': ..., 'issue_id': ...} objects.",
                suggestions=[
                    "Read domain and issue_id from ha_get_overview's repairs "
                    "(include_dismissed_repairs=True lists ignored ones too)"
                ],
                context={"action": action},
            )
        )
    return [{"domain": row["domain"], "issue_id": row["issue_id"]} for row in items]
