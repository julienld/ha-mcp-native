"""The unique public backup contract, shared by native Core and remote compatibility."""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from pydantic import Field

from ha_mcp.tools.coercion import JSON_STRING_COERCION


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
                description="(snapshot.create) Tarball name. Auto-generated if not provided. "
                "Home Assistant names a full backup made with its automatic-backup settings itself.",
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
            bool | None,
            Field(
                default=None,
                description="(snapshot.restore) Include the database in the restore. Default: "
                "restore it when the backup contains it. Must be false for a backup "
                "without one; on Supervisor installs it must match the backup.",
            ),
        ] = None,
        restore_addons: Annotated[
            list[str] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description="(snapshot.restore) Slugs of apps (add-ons) in the backup to "
                "restore, from its `addons` in the snapshot list. Default: none. "
                "Supervisor installs only.",
            ),
        ] = None,
        restore_folders: Annotated[
            list[str] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description="(snapshot.restore) Folders in the backup to restore, from its "
                "`folders` in the snapshot list (`share`, `addons/local`, `ssl`, `media`). "
                "Default: none. Supervisor installs only.",
            ),
        ] = None,
        include_homeassistant: Annotated[
            bool | None,
            Field(
                default=None,
                description="(snapshot.create) Custom backup: include Home Assistant's "
                "configuration.",
            ),
        ] = None,
        include_database: Annotated[
            bool | None,
            Field(
                default=None,
                description="(snapshot.create) Custom backup: include the database "
                "(history); needs include_homeassistant=True.",
            ),
        ] = None,
        include_addons: Annotated[
            list[str] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description="(snapshot.create) Custom backup: slugs of the installed apps "
                "(add-ons) to include. Supervisor installs only.",
            ),
        ] = None,
        include_all_addons: Annotated[
            bool | None,
            Field(
                default=None,
                description="(snapshot.create) Custom backup: include every app (add-on); "
                "not together with include_addons. Supervisor installs only.",
            ),
        ] = None,
        include_folders: Annotated[
            list[str] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description="(snapshot.create) Custom backup: folders to include "
                "(`share`, `addons/local`, `ssl`, `media`). Supervisor installs only.",
            ),
        ] = None,
        safety_backup: Annotated[
            bool,
            Field(
                default=False,
                description="(snapshot.restore) Back up what the restore replaces "
                "(configuration, plus the database, apps and folders it restores) first, "
                "as a manual backup on the default location. Off by default; offer it "
                "to the user, who decides. Needs the default backup password.",
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
                "'entity' captures registry settings under the HA entity ID; historical state/attributes files remain restorable. "
                "'entity_registry' explicitly uses the registry UUID. "
                "Required for edits.create; list/delete match the domain exactly.",
            ),
        ] = None,
        entity_id: Annotated[
            str | None,
            Field(
                default=None,
                description="(edits.create / edits.list / edits.delete) Snapshot target ID; required for edits.create. "
                "For entity creation, pass an HA entity ID. For entity_registry creation, pass an HA entity ID or registry UUID; create/list return the saved UUID. "
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

        **Entity edit history:** automatic captures from `ha_set_entity` and `ha_remove_entity`, and explicit `domain="entity"` captures, use the HA entity ID (e.g. `light.example`) for exact list/delete filters. Their registry image retains the durable UUID, so diff/restore follow the same registration across renames. Historical `entity` files are classified before restoring: state/attributes images restore only temporary state, while registry images restore user settings and options. Neither restores physical device state or recreates a deleted registration. Explicit `entity_registry` captures and files from the earlier Native format use the registry UUID as their filter key. Filters match stored domain/target exactly; they never expand deletion across aliases or formats. List without filters to discover all history and select one `backup_name`.

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
            restore_addons=restore_addons,
            restore_folders=restore_folders,
            safety_backup=safety_backup,
            parts={
                "include_homeassistant": include_homeassistant,
                "include_database": include_database,
                "include_addons": include_addons,
                "include_all_addons": include_all_addons,
                "include_folders": include_folders,
            },
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
