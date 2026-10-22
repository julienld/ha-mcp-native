"""Single public yaml contract, bound to the owning service."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from ha_mcp.tool_api.parameters import BestPracticeKeyParam


class YamlConfigTools:
    def __init__(self, yaml_editor: Any) -> None:
        self.yaml_editor = yaml_editor

    async def ha_config_set_yaml(
        self,
        yaml_path: Annotated[
            str,
            Field(
                description=(
                    "Top-level YAML key to modify. Only a narrow allowlist of "
                    "YAML-only or YAML-heavy integration keys is accepted (e.g., "
                    "'command_line', 'rest', 'shell_command', 'notify', 'knx'). "
                    "For YAML-mode dashboards, "
                    "use the dotted form 'lovelace.dashboards.<url_path>' where "
                    "<url_path> is lowercase, hyphenated, and not a reserved HA "
                    "route. For themes in themes/*.yaml, use the theme name "
                    "(simple name without dots; content is the mapping of "
                    "theme variables only, without the theme name)."
                ),
            ),
        ],
        action: Annotated[
            str,
            Field(
                description=(
                    "Action to perform: 'add' (insert content if yaml_path is absent; "
                    "otherwise merge one level deep: a list gets content's items "
                    "appended, a mapping gets content's top-level keys set, "
                    "overwriting same-named ones, so a nested mapping or list under "
                    "such a key is replaced, not merged; mismatched types are "
                    "rejected. For a nested edit, read the whole value of yaml_path, "
                    "not a nested path below it, and use 'replace' with the "
                    "complete edited value), "
                    "'replace' (overwrite yaml_path with content), or 'remove' "
                    "(delete yaml_path entirely)."
                ),
            ),
        ],
        content: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "YAML content for the value under yaml_path. Required for 'add' and "
                    "'replace' actions."
                ),
            ),
        ] = None,
        file: Annotated[
            str,
            Field(
                default="configuration.yaml",
                description=(
                    "Relative path to the YAML config file. Also supports 'packages/*.yaml'"
                    " and 'themes/*.yaml'."
                ),
            ),
        ] = "configuration.yaml",
        confirm_token: Annotated[
            str | None,
            Field(
                default=None,
                description=("Confirmation token from a prior preview response."),
            ),
        ] = None,
        MandatoryBPS: Annotated[
            bool,
            Field(default=True),
        ] = True,
        # BestPracticeKey (#1779): consumed by StrictBpsMiddleware, never read
        # here — see strict_bps.py for the declaration contract.
        BestPracticeKey: BestPracticeKeyParam = None,
    ) -> dict[str, Any]:
        """Update raw YAML configuration in configuration.yaml, packages/*.yaml, or themes/*.yaml (LAST RESORT).

        MUST call ha_get_skill_guide OR refer to your locally installed skills first.
        ``template-guidelines.md`` ships under ``skill_content`` by default.

        Destructive, disabled by default. Prefer the dedicated tools:
        ha_config_set_helper for template sensors and every other helper type,
        ha_config_set_automation / ha_config_set_script / ha_config_set_scene
        for storage-mode items.

        Intended for YAML-only integrations with no config-flow or API
        equivalent (command_line, rest, shell_command, notify platforms),
        for integrations with significant YAML-only configuration (knx
        entities in package files), for registering YAML-mode dashboards via
        ``lovelace.dashboards.<url_path>`` (no other ``lovelace.*`` keys),
        and for editing theme files in ``themes/*.yaml``. Themes only load when
        configuration.yaml carries the ``frontend: themes:`` include (e.g.
        ``!include_dir_merge_named themes``); this tool cannot add that include
        (``frontend`` is not an allowed key). ``automation``, ``script`` and
        ``scene`` keys are accepted only when ``file`` is a ``packages/*.yaml``
        and the operator has turned on that key's packages setting (each is
        off by default); writes of those keys to ``configuration.yaml`` are
        rejected; use the storage-mode tools.

        Check ``post_action`` in the response: most keys need a full HA
        restart. For ``themes/*.yaml`` this tool *performs* the reload itself
        (``frontend.reload_themes``), so ``post_action`` is ``reload_performed``
        (or ``reload_available`` plus ``reload_error`` if that reload failed).
        For template, mqtt, group, automation, script, and scene it only
        *advertises* the reload service (``post_action: reload_available`` with
        a ``reload_service`` to call yourself); the edit is on disk but not yet
        live. Preserves YAML comments and HA tags (``!include``,
        ``!secret``) on round-trip; ``replace`` swaps the subtree as-is.

        Two-step confirmation (default ON, toggle ENABLE_YAML_EDIT_CONFIRM /
        Server Settings): the first call returns ``preview: true`` with a
        unified ``diff`` of exactly what would change on disk and a
        ``confirm_token`` — nothing is written. Review the diff, then repeat
        the identical call adding ``confirm_token`` to apply. Every applied
        write also returns the final ``diff``. A token mismatch means the file
        changed since the preview (or the token was wrong); use the freshly
        returned token.
        """
        result: dict[str, Any] = await self.yaml_editor.edit(
            yaml_path=yaml_path,
            action=action,
            content=content,
            file=file,
            confirm_token=confirm_token,
            MandatoryBPS=MandatoryBPS,
            BestPracticeKey=BestPracticeKey,
        )
        return result
