"""The unique public backup contract, shared by native Core and remote compatibility."""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from pydantic import Field


class BackupFacade:
    def __init__(self, manage: Callable[..., Awaitable[dict[str, Any]]]) -> None:
        self.manage_backups = manage

    async def ha_manage_backup(
        self,
        scope: Annotated[
            Literal["snapshot", "edits"],
            Field(
                description="'snapshot' for full HA tarballs; 'edits' for per-entity auto-backups."
            ),
        ],
        action: Annotated[
            Literal["create", "restore", "list", "view", "diff", "delete"],
            Field(
                description="Operation to perform. Valid (scope, action) combinations are listed in the tool description."
            ),
        ],
        # snapshot scope params
        name: Annotated[
            str | None,
            Field(
                default=None,
                description="(snapshot.create) Tarball name. Auto-generated if not provided.",
            ),
        ] = None,
        backup_id: Annotated[
            str | None,
            Field(
                default=None,
                description="(snapshot.restore / snapshot.delete) Tarball ID (e.g. 'dd7550ed').",
            ),
        ] = None,
        restore_database: Annotated[
            bool,
            Field(
                default=False,
                description="(snapshot.restore) Include the database in the restore; otherwise "
                "config only.",
            ),
        ] = False,
        confirm: Annotated[
            bool,
            Field(
                default=False,
                description=("(snapshot.delete) Must be True to confirm deletion."),
            ),
        ] = False,
        # edits scope params
        domain: Annotated[
            str | None,
            Field(
                default=None,
                description="(edits.create / edits.list / edits.delete) Snapshot domain (e.g. 'automation', 'helper_timer'). "
                "'entity' stores state/attributes; 'entity_registry' stores registry settings. "
                "Required for edits.create; list/delete match the domain exactly.",
            ),
        ] = None,
        entity_id: Annotated[
            str | None,
            Field(
                default=None,
                description="(edits.create / edits.list / edits.delete) Snapshot target ID; required for edits.create. "
                "For entity_registry creation, pass an HA entity ID or registry UUID; create/list return the saved UUID. "
                "List/delete match the saved ID exactly, without resolving entity aliases.",
            ),
        ] = None,
        backup_name: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "(edits.view / edits.restore / edits.delete) Auto-backup filename "
                    "(format '<domain>.<entity_id>.<timestamp>[_NN].yaml'). Not a tarball ID."
                ),
            ),
        ] = None,
        older_than_days: Annotated[
            int | None,
            Field(
                default=None,
                ge=0,
                description="(edits.delete) Bulk-delete auto-backups older than this many days.",
            ),
        ] = None,
        limit: Annotated[
            int,
            Field(
                default=200,
                ge=1,
                le=10_000,
                description="(edits.list / snapshot.list) Maximum number of entries to return.",
            ),
        ] = 200,
        ctx: Any = None,
    ) -> dict[str, Any]:
        """Manage Home Assistant backups — both full HA snapshots AND per-edit auto-backups.

        **Pick the scope first**, then the action. Wrong scope routes through the wrong code path:

        | scope | action | What it does |
        |---|---|---|
        | `snapshot` | `create` | Create a full HA tarball (config + addons, no DB by default). Can take a while on a large instance; progress heartbeats are sent while waiting. |
        | `snapshot` | `list` | List full HA tarball snapshots (id, name, date, size). Read-only — use to discover a `backup_id` or confirm a backup landed. |
        | `snapshot` | `restore` | Restore a full HA tarball. **Restarts HA.** Last-resort recovery. |
        | `snapshot` | `delete` | Delete one full HA tarball by `backup_id` (`confirm=True` required). **Disabled by default** (`enable_snapshot_delete` setting) and layered with guards even when enabled — see below. |
        | `edits` | `create` | On-demand snapshot of one entity (`domain` + `entity_id` required). Use before the user manually edits in the HA UI. Same handler path the decorator takes on writes; bypasses the `enable_auto_backup` toggle. |
        | `edits` | `list` | List per-entity auto-backups (lightweight). Filter by `domain` and/or `entity_id`. |
        | `edits` | `view` | Read one auto-backup file by name; returns YAML and parsed `config`. |
        | `edits` | `diff` | Compare one auto-backup against the entity's current config. RFC 6902 JSON-Patch + add/remove/replace counts; bounded output. Read-only — fetches the live config, makes no changes. |
        | `edits` | `restore` | Re-apply one auto-backup. Existing flow helpers (template, group, utility_meter, …) and config subentries require a fresh safety snapshot; other domains follow auto-backup settings and may proceed without one. A deleted flow helper is recreated with a new config-entry ID and its saved entity IDs are restored if unoccupied; a deleted subentry is recreated with a new subentry ID. A deleted integration config entry cannot be recreated: its snapshot holds only metadata and the enabled state. **No HA restart.** |
        | `edits` | `delete` | Delete one auto-backup by `backup_name`, or bulk-delete by filter. |

        **When to use which scope:**
        - Use `scope="edits"` to undo a recent automation/script/scene/dashboard/helper edit by the agent. Lightweight, fast, no restart.
        - Use `scope="snapshot"` only for system-wide recovery (botched add-on update, mass config corruption, etc.).

        **`scope="snapshot"` backup-hint:**
        Run before operations that CANNOT be undone (e.g., deleting devices). If the current definition was fetched or can be fetched, this tool is usually not needed.

        **`(snapshot, delete)` is off by default and layered even when enabled:** a human must
        set `enable_snapshot_delete=true` (env var, web settings UI, or add-on Supervisor
        options) — an agent cannot turn this on itself. When enabled, a delete call is still
        refused if: the target is a scheduled/automatic backup; it's younger than
        `snapshot_delete_min_age_days` (default 7, 0 disables the floor); or it's the single
        newest snapshot remaining. These guarantee at least one recovery point always
        survives an agent's own mistakes.

        **Human-managed backup controls:** `enable_snapshot_actions=false` (ENABLE_SNAPSHOT_ACTIONS) blocks every snapshot action, including listing. `backup_read_only=true` (BACKUP_READ_ONLY) allows edits list/view/diff and snapshot list when snapshots are enabled; it blocks explicit create/restore/delete in both scopes. Automatic pre-edit capture still follows `enable_auto_backup`. A human must change these controls in the Backups tab, app configuration, or environment; developer tools cannot change them. Global and connection Read Only Mode still restrict writes.

        **`enable_auto_backup` and `scope="edits"`:** the automatic-on-write capture (every wrapped tool call) is gated by `enable_auto_backup=true` — if the listing is empty, check the toggle (web settings UI or `ENABLE_AUTO_BACKUP=true` env var). The explicit `(edits, create)` action bypasses the toggle since the request is explicit; `list` / `view` / `restore` / `delete` operate on whatever's already on disk regardless of the toggle's current state.

        **Flow-helper filters:** `edits.create` accepts a flow helper's entity ID and returns its stable config-entry ID as `entity_id`. Use that returned ID for `edits.list` and bulk `edits.delete`; those filters do not resolve entity aliases. After recreation, the restore result reports the replacement config-entry ID and `entity_id_mapping` separately.

        **Entity edit history:** new automatic captures from `ha_set_entity` and `ha_remove_entity` use `domain="entity_registry"` and the durable registry UUID as `entity_id`. They contain registry settings, including name, area, hidden state and options, and follow the same registration across entity-ID renames. An explicit registry capture accepts an HA entity ID (e.g. `light.example`) or registry UUID; reuse the saved UUID returned by `edits.create` / `edits.list` for exact list/delete filters. Historical `domain="entity"` snapshots remain readable and restore only temporary state and attributes, not registry settings or physical device state. Their old domain/entity-ID filters still select those old snapshots; they do not also select new registry history. List without filters to discover both types, then use the returned `backup_name` to view or restore one snapshot.

        **Examples:**
        - Snapshot before risky op: `ha_manage_backup(scope="snapshot", action="create", name="Before_Big_Change")`
        - List snapshots (to discover a backup_id or confirm one landed): `ha_manage_backup(scope="snapshot", action="list")`
        - Restore full snapshot: `ha_manage_backup(scope="snapshot", action="restore", backup_id="dd7550ed")`
        - Delete an old snapshot (requires `enable_snapshot_delete=true`): `ha_manage_backup(scope="snapshot", action="delete", backup_id="dd7550ed", confirm=True)`
        - On-demand entity snapshot before a manual UI edit: `ha_manage_backup(scope="edits", action="create", domain="helper_input_boolean", entity_id="welcome_home_active")`
        - List recent auto-backups for one automation: `ha_manage_backup(scope="edits", action="list", domain="automation", entity_id="welcome_home")`
        - View an auto-backup: `ha_manage_backup(scope="edits", action="view", backup_name="automation.welcome_home.20260521_153000.yaml")`
        - Diff an auto-backup vs current state: `ha_manage_backup(scope="edits", action="diff", backup_name="automation.welcome_home.20260521_153000.yaml")`
        - Restore an auto-backup: `ha_manage_backup(scope="edits", action="restore", backup_name="automation.welcome_home.20260521_153000.yaml")`
        - Delete one auto-backup: `ha_manage_backup(scope="edits", action="delete", backup_name="...")`
        - Bulk-delete old auto-backups: `ha_manage_backup(scope="edits", action="delete", older_than_days=30)`
        """
        return await self.manage_backups(
            scope=scope,
            action=action,
            name=name,
            backup_id=backup_id,
            restore_database=restore_database,
            confirm=confirm,
            domain=domain,
            entity_id=entity_id,
            backup_name=backup_name,
            older_than_days=older_than_days,
            limit=limit,
            ctx=ctx,
        )


def backup_description(hint: str) -> str:
    """Substitute the operator guidance without duplicating the tool document."""
    return (BackupFacade.ha_manage_backup.__doc__ or "").replace(
        "Run before operations that CANNOT be undone (e.g., deleting devices). If the current definition was fetched or can be fetched, this tool is usually not needed.",
        hint,
        1,
    )
