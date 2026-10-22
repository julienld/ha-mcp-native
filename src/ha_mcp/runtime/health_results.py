"""Pure health-section projections shared with the compatibility server."""

from typing import Any


class HealthResults:
    @staticmethod
    def _warn_unknown_includes(result: dict[str, Any], includes: set[str]) -> None:
        """Append a warning to ``result`` for any unrecognized include sections.

        Extracted verbatim from ``ha_get_system_health``.
        """
        VALID_INCLUDES = {
            "repairs",
            "zha_network",
            "zha_network_full",
            "zwave_network",
            "thread_network",
            "matter_network",
            "diagnostics",
            "config_check",
            "themes",
            "dead_entities",
        }
        unknown = includes - VALID_INCLUDES
        if unknown:
            result.setdefault("warnings", []).append(
                f"Unknown include sections ignored: {', '.join(sorted(unknown))}"
            )

    @staticmethod
    def _parse_includes(include: str | None) -> set[str]:
        """Parse the comma-separated include parameter into a set of section names."""
        if not include:
            return set()
        return {s.strip().lower() for s in include.split(",") if s.strip()}

    @staticmethod
    def _classify_registry_entries(
        registry: list[dict[str, Any]],
        state_by_id: dict[str, Any],
        live_entry_ids: set[str] | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Classify registry entries into ``(orphans, stale_restored)`` buckets.

        Extracted verbatim from ``_fetch_dead_entities``' classification loop.
        """
        orphans: list[dict[str, Any]] = []
        stale: list[dict[str, Any]] = []
        for entry in registry:
            if not isinstance(entry, dict):
                continue
            eid = entry.get("entity_id")
            if not eid:
                continue
            cfg = entry.get("config_entry_id")
            disabled_by = entry.get("disabled_by")

            # Tier 1 — config-entry orphan (only when the live set loaded).
            # Covers disabled leftovers too: a disabled entity whose config
            # entry is gone is still dead cruft (disabled_by is surfaced on
            # the item so the client sees why it lingered).
            if live_entry_ids is not None and cfg and cfg not in live_entry_ids:
                orphans.append(
                    {
                        "entity_id": eid,
                        "platform": entry.get("platform"),
                        "config_entry_id": cfg,
                        "disabled_by": disabled_by,
                        "has_state": eid in state_by_id,
                    }
                )
                continue

            # Tier 2 — stale restored. Skip intentionally-disabled entries
            # (they normally have no state object anyway).
            if disabled_by is not None:
                continue
            state_obj = state_by_id.get(eid)
            if state_obj is None:
                continue
            attrs = state_obj.get("attributes")
            if (
                state_obj.get("state") == "unavailable"
                and isinstance(attrs, dict)
                and attrs.get("restored")
            ):
                stale.append(
                    {
                        "entity_id": eid,
                        "platform": entry.get("platform"),
                        "config_entry_id": cfg,
                    }
                )
        return orphans, stale


def summarize_theme_listing(raw_themes: dict[str, Any]) -> dict[str, Any]:
    """Summarize a ``frontend/get_themes`` result into names plus defaults.

    Returns theme NAMES, not the full per-theme CSS variable dicts (installed
    community themes can carry hundreds of variables; listings are a
    discovery/verify surface, not a content dump).
    """
    themes_value = raw_themes.get("themes") or {}
    theme_names = sorted(themes_value.keys() if isinstance(themes_value, dict) else [])
    return {
        "themes": theme_names,
        "count": len(theme_names),
        "default_theme": raw_themes.get("default_theme"),
        "default_dark_theme": raw_themes.get("default_dark_theme"),
    }


def filter_active_repairs(
    issues: list[dict[str, Any]], *, include_dismissed: bool = False
) -> list[dict[str, Any]]:
    """Drop user-dismissed repairs unless ``include_dismissed`` is set.

    HA's `repairs/list_issues` returns both active and ignored repairs (the
    Repairs UI hides ignored ones by default). Mirror that UI default so
    overview / system-health responses don't surface repairs the user has
    already dismissed.

    Entries with ``active=False`` are always dropped, regardless of
    ``include_dismissed``: HA core's ``ws_list_issues`` never emits them
    (on restart the registry reloads each stored non-persistent issue as an
    inactive stub until it is re-raised or deleted), but the component's
    raw registry dump does.
    """
    if include_dismissed:
        return [r for r in issues if r.get("active") is not False]
    return [r for r in issues if r.get("active") is not False and not r.get("ignored")]


def repair_section(
    raw_issues: list[dict[str, Any]], include_dismissed: bool
) -> dict[str, Any]:
    all_issues = [i for i in raw_issues if i.get("active") is not False]
    visible_issues = filter_active_repairs(
        all_issues, include_dismissed=include_dismissed
    )
    repairs = {
        "issues": visible_issues,
        "count": len(visible_issues),
    }
    if not include_dismissed:
        # Baseline excludes inactive registry stubs so they are
        # not miscounted as dismissed.
        dismissed_count = len(
            filter_active_repairs(all_issues, include_dismissed=True)
        ) - len(visible_issues)
        if dismissed_count:
            repairs["dismissed_count"] = dismissed_count

    return repairs


def zha_network_result(raw_devices: list[dict[str, Any]], full: bool) -> dict[str, Any]:
    ZHA_SUMMARY_LIMIT = 50
    ZHA_FULL_LIMIT = 25
    total = len(raw_devices)
    device_limit = ZHA_FULL_LIMIT if full else ZHA_SUMMARY_LIMIT
    truncated = total > device_limit
    capped_devices = raw_devices[:device_limit]
    if full:
        zha_devices = capped_devices
    else:
        zha_devices = [
            {
                "ieee": d.get("ieee"),
                "name": d.get("user_given_name") or d.get("name"),
                "manufacturer": d.get("manufacturer"),
                "model": d.get("model"),
                "lqi": d.get("lqi"),
                "rssi": d.get("rssi"),
                "available": d.get("available"),
            }
            for d in capped_devices
        ]
    zha_network = {
        "devices": zha_devices,
        "count": len(zha_devices),
        "total_count": total,
    }
    if truncated:
        zha_network["truncated"] = True
        zha_network["hint"] = (
            f"Showing {device_limit} of {total} devices. "
            "Use ha_get_device(integration='zha') for full device list."
        )

    return zha_network


def zwave_network_result(net_data: dict[str, Any]) -> dict[str, Any]:
    ZWAVE_NODE_LIMIT = 50
    zwave_network: dict[str, Any] = {}
    zwave_network["controller"] = net_data.get("controller", {})
    nodes = net_data.get("controller", {}).get("nodes", [])
    total_nodes = len(nodes)
    capped_nodes = nodes[:ZWAVE_NODE_LIMIT]
    node_summaries = [
        {
            "node_id": n.get("node_id"),
            "status": n.get("status"),
            "is_routing": n.get("is_routing"),
            "is_secure": n.get("is_secure"),
            "zwave_plus_version": n.get("zwave_plus_version"),
            "is_controller_node": n.get("is_controller_node"),
        }
        for n in capped_nodes
    ]
    zwave_network["nodes"] = node_summaries
    zwave_network["count"] = len(node_summaries)
    zwave_network["total_count"] = total_nodes
    if total_nodes > ZWAVE_NODE_LIMIT:
        zwave_network["truncated"] = True
        zwave_network["hint"] = (
            f"Showing {ZWAVE_NODE_LIMIT} of {total_nodes} nodes. "
            "Use ha_get_device(integration='zwave_js') for full device list."
        )

    return zwave_network


def thread_network_result(routers_raw: dict[str, Any]) -> dict[str, Any]:
    border_routers = [
        {
            "extended_address": ext_addr,
            "channel": (info or {}).get("channel"),
            "extended_pan_id": (info or {}).get("extended_pan_id"),
            "border_agent_id": (info or {}).get("border_agent_id"),
        }
        for ext_addr, info in routers_raw.items()
    ]
    thread_network = {
        "border_routers": border_routers,
        "count": len(border_routers),
    }

    return thread_network


def dead_entities_result(
    state_by_id: dict[str, Any],
    registry: list[dict[str, Any]],
    live_entry_ids: set[str] | None,
) -> dict[str, Any]:
    DEAD_ENTITIES_LIMIT = 50
    dead: dict[str, Any] = {}
    orphans, stale = HealthResults._classify_registry_entries(
        registry, state_by_id, live_entry_ids
    )

    def _bucket(items: list[dict[str, Any]]) -> dict[str, Any]:
        total = len(items)
        capped = items[:DEAD_ENTITIES_LIMIT]
        bucket: dict[str, Any] = {
            "items": capped,
            "count": len(capped),
            "total_count": total,
        }
        if total > DEAD_ENTITIES_LIMIT:
            bucket["truncated"] = True
            bucket["hint"] = (
                f"Showing {DEAD_ENTITIES_LIMIT} of {total}; "
                "remove cleanup candidates in batches and re-run."
            )
        return bucket

    candidate_total = len(orphans) + len(stale)
    dead["config_entry_orphans"] = _bucket(orphans)
    dead["stale_restored"] = _bucket(stale)
    dead["summary"] = {
        "candidate_total": candidate_total,
        "registry_total": len(registry),
    }
    # Only attach the guidance note when there is something to act on —
    # no point spending tokens on cleanup advice for an empty result.
    if candidate_total:
        dead["note"] = (
            "Excludes 'unknown'-state entities and merely-offline "
            "devices (bare 'unavailable' without 'restored'). Entries "
            "can appear under stale_restored transiently right after a "
            "restart; re-run if HA restarted recently. Remove a "
            "confirmed-dead entity with ha_remove_entity(entity_id)."
        )

    return dead


_REPAIR_PROJECTION_FIELDS = (
    "issue_id",
    "domain",
    "severity",
    "translation_key",
    "ignored",
    "dismissed_version",
    "is_fixable",
    "breaks_in_ha_version",
    "created",
    "issue_domain",
)


def project_repair_fields(issue: dict[str, Any]) -> dict[str, Any]:
    """Project a repair issue dict to the public-facing field subset.

    Drops verbose fields (`translation_placeholders`, `learn_more_url`) to
    keep overview payloads compact.
    """
    return {k: issue[k] for k in _REPAIR_PROJECTION_FIELDS if k in issue}
