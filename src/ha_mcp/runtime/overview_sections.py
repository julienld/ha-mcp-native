"""Pure presentation of overview config, notifications and repairs."""

from typing import Any

from .health_results import filter_active_repairs, project_repair_fields


def apply_overview_system_info(
    result: dict[str, Any],
    config: dict[str, Any],
    base_url: str | None,
    detail_level: str,
) -> None:
    system_info: dict[str, Any] = {
        "base_url": base_url,
        "version": config.get("version"),
        "location_name": config.get("location_name"),
        "time_zone": config.get("time_zone"),
        "language": config.get("language"),
        "state": config.get("state"),
    }
    if detail_level == "full":
        system_info.update(
            {
                "country": config.get("country"),
                "currency": config.get("currency"),
                "unit_system": config.get("unit_system", {}),
                "latitude": config.get("latitude"),
                "longitude": config.get("longitude"),
                "elevation": config.get("elevation"),
                "components_loaded": len(config.get("components", [])),
                "safe_mode": config.get("safe_mode", False),
                "internal_url": config.get("internal_url"),
                "external_url": config.get("external_url"),
                # No default: distinguish HA-not-exposing-the-key (None)
                # from empty-allowlist ([]) — security-relevant for agents.
                "allowlist_external_dirs": config.get("allowlist_external_dirs"),
            }
        )
    result["system_info"] = system_info
    if "system_summary" in result:
        result["system_summary"]["version"] = config.get("version") or "unknown"


def apply_overview_notifications(
    result: dict[str, Any], notifications: list[dict[str, Any]]
) -> None:
    result["notification_count"] = len(notifications)
    result["notifications"] = [
        {
            "notification_id": n.get("notification_id"),
            "title": n.get("title"),
            "message": n.get("message"),
            "created_at": n.get("created_at"),
        }
        for n in notifications
    ]


def apply_overview_repairs(
    result: dict[str, Any],
    raw_issues: list[dict[str, Any]],
    include_dismissed_repairs_bool: bool,
) -> None:
    all_issues = [i for i in raw_issues if i.get("active") is not False]
    visible_issues = filter_active_repairs(
        all_issues,
        include_dismissed=include_dismissed_repairs_bool,
    )
    result["repair_count"] = len(visible_issues)
    if not include_dismissed_repairs_bool:
        # Baseline excludes inactive registry stubs so they are
        # not miscounted as dismissed.
        dismissed_count = len(
            filter_active_repairs(all_issues, include_dismissed=True)
        ) - len(visible_issues)
        if dismissed_count:
            result["dismissed_repair_count"] = dismissed_count
    result["repairs"] = [project_repair_fields(r) for r in visible_issues]
