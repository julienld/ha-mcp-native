"""The one public update/repair contract, bound to its native or legacy service."""

from typing import Annotated, Any, Protocol

from pydantic import Field

from ha_mcp.tools.coercion import JSON_STRING_COERCION


class UpdateService(Protocol):
    async def manage(
        self,
        action: str = "list",
        entity_ids: list[str] | str | None = None,
        categories: list[str] | str | None = None,
        include_skipped: bool = False,
        include_release_notes: bool = False,
        backup: bool = False,
        repairs: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]: ...


class UpdateToolsFacade:
    def __init__(self, updates: UpdateService) -> None:
        self._updates = updates

    async def ha_manage_updates(
        self,
        action: Annotated[
            str,
            Field(
                description="'list' (all pending updates, default), 'get' "
                "(details/release notes for one update), 'install' (apply "
                "pending updates), 'skip' (hide the offered version), "
                "'clear_skipped' (re-offer a skipped version), 'ignore_repair' "
                "(dismiss Repairs issues, as the Repairs UI's Ignore does), or "
                "'unignore_repair' (show them again).",
                default="list",
            ),
        ] = "list",
        entity_ids: Annotated[
            list[str] | str | None,
            JSON_STRING_COERCION,
            Field(
                description="Update entity_id(s) to act on. 'get' takes exactly "
                "one; skip/clear_skipped require at least one; for install, "
                "mutually exclusive with categories.",
                default=None,
            ),
        ] = None,
        categories: Annotated[
            list[str] | str | None,
            JSON_STRING_COERCION,
            Field(
                description="For install: apply every pending update in these categories ('addons',"
                " 'hacs', 'devices', 'other'). As with the HA 'Update all' button, "
                "core/os/supervisor are excluded (target those individually via "
                "entity_ids) and skipped updates are never included.",
                default=None,
            ),
        ] = None,
        include_skipped: Annotated[
            bool,
            Field(
                description="For list: include updates that have been skipped.",
                default=False,
            ),
        ] = False,
        include_release_notes: Annotated[
            bool,
            Field(
                description="For get on a Core update entity: fetch multi-version release notes and"
                " breaking changes for all versions between installed and latest. Adds "
                "breaking_changes, multi_version_release_notes, and "
                "installed_integrations to the response.",
                default=False,
            ),
        ] = False,
        backup: Annotated[
            bool,
            Field(
                description="For install: create a backup before installing where the update entity"
                " supports it (apps (add-ons)).",
                default=False,
            ),
        ] = False,
        repairs: Annotated[
            list[dict[str, Any]] | None,
            JSON_STRING_COERCION,
            Field(
                description="For ignore_repair / unignore_repair: the Repairs "
                "issues to act on, as [{'domain': ..., 'issue_id': ...}] taken "
                "from ha_get_overview's repairs.",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Manage Home Assistant updates (list, details, install, skip) and Repairs issues (ignore).

        Covers Core, OS, supervisor, apps (add-ons), device firmware, and HACS
        update entities, plus the Repairs issues in Settings > System > Repairs.
        Repairs are listed by ha_get_overview (not here); pass their domain and
        issue_id to 'ignore_repair' / 'unignore_repair'. In Read Only Mode the
        read actions ('list', 'get') stay available; write actions are blocked.

        'install' blocks until each update finishes (minutes). Any unknown
        service outcome stops the batch, preserving earlier results and the
        current item's diagnostic; later targets are marked not_attempted.
        An update may still be running, or Core restarting. Check the affected
        entity with action='get' before retrying. Finished updates drop out of
        'list'. When hosted in
        Home Assistant, Core/OS installs are scheduled before its process stops;
        that reply cannot confirm completion and later batch targets are not
        started. 'list' may include ha_mcp_update {current, latest,
        update_available} when the host has authoritative update information
        for this MCP server. The native HACS host does not substitute PyPI's
        sidecar release status for this component's own update status.

        EXAMPLES:
        - Pre-update analysis: ha_manage_updates(action="get", entity_ids=["update.home_assistant_core_update"], include_release_notes=True)
        - Update everything pending in a category: ha_manage_updates(action="install", categories=["addons", "hacs"])
        - Dismiss a repair: ha_manage_updates(action="ignore_repair", repairs=[{"domain": "sun", "issue_id": "abc"}])
        """

        return await self._updates.manage(
            action,
            entity_ids,
            categories,
            include_skipped,
            include_release_notes,
            backup,
            repairs,
        )
