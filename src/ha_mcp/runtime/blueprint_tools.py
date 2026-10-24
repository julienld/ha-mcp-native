"""One public blueprint lifecycle contract, bound to its owning domain service."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from ha_mcp.tools.coercion import JSON_STRING_COERCION


class BlueprintTools:
    def __init__(self, blueprints: Any) -> None:
        self.blueprints = blueprints

    async def ha_manage_blueprints(
        self,
        action: Annotated[
            Literal["list", "get", "import", "save", "delete", "substitute"],
            Field(
                description=(
                    "'list' installed blueprints, 'get' one blueprint's "
                    "metadata/inputs/YAML, 'import' one from a URL, 'save' YAML "
                    "text to a blueprint path, 'delete' an installed one, or "
                    "'substitute' to render a standalone config (the UI's "
                    '"Take control")'
                )
            ),
        ],
        domain: Annotated[
            str,
            Field(
                description=(
                    "Blueprint domain: 'automation' or 'script'. Ignored by "
                    "action='import' — the blueprint file declares its own domain."
                ),
                default="automation",
            ),
        ] = "automation",
        path: Annotated[
            str | None,
            Field(
                description=(
                    "Installed blueprint path, e.g. 'homeassistant/motion_light.yaml' "
                    "(action='get' / 'save' / 'delete' / 'substitute'). 'save' "
                    "appends '.yaml' when it is missing, as Home Assistant does."
                ),
                default=None,
            ),
        ] = None,
        url: Annotated[
            str | None,
            Field(
                description=(
                    "URL to import from — GitHub, Home Assistant Community, or a "
                    "direct YAML link (action='import')"
                ),
                default=None,
            ),
        ] = None,
        yaml: Annotated[
            str | None,
            Field(
                description="Blueprint YAML text to write (action='save')",
                default=None,
            ),
        ] = None,
        source_url: Annotated[
            str | None,
            Field(
                description=(
                    "Origin URL to stamp into the saved blueprint's metadata "
                    "(action='save'); omit for a hand-authored blueprint"
                ),
                default=None,
            ),
        ] = None,
        overwrite: Annotated[
            bool,
            Field(
                description=(
                    "Write over an already-installed blueprint "
                    "(action='import' / 'save'). Home Assistant reloads every "
                    "automation/script using it."
                ),
                default=False,
            ),
        ] = False,
        input: Annotated[  # noqa: A002 — mirrors core's blueprint/substitute key
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Blueprint input values keyed by input name "
                    "(action='substitute'); defaults to {}"
                ),
                default=None,
            ),
        ] = None,
        confirm: Annotated[
            bool,
            Field(
                description="Required confirmation for action='delete'",
                default=False,
            ),
        ] = False,
    ) -> dict[str, Any]:
        """Manage Home Assistant blueprints — list, read, import, save, delete, or render a standalone config.

        One interface for the whole blueprint lifecycle in the ``automation``
        and ``script`` domains.

        DO NOT use this to create an automation or script FROM a blueprint —
        that is ``ha_config_set_automation`` / ``ha_config_set_script`` with a
        ``use_blueprint`` config.

        To duplicate a blueprint, ``get`` it and ``save`` its ``yaml`` under a
        new ``path``; to edit one in place, ``get`` it, change the text, and
        ``save`` it back to the same ``path`` with ``overwrite=True``.

        ``get`` also reports ``used_by``: the automations or scripts built on
        the blueprint, which is the UI's "Show automations using this
        blueprint". Check it before deleting — Home Assistant refuses to delete a
        blueprint anything still uses (the error lists the consumers), and it
        goes on counting a consumer that has since taken control of its own
        config until that consumer is removed.

        CAVEATS: ``get`` returns the on-disk YAML only when something can read
        it — an in-process server, the ha_mcp_tools component, the File & YAML
        Tools entry, or the blueprint's ``source_url``; ``yaml_source`` names
        which one answered, and ``source_url`` text is a fresh download that can
        differ from the installed file. Core's blueprint API alone exposes
        metadata only, so a locally authored blueprint on a bare install has no
        readable text. Both writes are snapshotted first when a copy can be
        read, so ``ha_manage_backup(scope="edits")`` can restore the previous
        file. ``substitute`` only renders — it writes nothing, so pass the
        returned config to ``ha_config_set_automation`` /
        ``ha_config_set_script`` to persist it. To convert an automation or
        script that ALREADY exists, prefer those tools' ``take_control_of_blueprint=True``:
        it renders with that item's own current inputs and saves the result over
        itself in one call.

        EXAMPLES:
        - Get one (with its consumers in ``used_by``): ha_manage_blueprints(action="get", path="homeassistant/motion_light.yaml")
        - Import: ha_manage_blueprints(action="import", url="https://example.com/bp.yaml")
        - Edit in place: ha_manage_blueprints(action="save", path="user/motion.yaml", yaml=<edited text>, overwrite=True)
        - Delete: ha_manage_blueprints(action="delete", path="user/motion.yaml", confirm=True)
        - Render standalone: ha_manage_blueprints(action="substitute", path="user/motion.yaml", input={"motion_sensor": "binary_sensor.hall"})

        RELATED TOOLS: ``ha_config_remove_automation`` / ``ha_config_remove_script``
        to clear consumers blocking a delete, ``ha_search`` to find them.
        """
        result: dict[str, Any] = await self.blueprints.manage(
            action=action,
            domain=domain,
            path=path,
            url=url,
            yaml=yaml,
            source_url=source_url,
            overwrite=overwrite,
            input=input,
            confirm=confirm,
        )
        return result
