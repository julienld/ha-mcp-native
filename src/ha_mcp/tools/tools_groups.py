"""Remote compatibility bindings for the shared service-group contracts."""

import logging
from typing import Any

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool
from ha_mcp.backends.legacy import legacy_errors
from ha_mcp.client.rest_client import (
    HomeAssistantAuthError,
    HomeAssistantCommandNotSent,
    HomeAssistantConnectionError,
)
from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.runtime.group_tools import GroupTools as _GroupContractTools
from ha_mcp.runtime.group_values import (
    _build_group_service_data,
    group_list_result,
    group_remove_result,
    group_remove_warnings,
    group_set_result,
    group_set_warnings,
)

from .auto_backup import with_auto_backup
from .helpers import (
    exception_to_structured_error,
    get_entity_state_or_none,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    ws_failure_code,
)
from .tool_hints import read_only_hints, write_hints
from .ws_waiters import verify_entity_removed, wait_for_entity_registered

logger = logging.getLogger(__name__)


class RemoteGroups:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def _call_group_service(
        self, service: str, service_data: dict[str, Any]
    ) -> None:
        """Call group.<service> over WebSocket, where Core's schema rejection keeps
        its text; the REST endpoint answers it with a bare 400."""
        object_id = service_data["object_id"]
        try:
            result = await self._client.send_websocket_message(
                {
                    "type": "call_service",
                    "domain": "group",
                    "service": service,
                    "service_data": service_data,
                }
            )
        except HomeAssistantCommandNotSent:
            raise
        except HomeAssistantConnectionError as e:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"group.{service} got no answer, so whether it was applied is "
                    f"unknown: {e}",
                    context={"object_id": object_id, "write_outcome_unknown": True},
                    suggestions=["Check with ha_config_list_groups() before retrying"],
                )
            )
        if result.get("success"):
            return
        error = str(result.get("error", "Unknown error")).removeprefix(
            "Command failed: "
        )
        error_code = result.get("error_code")
        code = ws_failure_code(result)
        suggestions: list[str] | None = None
        if error_code == "not_found":
            # Core's call_service answers not_found only for an unknown service.
            code = ErrorCode.SERVICE_NOT_FOUND
            suggestions = ["The group integration is not loaded in Home Assistant"]
        elif error_code == "invalid_format" and object_id.startswith("group."):
            # Core's slug hint ("try group_x") names a different group.
            suggestions = [
                f"Drop the 'group.' prefix: object_id={object_id.removeprefix('group.')!r}"
            ]
        raise_tool_error(
            create_error_response(
                code,
                f"group.{service} failed: {error}",
                context={"object_id": object_id},
                suggestions=suggestions,
            )
        )

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
            entity_id = f"group.{object_id}"
            existing = await get_entity_state_or_none(self._client, entity_id)
            await self._call_group_service("set", service_data)
            warnings = group_set_warnings(
                service_data,
                existed=existing is not None,
                auto=bool(existing and existing.get("attributes", {}).get("auto")),
            )
            result: dict[str, Any] = {"warnings": warnings} if warnings else {}
            if wait:
                action_word = "created" if existing is None else "updated"
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
            return group_set_result(service_data, result, existed=existing is not None)
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
            entity_id = f"group.{object_id}"
            existing = await get_entity_state_or_none(self._client, entity_id)
            if existing is None:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.ENTITY_NOT_FOUND,
                        f"Group '{entity_id}' not found",
                        context={"entity_id": entity_id},
                        suggestions=[
                            "Use ha_config_list_groups() to find the object_id (without the 'group.' prefix)"
                        ],
                    )
                )
            await self._call_group_service("remove", {"object_id": object_id})
            warnings = group_remove_warnings(
                entity_id, auto=bool(existing.get("attributes", {}).get("auto"))
            )
            result: dict[str, Any] = {"warnings": warnings} if warnings else {}
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
