"""Remote compatibility bindings for the shared service-group contracts."""

import logging
from typing import Any

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool
from ha_mcp.backends.legacy import legacy_errors, legacy_errors_sync
from ha_mcp.client.rest_client import (
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)
from ha_mcp.runtime.group_tools import GroupTools as _GroupContractTools
from ha_mcp.runtime.group_values import (
    _build_group_service_data,
    _validate_group_params,
    group_action_word,
    group_list_result,
    group_remove_result,
    group_set_result,
)

from .auto_backup import with_auto_backup
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    register_tool_methods,
)
from .tool_hints import read_only_hints, write_hints
from .ws_waiters import verify_entity_removed, wait_for_entity_registered

logger = logging.getLogger(__name__)


class RemoteGroups:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def list(self, limit: int, offset: int) -> dict[str, Any]:
        try:
            return group_list_result(await self._client.get_states(), limit, offset)
        except Exception as error:
            exception_to_structured_error(
                error,
                context={"operation": "list_groups"},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify REST API is accessible",
                ],
            )
            raise

    async def set(self, service_data: dict[str, Any], *, wait: bool) -> dict[str, Any]:
        object_id = service_data["object_id"]
        try:
            await self._client.call_service("group", "set", service_data)
            result: dict[str, Any] = {}
            if wait:
                action_word = group_action_word(service_data)
                entity_id = f"group.{object_id}"
                try:
                    registered = await wait_for_entity_registered(
                        self._client, entity_id
                    )
                    if not registered:
                        result.setdefault("warnings", []).append(
                            f"Group {action_word} but {entity_id} not yet queryable. "
                            "It may take a moment to become available."
                        )
                except (HomeAssistantConnectionError, HomeAssistantAuthError) as error:
                    result.setdefault("warnings", []).append(
                        f"Group {action_word} but verification failed: {error}"
                    )
            return group_set_result(service_data, result)
        except ToolError:
            raise
        except Exception as error:
            exception_to_structured_error(
                error,
                context={"object_id": object_id},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify all entity IDs in the entities list exist",
                    "Ensure object_id is valid (no dots, no 'group.' prefix)",
                    "Use ha_config_list_groups() to see existing groups",
                ],
            )
            raise

    async def remove(self, object_id: str, *, wait: bool) -> dict[str, Any]:
        try:
            await self._client.call_service("group", "remove", {"object_id": object_id})
            result: dict[str, Any] = {}
            if wait:
                await verify_entity_removed(self._client, f"group.{object_id}", result)
            return group_remove_result(object_id, result)
        except ToolError:
            raise
        except Exception as error:
            exception_to_structured_error(
                error,
                context={"object_id": object_id},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify the group exists using ha_config_list_groups()",
                    "Groups defined in YAML cannot be permanently removed",
                ],
            )
            raise


class GroupTools(RemoteGroups):
    _validate_group_params = staticmethod(legacy_errors_sync(_validate_group_params))
    _build_group_service_data = staticmethod(_build_group_service_data)

    @property
    def groups(self) -> RemoteGroups:
        return self

    ha_config_list_groups = tool(
        name="ha_config_list_groups",
        tags={"Groups"},
        annotations=read_only_hints("List Groups", open_world=False),
    )(log_tool_usage(legacy_errors(_GroupContractTools.ha_config_list_groups)))

    ha_config_set_group = tool(
        name="ha_config_set_group",
        tags={"Groups"},
        annotations=write_hints(
            "Create or Update Group",
            destructive=True,
            idempotent=False,
            open_world=False,
        ),
    )(
        with_auto_backup(domain="group", id_param="object_id")(
            log_tool_usage(legacy_errors(_GroupContractTools.ha_config_set_group))
        )
    )

    ha_config_remove_group = tool(
        name="ha_config_remove_group",
        tags={"Groups"},
        annotations=write_hints(
            "Remove Group", destructive=True, idempotent=True, open_world=False
        ),
    )(
        with_auto_backup(domain="group", id_param="object_id")(
            log_tool_usage(legacy_errors(_GroupContractTools.ha_config_remove_group))
        )
    )


def register_group_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    register_tool_methods(mcp, GroupTools(client))
