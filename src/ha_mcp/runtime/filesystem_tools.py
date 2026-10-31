"""Single public filesystem contract, bound to the owning service."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field


class FilesystemTools:
    def __init__(self, filesystem: Any) -> None:
        self.filesystem = filesystem

    async def ha_list_files(
        self,
        path: Annotated[
            str,
            Field(
                description=(
                    "Directory path. Relative to the config dir for the built-in "
                    "allowlist; absolute for a configured custom directory or "
                    "HAOS sibling volume. "
                    "Example: 'www/' or '/share/llm'"
                ),
            ),
        ],
        pattern: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Optional glob pattern to filter files. "
                    "Example: '*.css', '*.yaml', '*.js'"
                ),
            ),
        ] = None,
    ) -> dict[str, Any]:
        """List files in a directory within the Home Assistant config directory.

        Returns file names, sizes, and modification times for one of the allowed
        directories:
        - `www/` - Web assets (CSS, JS, images for dashboards)
        - `themes/` - Theme files
        - `custom_templates/` - Jinja2 template files
        - `dashboards/` - YAML-mode dashboard files
        - `blueprints/` - Automation/script blueprint sources (read-only)
        - `packages/`, plus the folder bound by `homeassistant: packages:` when
          it has another name
        - Plus any custom directories OR HAOS sibling volumes (`/share`,
          `/media`, `/ssl`, `/backup`) configured in the ha-mcp settings UI
        Path traversal (../) is blocked.

        EXAMPLE: ha_list_files(path="www/", pattern="*.css")
        """
        result: dict[str, Any] = await self.filesystem.list_files(
            path=path,
            pattern=pattern,
        )
        return result

    async def ha_read_file(
        self,
        path: Annotated[
            str,
            Field(
                description=(
                    "File path. Relative to the config dir for the built-in "
                    "allowlist; absolute for a configured HAOS sibling volume. "
                    "Examples: "
                    "'configuration.yaml', 'www/custom.css', '/share/llm/notes.md'"
                ),
            ),
        ],
        tail_lines: Annotated[
            int | None,
            Field(
                default=None,
                ge=1,
                le=10000,
                description=(
                    "For log files, return only the last N lines. Default: None (return "
                    "full file, or last 1000 lines for logs)"
                ),
            ),
        ] = None,
        yaml_path: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Dotted YAML key path (e.g. 'alert2', 'mqtt.sensor'). When set, the "
                    "response also carries 'subtree': the round-trip text of just that "
                    "key's value (null when the key is absent); comments and HA tags "
                    "('!secret', '!include') survive as written and a '!secret' is never "
                    "resolved."
                ),
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Read a file from the Home Assistant config directory.

        General-purpose escape hatch — prefer a dedicated tool when one exists:
        ha_manage_blueprints(action="get") for a blueprint body,
        ha_config_get_yaml for a config key, ha_config_get_automation/script/scene
        for storage-mode items. Reach for ha_read_file only for raw on-disk text
        those tools don't expose.

        Some files have special handling:
        - `secrets.yaml`: Values are masked for security
        - `home-assistant.log` / `home-assistant.log.fault`: Prefer
          ha_get_logs(source='error_log') and
          ha_get_logs(source='fault_log') over reading these directly.

        **Allowed Read Paths:**
        - `configuration.yaml`, `automations.yaml`, `scripts.yaml`, `scenes.yaml`
        - `secrets.yaml` (values masked)
        - `*.yaml` at any depth under `packages/` and under the folder bound by
          `homeassistant: packages:`
        - `home-assistant.log`, `home-assistant.log.fault` (tail only)
        - `www/**`, `themes/**`, `custom_templates/**`, `dashboards/**`, `blueprints/**`
        - `custom_components/**/*.py` (read-only)
        - Plus any custom directories OR HAOS sibling volumes (`/share`,
          `/media`, `/ssl`, `/backup`) configured in the ha-mcp settings UI
        Path traversal (../) is blocked.

        EXAMPLE: ha_read_file(path="packages/alert2.yaml", yaml_path="alert2")
        """
        result: dict[str, Any] = await self.filesystem.read_file(
            path=path,
            tail_lines=tail_lines,
            yaml_path=yaml_path,
        )
        return result

    async def ha_write_file(
        self,
        path: Annotated[
            str,
            Field(
                description=(
                    "File path in an allowed write directory; pass the "
                    "absolute path for a HAOS sibling volume. "
                    "Example: 'www/custom.css', '/share/llm/out.txt'"
                ),
            ),
        ],
        content: Annotated[
            str,
            Field(
                description="The content to write to the file.",
            ),
        ],
        overwrite: Annotated[
            bool,
            Field(
                default=False,
                description=("Whether to overwrite if the file exists."),
            ),
        ] = False,
        create_dirs: Annotated[
            bool,
            Field(
                default=True,
                description=(
                    "Whether to create parent directories if they don't exist."
                ),
            ),
        ] = True,
    ) -> dict[str, Any]:
        """Write a file to allowed directories in the Home Assistant config.

        Creates or updates files (custom CSS/JS for dashboards, Jinja2 macro
        templates, theme YAML) in restricted directories only.

        **Allowed Write Directories:**
        - `www/` - Web assets for dashboards
        - `themes/` - Theme YAML files
        - `custom_templates/` - Jinja2 template files
        - `dashboards/` - YAML-mode dashboard files
        - Plus any custom directories OR HAOS sibling volumes (`/share`,
          `/media`, `/ssl`, `/backup`) configured in the ha-mcp settings UI
        Configuration files (configuration.yaml, etc.) cannot be written; path
        traversal (../) is blocked.

        Text content only. Overwriting a file that currently holds binary
        content still succeeds, but its prior bytes cannot be captured by
        auto-backup (only modifications/deletions of text files are
        snapshotted); the skip is logged, the write is not blocked.

        EXAMPLE: ha_write_file(path="custom_templates/formatters.jinja", content="{% macro shout(text) %}{{ text | upper }}{% endmacro %}")
        """
        result: dict[str, Any] = await self.filesystem.write_file(
            path=path,
            content=content,
            overwrite=overwrite,
            create_dirs=create_dirs,
        )
        return result

    async def ha_delete_file(
        self,
        path: Annotated[
            str,
            Field(
                description=(
                    "File path in an allowed delete directory; pass the "
                    "absolute path for a HAOS sibling volume. "
                    "Example: 'www/old-file.css'"
                ),
            ),
        ],
        confirm: Annotated[
            bool,
            Field(
                default=False,
                description=("Must be True to confirm deletion."),
            ),
        ] = False,
    ) -> dict[str, Any]:
        """Delete a file from allowed directories in the Home Assistant config.

        ha_manage_backup(scope="edits") can restore a text file from its most
        recent auto-backup snapshot; a binary file has no snapshot and its
        deletion is permanent. Allowed directories:
        `www/`, `themes/`, `custom_templates/`, `dashboards/`, plus any custom
        directories OR HAOS sibling volumes (`/share`, `/media`, `/ssl`,
        `/backup`) configured in the ha-mcp settings UI. Configuration files
        cannot be deleted; path traversal (../) is blocked.

        EXAMPLE: ha_delete_file(path="www/deprecated-style.css", confirm=True)
        """
        result: dict[str, Any] = await self.filesystem.delete_file(
            path=path,
            confirm=confirm,
        )
        return result
