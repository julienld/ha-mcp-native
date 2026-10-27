"""Legacy label registration binds the one public contract to frozen compatibility."""

from typing import Any, cast

from ha_mcp._vendor.fastmcp.tools import tool

from ..backends.legacy import legacy_errors
from ..backends.legacy_labels import LegacyLabels
from ..runtime.label_tools import LabelTools as LabelFacade
from .auto_backup import with_auto_backup
from .helpers import log_tool_usage, register_tool_methods
from .tool_hints import read_only_hints, write_hints


class LabelTools(LabelFacade):
    def __init__(self, client: Any) -> None:
        super().__init__(LegacyLabels(client))

    @property
    def _client(self) -> Any:
        return cast(LegacyLabels, self.labels)._client

    async def _add_label_to_one_area(
        self, label_id: str, area_id: str, assigned: list[str]
    ) -> None:
        await cast(LegacyLabels, self.labels)._add_label_to_one_area(
            label_id, area_id, assigned
        )

    ha_config_get_label = tool(
        name="ha_config_get_label",
        tags={"Labels & Categories"},
        annotations=read_only_hints("Get Label", open_world=False),
    )(log_tool_usage(legacy_errors(LabelFacade.ha_config_get_label)))
    ha_config_set_label = tool(
        name="ha_config_set_label",
        tags={"Labels & Categories"},
        annotations=write_hints(
            "Create or Update Label",
            destructive=True,
            idempotent=False,
            open_world=False,
        ),
    )(
        with_auto_backup(domain="label", id_param="label_id")(
            log_tool_usage(legacy_errors(LabelFacade.ha_config_set_label))
        )
    )
    ha_config_remove_label = tool(
        name="ha_config_remove_label",
        tags={"Labels & Categories"},
        annotations=write_hints(
            "Remove Label", destructive=True, idempotent=True, open_world=False
        ),
    )(
        with_auto_backup(domain="label", id_param="label_id")(
            log_tool_usage(legacy_errors(LabelFacade.ha_config_remove_label))
        )
    )


def register_label_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    register_tool_methods(mcp, LabelTools(client))
