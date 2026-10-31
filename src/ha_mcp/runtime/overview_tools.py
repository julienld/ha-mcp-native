"""The single public overview contract, independent of transport and settings."""

from typing import Annotated, Any, Literal, Protocol

from pydantic import Field

from ha_mcp.tools.coercion import JSON_STRING_COERCION


class OverviewService(Protocol):
    async def get_overview(self, **arguments: Any) -> dict[str, Any]: ...


class OverviewTools:
    def __init__(self, overview: OverviewService) -> None:
        self.overview = overview

    async def ha_get_overview(
        self,
        detail_level: Annotated[
            Literal["minimal", "standard", "full"],
            Field(
                default="minimal",
                description=(
                    "'minimal': 10 entities/domain, top-5 states; 'standard': 200 "
                    "entities/page, top-10 states (use offset for more); 'full': 200 "
                    "entities/page + entity_id + state + full states."
                ),
            ),
        ] = "minimal",
        domains: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description=(
                    "Filter to specific domains (e.g. 'light,sensor' or "
                    "['light','sensor']). None = all domains."
                ),
            ),
        ] = None,
        limit: Annotated[
            int | None,
            Field(
                default=None,
                ge=1,
                description=(
                    "Max total entities across all domains (default: unlimited for minimal,"
                    " 200 for standard/full)."
                ),
            ),
        ] = None,
        offset: Annotated[
            int,
            Field(
                default=0,
                ge=0,
                description="Number of entities to skip for pagination",
            ),
        ] = 0,
        max_entities_per_domain: Annotated[
            int | None,
            Field(
                default=None,
                description="Override default entity cap per domain (minimal=10, standard/full=unlimited). 0 = no limit on entities or states.",
            ),
        ] = None,
        include_state: Annotated[
            bool | None,
            Field(
                default=None,
                description="Include state field for entities (None = auto based on level). Full defaults to True.",
            ),
        ] = None,
        include_entity_id: Annotated[
            bool | None,
            Field(
                default=None,
                description="Include entity_id field for entities (None = auto based on level). Full defaults to True.",
            ),
        ] = None,
        include_notifications: Annotated[
            bool | None,
            Field(
                default=True,
                description="Include active persistent notifications.",
            ),
        ] = True,
        include_dismissed_repairs: Annotated[
            bool | None,
            Field(
                default=False,
                description=(
                    "Include user-dismissed/ignored repairs. Dismiss or restore "
                    "one with ha_manage_updates(action='ignore_repair' / "
                    "'unignore_repair')."
                ),
            ),
        ] = False,
        fields: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description=(
                    "Return only the specified top-level response keys to reduce response "
                    'size (e.g. ["system_info", "domain_stats"]). None = full response. '
                    "Available keys: success, system_summary, domain_stats, area_analysis, "
                    "ai_insights, pagination, partial, warnings, device_types, "
                    "service_availability, system_info, notification_count, notifications, "
                    "repair_count, dismissed_repair_count, repairs, repairs_error, "
                    "tool_discovery, settings_url, settings_url_hint, read_only_mode, "
                    "read_only_mode_hint, ha_mcp_update. Note: ``settings_url`` (stdio "
                    "mode), ``settings_url_hint`` (standalone HTTP/Docker mode), the "
                    "``read_only_mode`` / ``read_only_mode_hint`` pair (only while Read "
                    "Only Mode is on), and ``ha_mcp_update`` (when an update check applies)"
                    " are emitted regardless of ``fields=`` projection."
                ),
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Get AI-friendly system overview with intelligent categorization.

        Returns comprehensive system information at the requested detail level,
        including Home Assistant base_url, version, location, timezone, entity overview,
        and active persistent notifications (if any).
        Use 'minimal' (default) for most queries. Domain counts and states_summary
        are always complete regardless of entity pagination.

        Requests whose fields= are composed only of system_info, notification,
        repair, or server metadata fields skip the unrelated state, service, and
        registry reads.

        Do not use this tool to inspect a known entity or a narrow set of entities.
        Use ha_get_state for one entity, ha_get_entity for registry metadata, or
        ha_search with a domain or area filter. An unprojected overview collects
        system-wide state, service, and registry data and can be expensive on large
        Home Assistant installations.

        When the ha-mcp settings-UI sidecar is running (stdio mode, e.g. Claude
        Desktop / Claude Code) the response carries ``settings_url``, the local
        URL of the tool-configuration page; in standalone HTTP / Docker modes
        with an HTTP settings prefix it instead carries ``settings_url_hint``,
        saying where the page is mounted and how to construct the full URL. Hand
        whichever is present to the user when they ask how to enable or disable
        tools or change server settings.

        The response also carries ``ha_mcp_update`` ``{current, latest,
        update_available}`` (PyPI for pip/Docker, the Supervisor store for the
        app) — proactively tell the user when ``update_available`` is true.
        Omitted for the ``unknown`` version, when ``HA_MCP_DISABLE_UPDATE_CHECK``
        is set, or when the update check itself failed.
        """
        return await self.overview.get_overview(
            detail_level=detail_level,
            domains=domains,
            limit=limit,
            offset=offset,
            max_entities_per_domain=max_entities_per_domain,
            include_state=include_state,
            include_entity_id=include_entity_id,
            include_notifications=include_notifications,
            include_dismissed_repairs=include_dismissed_repairs,
            fields=fields,
        )
