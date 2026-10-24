"""One public helper-list signature over bounded catalog/description ports."""

from typing import Annotated, Any, Literal

from pydantic import Field

from ha_mcp.tool_api import BackendTools

from .helper_listing_shape import _paginate_helpers_response
from .helper_schema import SIMPLE_HELPER_TYPES, StorageHelperType


def description_starts_flow(arguments: dict[str, Any]) -> bool:
    """Core may execute a flow during inspection, so it needs write admission."""
    return arguments.get("describe") is True and arguments.get("helper_type") not in {
        *SIMPLE_HELPER_TYPES,
        "all",
    }


class HelperListingTools(BackendTools):
    async def ha_config_list_helpers(
        self,
        helper_type: Annotated[
            StorageHelperType | Literal["all"] | str,
            Field(
                description=(
                    "Helper type to list: a storage type (input_boolean, "
                    "counter, timer, ...), a helper flow Home Assistant lists "
                    "(template, group, utility_meter, ...), or 'all' for every "
                    "type in one call."
                )
            ),
        ],
        limit: Annotated[
            int,
            Field(
                default=100,
                ge=1,
                le=500,
                description="Max helpers to return per page",
            ),
        ] = 100,
        offset: Annotated[
            int,
            Field(
                default=0,
                ge=0,
                description="Number of helpers to skip for pagination",
            ),
        ] = 0,
        describe: Annotated[
            bool,
            Field(
                description=(
                    "Instead of listing, return the fields ha_config_set_helper "
                    "accepts in config for helper_type, as Home Assistant "
                    "reports them (the form the HA UI shows)."
                )
            ),
        ] = False,
        menu_choice: Annotated[
            str | None,
            Field(
                description=(
                    "describe only: sub-type of a menu-based helper "
                    "(template, group), e.g. 'sensor'."
                )
            ),
        ] = None,
        helper_id: Annotated[
            str | None,
            Field(
                description=(
                    "describe only: an existing helper (config entry id for "
                    "flow helpers, entity_id or id otherwise); each field then "
                    "carries its current value."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """List Home Assistant helpers of a specific type with their configurations.

        Returns one page of helpers; `total_count` and `has_more` report the full
        set. Each record carries the configuration for its helper:
        id (immutable storage key), entity_id (current — address the helper by
        this, where available), name (current display name), original_name
        (creation-time name), icon, type-specific settings, and area and label
        assignments. A flow helper record carries entry_id and options instead of
        id; a custom helper integration's options are withheld, marked
        `options_withheld`.

        For a helper renamed in the UI, id/original_name keep the storage values while
        entity_id/name reflect the current entity registry (entity_id is the identifier
        ha_config_set_helper resolves against, so prefer it over id for a renamed helper).
        entity_id/original_name are present only for storage-collection helpers matched in
        the entity registry — types with no backing entity (e.g. tag), and every record when
        the registry read degrades, carry only id/name (a warning flags the degraded case).

        Storage types list what HA's ``{type}/list`` command returns: the
        storage-backed helpers (created via UI/API), not the YAML-defined ones.
        ``person`` is the exception — HA lists its YAML-configured persons
        alongside the storage ones, so both appear here.

        Listing flow types requires the ha_mcp_tools custom component (>= 1.1.0);
        without it, such a listing returns a COMPONENT_NOT_INSTALLED error.

        With helper_type="all", each record carries its own ``helper_type``.
        This mode is component-only (there is no single built-in command that
        lists all types): without the ha_mcp_tools component it returns a
        COMPONENT_NOT_INSTALLED error rather than a partial or empty list.

        describe=True returns each field's name, type, required flag, options,
        default and (with helper_id) current value, or the menu_options a
        menu-based helper needs a menu_choice from. Call it before
        ha_config_set_helper to learn the config keys for a type.

        EXAMPLES:
        - List all counters: ha_config_list_helpers("counter")
        - List every helper type at once: ha_config_list_helpers("all")
        - Next page: ha_config_list_helpers("input_boolean", offset=100)
        - Fields for a template sensor:
          ha_config_list_helpers("template", describe=True, menu_choice="sensor")

        For detailed helper documentation, use ha_get_skill_guide.
        """
        await self._require_list_helper_type(helper_type)
        if describe or menu_choice is not None or helper_id is not None:
            return await self._describe_helper(
                helper_type, menu_choice, helper_id, describe=describe
            )
        return _paginate_helpers_response(
            await self._list_helper_records(helper_type), offset, limit
        )

    async def _require_list_helper_type(self, helper_type: str) -> None:
        if helper_type in SIMPLE_HELPER_TYPES or helper_type == "all":
            return
        result = await self.backend.execute(
            "helpers.flow.types", {"helper_type": helper_type}, self.context
        )
        if helper_type not in result["types"]:
            from .flows.helpers import _raise_unknown_helper_type

            _raise_unknown_helper_type(helper_type, frozenset(result["types"]))

    async def _list_helper_records(self, helper_type: str) -> dict[str, Any]:
        result: dict[str, Any] = await self.backend.execute(
            "helpers.catalog", {"helper_type": helper_type}, self.context
        )
        return result

    async def _describe_helper(
        self,
        helper_type: str,
        menu_choice: str | None,
        helper_id: str | None,
        *,
        describe: bool,
    ) -> dict[str, Any]:
        from .flows.session import FlowSession
        from .flows.settings import FlowSettings, bind_flow_settings
        from .helper_describe import describe_helper_response

        flags = await self.backend.execute("config.flow.settings", {}, self.context)
        with bind_flow_settings(FlowSettings(lambda: flags["redact_secrets"])):
            return await describe_helper_response(
                FlowSession(self.backend, self.context),
                helper_type,
                menu_choice,
                helper_id,
                describe=describe,
            )
