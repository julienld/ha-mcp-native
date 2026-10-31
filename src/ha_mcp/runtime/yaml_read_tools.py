"""Single public yaml read contract, bound to the owning service."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field


class YamlReadTools:
    def __init__(self, yaml_reader: Any) -> None:
        self.yaml_reader = yaml_reader

    async def ha_config_get_yaml(
        self,
        yaml_path: Annotated[
            str,
            Field(
                description=(
                    "Dotted YAML key path to look up, e.g. 'alert2', 'mqtt', "
                    "'template'. Any key is readable — unlike ha_config_set_yaml, "
                    "which only writes an allowlisted set."
                ),
            ),
        ],
        file: Annotated[
            str,
            Field(
                default="configuration.yaml",
                description=(
                    "Config-relative file to read. Accepts an fnmatch glob to "
                    "search several files at once — 'packages/*.yaml' matches one "
                    "directory level, not a nested tree."
                ),
            ),
        ] = "configuration.yaml",
        include_content: Annotated[
            bool,
            Field(
                default=True,
                description=(
                    "Return the round-trip YAML text of each match. Set False to "
                    "discover only which files define the key."
                ),
            ),
        ] = True,
        include_parsed: Annotated[
            bool,
            Field(
                default=False,
                description=(
                    "Additionally return each match as structured data. HA tags "
                    "stay in source form ('!secret api_key'), never resolved."
                ),
            ),
        ] = False,
    ) -> dict[str, Any]:
        """Get the current YAML fragment under a key, from one config file or across a glob.

        Use before ha_config_set_yaml to inspect what a key currently holds, and
        to find which file defines it — when ha_config_set_yaml accepts this
        ``yaml_path`` for the returned ``file``, the two are exactly the
        arguments that address the same fragment for an edit. To change a value
        nested below such a key, read the whole key and replace it.

        Not for storage-mode items: automations, scripts and scenes created
        through the UI live outside YAML — read those with ha_config_get_automation,
        ha_config_get_script and ha_config_get_scene. This tool sees only what is
        written in the config files themselves.

        Reads the file, so it reflects what is on disk rather than what Home
        Assistant currently has loaded; a fragment edited but not yet reloaded
        differs from the running config. Comments and HA tags survive as written
        and a ``!secret`` is never resolved to its value, so ``content`` can be
        handed back to ha_config_set_yaml unchanged. Files outside the read
        allowlist stay unreadable, and secrets.yaml reads back with its values
        masked.
        """
        result: dict[str, Any] = await self.yaml_reader.get(
            yaml_path=yaml_path,
            file=file,
            include_content=include_content,
            include_parsed=include_parsed,
        )
        return result
